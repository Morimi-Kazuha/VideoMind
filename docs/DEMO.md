# DOVideo Python — legacy demo

This document describes the temporary standard-library Web Demo only. It is
not the final product architecture; the final frontend/API direction is Vue
3 + Vite → FastAPI + SSE.

This demo shows the product path over the already-prepared representative
media. It reuses the existing real media and local Whisper artifact; it does
not regenerate media or run eSpeak.

## Legacy Web Demo

Start the optional local Web Demo with one command:

```powershell
$env:PYTHONPATH = "src;tools/asr/python-packages"
& D:\python\python.exe -m dovideo web
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). The page is a
single presentation surface, not an admin dashboard. Its flow is:

1. Select a supported local video (`.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`, or
   `.m4v`). The server streams it into a generated safe name under
   `work/uploads/`; the original filename is display metadata only.
2. Enter an analysis goal and choose `Local TF-IDF` or configured `Remote
   BGE-M3`.
3. Click `Analyze video`. The server creates a process-local demo job and
   runs the same `VideoAnalysisApplication` used by the CLI in a bounded
   background thread.
4. The browser polls `GET /api/analysis/{id}` and renders the reliable
   composition progress stages. It does not call a model provider directly.
5. The completed result shows conclusions, source type, evidence text, and
   the exact point timestamp plus its containing 60-second context window.
   `Jump to evidence` seeks the native video player to the evidence timestamp.

Configure model/provider credentials in the server process environment using
`.env.example`; no key is entered into the page or returned by the API. Remote
embedding errors are surfaced and are never downgraded to Local TF-IDF. The
demo registry is intentionally in-memory browser state and is not the durable
Phase 8/9 task/checkpoint system. The local UI is not production SaaS or an
internet-hardened upload service.

## Local-vector demo

Set the structured model variables from `.env.example` in the process
environment, then run:

```powershell
$env:PYTHONPATH = "src;tools/asr/python-packages"
& D:\python\python.exe -m dovideo analyze `
  .\work\media\representative-long.mp4 `
  --goal "What does the later poem say about the sea, pool, and tide?" `
  --embedding-mode local `
  --output .\work\media\phase11-demo.md
```

The output is user-facing Markdown. Progress labels include `MEDIA`, `ASR`,
`OCR`, `CONTEXT`, `CHUNK`, `EMBEDDING`, `RETRIEVAL`, `PLANNER`, `EXECUTOR`,
`CRITIC`, and `DONE`. Evidence lines show a timestamp, its source window
range, and `ASR`/`OCR` source.

## Remote-vector verification

Remote embedding is a separate, explicit operator check so pytest remains
offline:

```powershell
$env:PYTHONPATH = "src"
& D:\Agent Learning\.pico-baseline-venv\Scripts\python.exe `
  .\tools\run_phase11_embedding_live.py
```

Set `DOVIDEO_EMBEDDING_API_KEY` only in the process environment. The script
first sends one harmless smoke input, then embeds the two existing Phase 10B
five-minute chunks and one query through `EmbeddingPort` and the existing
retrieval service. It writes only sanitized dimensions/counts and retrieval
metadata to `work/media/phase11-embedding-live.json`.

The script classifies the result as exactly `LIVE VERIFIED`, `LIVE PROVIDER
BLOCKED`, `ADAPTER DEFECT`, or `CONFIGURATION BLOCKED`. Read the current
`LUNA_REPORT.md` for the authoritative phase status. The current run is
`LIVE VERIFIED`: `BAAI/bge-m3` returned four dimension-1024 vectors, and the
existing retrieval selected the later `300–360s` After Love region from two
existing five-minute chunks with six candidates.
