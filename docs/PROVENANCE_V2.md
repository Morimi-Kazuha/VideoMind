# OCR provenance v2

`x2-a-v2` names each OCR frame from the stable media content identity, its
source timestamp, and its extraction index. The local FFmpeg input path and
workspace path are operational values and do not enter the frame reference or
the prepared source revision. An optional uploaded frame location remains
available in `VideoSegment.evidence_frames` without changing source identity.

Historical `x2-a-v1` checkpoints and execution records are not rewritten.
Legacy source items with no version field still decode as v1. Golden dataset
v1 remains immutable and its frozen source revision is not expected to match
the new contract.

On the representative 5,545,108-byte media, two local passes used separate
UUID input paths and workspaces. Each produced six ASR and one OCR observation.
Both passes produced the same source revision
`0d69d3413d4adc2746d9c2755b9d33b41bab1defc4aafc75f9fca12e4c540495`,
the same segment IDs, and the same source item IDs. The audit used the bundled
FFmpeg and Tesseract adapters, the provisioned `tiny.en` Whisper model, and
R4's FFmpeg PATH bootstrap. Whisper ran in the local Python 3.13 environment
because the project test venv does not contain PyTorch; its segments were
decoded with the production `_decode_segments` function. No provider,
embedding, vector write, or AgentLoop call occurred.
