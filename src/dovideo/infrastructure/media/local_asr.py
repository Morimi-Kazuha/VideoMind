"""Optional local OpenAI Whisper adapter for the existing ASR port."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dovideo.application import ReadableSource, TranscriptSpan

from .errors import AsrResponseError


class LocalWhisperTranscriptionAdapter:
    """Map an injected OpenAI Whisper model to ``TranscriptionPort``.

    The Whisper package/model remain infrastructure concerns.  They are
    injected for offline composition, while ``from_openai_whisper`` provides a
    lazy convenience loader for an explicitly provisioned local installation.
    """

    def __init__(
        self,
        model: object,
        *,
        fp16: bool = False,
        language: str | None = None,
    ) -> None:
        if model is None:
            raise TypeError("a local Whisper model is required")
        self._model = model
        self._options: dict[str, Any] = {
            "fp16": fp16,
            "verbose": False,
            "temperature": 0,
            "condition_on_previous_text": False,
        }
        if language is not None:
            if not isinstance(language, str) or not language.strip():
                raise ValueError("language must be nonblank when provided")
            self._options["language"] = language.strip()

    @classmethod
    def from_openai_whisper(
        cls,
        model_name: str = "tiny.en",
        *,
        model_root: str | Path | None = None,
        device: str = "cpu",
        language: str | None = None,
    ) -> "LocalWhisperTranscriptionAdapter":
        """Load a caller-selected local Whisper model without import-time I/O."""

        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be nonblank")
        try:
            import whisper  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AsrResponseError("local Whisper package is unavailable") from exc
        kwargs: dict[str, Any] = {"device": device}
        if model_root is not None:
            kwargs["download_root"] = str(Path(model_root))
        model = whisper.load_model(model_name.strip(), **kwargs)
        return cls(model, language=language)

    async def transcribe(
        self,
        source: ReadableSource,
        *,
        trace_id: str | None = None,
    ) -> tuple[TranscriptSpan, ...]:
        del trace_id
        if not isinstance(source, ReadableSource):
            raise TypeError("source must be a ReadableSource")
        result = await asyncio.to_thread(self._invoke, source.uri)
        return _decode_segments(result)

    async def transcribe_path(
        self,
        audio_path: str | Path,
        *,
        trace_id: str | None = None,
    ) -> tuple[TranscriptSpan, ...]:
        return await self.transcribe(
            ReadableSource(uri=str(Path(audio_path))),
            trace_id=trace_id,
        )

    def _invoke(self, source: str) -> object:
        transcribe = getattr(self._model, "transcribe", None)
        if not callable(transcribe):
            raise AsrResponseError("local Whisper model has no transcribe method")
        return transcribe(source, **self._options)


WhisperLocalTranscriptionAdapter = LocalWhisperTranscriptionAdapter


def _decode_segments(result: object) -> tuple[TranscriptSpan, ...]:
    if not isinstance(result, Mapping):
        raise AsrResponseError("local Whisper response is not an object")
    segments = result.get("segments", ())
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes, bytearray)):
        raise AsrResponseError("local Whisper segments are malformed")
    decoded: list[TranscriptSpan] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise AsrResponseError("local Whisper segment is malformed")
        start = _seconds(segment.get("start"), "start")
        end = _seconds(segment.get("end"), "end")
        start_ms = round(start * 1000)
        end_ms = round(end * 1000)
        if end_ms <= start_ms:
            raise AsrResponseError("local Whisper segment range is invalid")
        text = segment.get("text", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise AsrResponseError("local Whisper segment text is malformed")
        decoded.append(
            TranscriptSpan(start_ms=start_ms, end_ms=end_ms, text=text)
        )
    return tuple(decoded)


def _seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AsrResponseError(f"local Whisper segment {name} is malformed")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise AsrResponseError(f"local Whisper segment {name} is invalid")
    return result


__all__ = [
    "LocalWhisperTranscriptionAdapter",
    "WhisperLocalTranscriptionAdapter",
]
