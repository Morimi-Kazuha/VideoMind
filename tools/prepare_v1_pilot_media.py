"""Local real-media preparation; no upload, no downloads, no synthetic transcript.

Paths are supplied explicitly. Reuses VideoMind ffprobe, keyframe, Whisper,
Tesseract and VideoContext adapters. All outputs must stay in ignored work/.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dovideo.application.context import VideoContextBuilder
from dovideo.application.value_objects import (AsrBranchOutcome, OcrBranchOutcome,
    MediaObservationBundle, TranscriptSpan, OcrObservation)
from dovideo.infrastructure.media.ffprobe import FfprobeDurationAdapter
from dovideo.infrastructure.media.runner import AsyncSubprocessRunner
from dovideo.infrastructure.media.keyframes import KeyframeExtractor
from dovideo.infrastructure.media.workspace import MediaWorkspace
from dovideo.infrastructure.media.ocr import TesseractOcrAdapter
from dovideo.infrastructure.media.local_asr import LocalWhisperTranscriptionAdapter


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def private_output(path):
    path = Path(path).resolve()
    path.relative_to((ROOT / "work").resolve())
    check = subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}",
        "check-ignore", str(path)], cwd=ROOT, capture_output=True)
    if check.returncode != 0:
        raise ValueError("private output must be gitignored under work/")
    path.mkdir(parents=True, exist_ok=True)
    return path


async def prepare(args):
    print("PREPARATION_START", flush=True)
    out = private_output(args.output)
    media = args.media.resolve(strict=True)
    identity = digest(media)
    model_digest = digest(args.model)
    configuration = {"pipelineVersion": "v1-pilot-local-media-v1", "mediaSha256": identity,
        "modelSha256": model_digest, "model": "OpenAI Whisper local multilingual tiny",
        "language": "zh", "temperature": 0, "conditionOnPreviousText": False,
        "fp16": False, "device": args.device, "sourcePrefix": args.source_prefix,
        "audio": "16000Hz mono PCM; full recording, absolute ASR timestamps",
        "ocr": "Tesseract chi_sim+eng; every existing scene/30-second keyframe, no dedup",
        "toolDigests": {name: digest(path) for name, path in
            (("ffmpeg", args.ffmpeg), ("ffprobe", args.ffprobe), ("tesseract", args.tesseract))},
        "preparationCodeSha256": digest(__file__), "networkCalls": 0}
    cache_path = out / "preparation-config.json"
    if cache_path.exists() and json.loads(cache_path.read_text(encoding="utf-8")) != configuration:
        raise ValueError("existing media cache has another source/configuration; use a new output")
    save(cache_path, configuration)
    started = time.monotonic()
    runner = AsyncSubprocessRunner()
    duration = await FfprobeDurationAdapter(runner, executable=args.ffprobe).probe(media)
    probe = await runner.run((str(args.ffprobe), "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(media)), timeout=60)
    probe_json = json.loads(probe.stdout)
    if not {"audio", "video"}.issubset({s["codec_type"] for s in probe_json["streams"]}):
        raise ValueError("valid audio and video streams are required")
    save(out / "ffprobe.json", probe_json)
    wav = out / "audio.wav"
    if not wav.exists():
        await runner.run((str(args.ffmpeg), "-nostdin", "-n", "-v", "error", "-i", str(media),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)), timeout=180)
    print("PROBE_AND_AUDIO_READY", flush=True)

    async def ocr():
        path = out / "ocr.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        observations = []
        frames_dir = out / "frames"
        frames_dir.mkdir(exist_ok=True)
        async with MediaWorkspace(parent=out, prefix="extract-") as workspace:
            frames = await KeyframeExtractor(runner, executable=args.ffmpeg).extract(media, workspace)
            adapter = TesseractOcrAdapter(runner, executable=args.tesseract)
            for frame in frames:
                # Original PTS is preserved by the existing extractor's showinfo parser.
                name = f"{frame.timestamp_ms:09d}.jpg"
                target = frames_dir / name
                shutil.copyfile(frame.path, target)
                text = await adapter.recognize_frame(target)
                observations.append({"timestamp_ms": frame.timestamp_ms, "text": text,
                    "frame_ref": "frames/" + name, "frameSha256": digest(target)})
                save(path, observations)
        save(out / "ocr-complete.json", {"count": len(observations)})
        print(f"OCR_READY {len(observations)}", flush=True)
        return observations

    async def asr():
        path = out / "asr.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if args.whisper_packages:
            sys.path.insert(0, str(args.whisper_packages.resolve()))
        if args.torch_packages:
            sys.path.append(str(args.torch_packages.resolve()))
        os.environ["PATH"] = str(args.ffmpeg.parent.resolve()) + os.pathsep + os.environ["PATH"]
        import torch
        import whisper
        torch.set_num_threads(args.cpu_threads)
        torch.manual_seed(0)
        model = await asyncio.to_thread(whisper.load_model, str(args.model.resolve()), device=args.device)
        adapter = LocalWhisperTranscriptionAdapter(model, language="zh")
        print("ASR_RUNNING_LOCAL", flush=True)
        begun = time.monotonic()
        spans = await adapter.transcribe_path(wav)
        result = [s.model_dump(mode="json", by_alias=False) if hasattr(s, "model_dump") else
            {"start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text} for s in spans]
        save(path, result)
        save(out / "asr-runtime.json", {"seconds": time.monotonic()-begun,
            "torch": torch.__version__, "whisperModuleSha256": digest(whisper.__file__),
            "cpuThreads": args.cpu_threads, "device": args.device, "calls": 1, "providerTokens": "NOT_MEASURED"})
        print(f"ASR_READY {len(result)}", flush=True)
        return result

    # Incomplete OCR caches may not be reused as a full-video branch.
    if (out / "ocr.json").exists() and not (out / "ocr-complete.json").exists():
        raise ValueError("incomplete OCR cache; use another output instead of overwriting")
    speech, visual = await asyncio.gather(asr(), ocr())
    if any(s["start_ms"] < 0 or s["end_ms"] > duration.milliseconds + 500 for s in speech):
        raise ValueError("ASR timestamps outside media")
    context = VideoContextBuilder().build(args.source_prefix + identity[:16], "",
        MediaObservationBundle(asr=AsrBranchOutcome(observations=tuple(TranscriptSpan(**s) for s in speech),
            attempted=1), ocr=OcrBranchOutcome(observations=tuple(OcrObservation(
                timestamp_ms=s["timestamp_ms"], text=s["text"], frame_ref=s["frame_ref"]) for s in visual),
                attempted=len(visual))), media_content_identity=identity)
    save(out / "context.json", context.model_dump(mode="json", by_alias=True))
    save(out / "media-manifest.json", {**configuration, "mediaPath": str(media),
        "sizeBytes": media.stat().st_size, "durationMs": duration.milliseconds,
        "sourceRevision": context.source_revision, "provenanceVersion": context.provenance_version,
        "segments": len(context.segments), "sourceItems": len(context.observations),
        "asrSpans": len(speech), "ocrFrames": len(visual), "realAsr": True, "realOcr": True,
        "elapsedSeconds": time.monotonic()-started, "chapter": "NEEDS_CONTENT_REVIEW",
        "artifactDigests": {name: digest(out/name) for name in ("asr.json", "ocr.json", "context.json", "ffprobe.json")}})
    print("CONTEXT_READY " + context.source_revision, flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("media", "output", "ffmpeg", "ffprobe", "tesseract", "model"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--whisper-packages", type=Path)
    p.add_argument("--torch-packages", type=Path)
    p.add_argument("--cpu-threads", type=int, default=4)
    p.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    p.add_argument("--source-prefix", default="wangdao-pilot-")
    asyncio.run(prepare(p.parse_args()))


if __name__ == "__main__":
    main()
