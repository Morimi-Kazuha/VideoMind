# VideoMind temporal and citation read model

VideoMind uses the Pixel Future Academy interface. The Media Library selects an
owned media record; the Analysis Workspace joins its original video, full
context windows, query-specific evidence, and AI result.

## Data lineage and granularity

Media extraction produces ASR `TranscriptSpan` and OCR `OcrObservation` values.
`VideoContextBuilder` groups those source observations into fixed 60-second
`VideoSegment` windows. Each window retains aggregated transcript/OCR text and
stable provenance identities (`sourceRevision`, `segmentId`, `sourceItemId`),
then the checkpoint stores `VideoContext` per media item. Five-minute chunks
retain raw windows for retrieval. Retrieval returns at most eight ranked
`VideoEvidenceHit` values per query; these are **not** the full video corpus.

The persisted source-item identities retain exact timestamps and content
digests, but do not contain independent source text. Consequently, the new
read API exposes complete **context windows**, not exact per-utterance or
per-frame text records. A window's `startMs` and `endMs` describe its 60-second
bucket. OCR and ASR text within the window may occur at different exact times;
the UI labels a seek to the window's start accordingly.

`GET /analysis/temporal-windows?id=...&limit=100&offset=0` returns an owned,
read-only page with `available`, `granularity`, `total`, `limit`, `offset`, and
`items`. Each item contains only persisted `segmentId`, `sourceRevision`,
`startMs`, `endMs`, `transcript`, and `ocrTexts`. The page limit is 1–200. The
client loads the first 200 windows, then offers explicit additional pages.
No source frame paths or content digests are exposed.

## Answer-level citations

The Agent result already has `AnalysisEvidence` records. During Critic work,
unambiguous evidence text can be bound to source item identities; the verifier
checks revision, segment, source type, exact source text, and time. The new
`GET /analysis/agent-citations?id=...&goal=...&mode=...` projects only records
whose claim belongs to a result conclusion and whose source identity passes
the verifier. The read projection may bind a legacy result if the existing
deterministic binder finds exactly one source candidate; it never trusts a
model-supplied ID without verification. Ambiguous, invalid, unbound, and
historical answers simply return no structured citations.

Citations are at **answer level**. The API does not claim per-sentence or
per-token attribution. The existing markdown status, SSE, retrieval, and
feedback contracts remain unchanged. The UI continues to support legacy
markdown timestamps when no structured citations are available.

Both new endpoints apply the same media ownership check as existing analysis
reads. They do not mutate checkpoints or require a data migration.

## Compatibility identifiers

The public product name is VideoMind. The Python import package `dovideo`,
`DOVIDEO_*` environment variables, existing routes and DTO fields, durable
checkpoint/queue names, and `dovideo:` browser draft keys remain unchanged for
compatibility. Historical X3 reports retain their original DOVideo name.
The frozen model/Jev system instructions also retain their original internal
name so the exact benchmark request contract is unchanged by a visual rename.
