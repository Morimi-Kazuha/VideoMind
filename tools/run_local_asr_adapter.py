"""Exercise the production local Whisper adapter on a real D-drive WAV."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import whisper

from dovideo.application import ReadableSource
from dovideo.infrastructure.media import LocalWhisperTranscriptionAdapter


ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    model = whisper.load_model(
        "tiny.en",
        device="cpu",
        download_root=ROOT / "tools" / "asr" / "models",
    )
    adapter = LocalWhisperTranscriptionAdapter(model)
    spans = await adapter.transcribe(
        ReadableSource(uri=str(ROOT / "work" / "media" / "jfk-smoke.wav"))
    )
    print(
        json.dumps(
            [
                {"startMs": span.start_ms, "endMs": span.end_ms, "text": span.text}
                for span in spans
            ],
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
