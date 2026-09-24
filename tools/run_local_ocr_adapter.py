"""Exercise the production Tesseract adapter on a real extracted frame."""

from __future__ import annotations

import asyncio
from pathlib import Path

from dovideo.infrastructure.media import AsyncSubprocessRunner, TesseractOcrAdapter


ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    adapter = TesseractOcrAdapter(
        AsyncSubprocessRunner(),
        executable=ROOT / "tools" / "tesseract" / "install" / "tesseract.exe",
    )
    text = await adapter.recognize_frame(
        ROOT / "work" / "media" / "jfk-frames" / "frame_01.jpg"
    )
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
