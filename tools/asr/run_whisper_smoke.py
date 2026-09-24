"""Run one real local Whisper smoke against the prepared D-drive media."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import whisper


ROOT = Path(__file__).resolve().parents[2]
audio_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "work" / "media" / "smoke-audio.wav"
model_name = sys.argv[2] if len(sys.argv) > 2 else "tiny"
output_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
model = whisper.load_model(
    model_name,
    device="cpu",
    download_root=ROOT / "tools" / "asr" / "models",
)
result = model.transcribe(
    str(audio_path),
    fp16=False,
    verbose=False,
    temperature=0,
    condition_on_previous_text=False,
)
payload = {
    "text": result.get("text", "").strip(),
    "segments": [
        {
            "start": round(float(segment["start"]), 3),
            "end": round(float(segment["end"]), 3),
            "text": segment["text"].strip(),
        }
        for segment in result.get("segments", [])
        if segment.get("text", "").strip()
    ],
}
encoded = json.dumps(payload, ensure_ascii=False, indent=2)
if output_path is not None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(encoded + "\n", encoding="utf-8")
print(encoded)
