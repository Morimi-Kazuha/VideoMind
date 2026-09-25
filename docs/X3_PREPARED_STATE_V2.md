# X3 v2 prepared source snapshot

This is a measured local persistence snapshot, not a benchmark run.

- New media ID: 17. It was ingested through `MediaIngestService` from the
  representative media bytes; historical media ID 16 was not changed.
- Content hash: `cd22673b3eed2108b558385371668b74` (DOVideo MD5 identity).
- Source revision: `45d7e5513af70f05c36ffeb04d981948d2cec85c874ff6f4ac5b5f0e83ca47bd`.
- Provenance version: `x2-a-v3`; five context windows and 53 source items
  (52 ASR, one OCR).
- Two prepared chunks with 1024 dimensional embeddings. Qdrant contains two
  points for media ID 17 with matching `sourceRevision` and `chunkId` payloads.

The first preparation attempt successfully saved the context and made two
CHUNK_SUMMARY and two embedding calls, then failed saving chunks because
their checkpoint payload exceeded MySQL `TEXT` capacity. An offline payload
estimate was 68,090 bytes. The checkpoint column was widened to `LONGTEXT`
without deleting rows. The same media ID was resumed; its saved context was
reused and two further CHUNK_SUMMARY and embedding calls completed. The local
Redis trace records four model calls, four embedding calls, two vector writes,
and 29,842 tokens in its `estimatedTokens` field. The chat client populates
that field from provider usage when available; the trace does not retain
per-call token splits. `estimatedCost` is zero because no cost was reported
through this path, so zero is not a measured provider charge.

No AgentLoop or formal X3-C benchmark execution occurred during preparation.
