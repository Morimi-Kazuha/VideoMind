# VideoMind temporal and citation read model

VideoMind uses the Pixel Future Academy interface. The Media Library selects an
owned media record; the Analysis Workspace joins its original video, full
context windows, query-specific evidence, and AI result.

## Data lineage and granularity

Media extraction produces ASR `TranscriptSpan` and OCR `OcrObservation` values.
`VideoContextBuilder` assigns the existing deterministic `SourceItemIdentity` to
each original observation and groups them into fixed 60-second `VideoSegment`
windows. New contexts persist the original text together with that identity as
`VideoContext.observations`. The media-scoped `VideoContext` checkpoint is the
single durable source for temporal observations and the aggregated window
projection. The repository writes that one checkpoint durably and mirrors it
through its existing Redis cache-aside path; no new database table, migration,
or separate observation store was introduced. Five-minute chunks still retain
raw windows for retrieval.
Retrieval returns at most eight ranked `VideoEvidenceHit` values per query;
these are **not** the full video corpus.

The model adapter serializes the window projection for Planner, Executor,
Critic, and related Agent prompts, explicitly excluding the new observation
collection. This preserves the prior prompt contract and avoids sending the
same source text twice. The first live run after adding observations exposed
this boundary: it exhausted the 50,000-token Agent budget after repeated
provider calls. With the prompt projection corrected, a fresh real R4 run
completed with 53 durable observations and three verified citations.

New contexts retain exact ASR span start/end and OCR extraction timestamp, text,
source ordinal, segment ID, source revision, and the stable OCR frame digest
when present. The digest is a reference identity, not an image URL. Frame
locations and local paths remain private. The `TemporalObservation` validator
checks text and frame digests against the corresponding source identity, and
`VideoContext` checks that every observation belongs to a source item in its
own revision. Existing checkpoints have no `observations` field and continue
to decode; their source-item identities retain times and digests, but their
original text cannot be reconstructed losslessly. Those records use the
existing **context window** read path, which does not claim per-utterance or
per-frame text precision.

`GET /analysis/temporal-observations?id=...&limit=100&offset=0` returns an
owned, read-only page with `available`, `granularity: source-observation`,
`sourceRevision`, `total`, `limit`, `offset`, and `items`. Items include stable
`id`, `segmentId`, `sourceRevision`, `kind`, `ordinal`, exact `startMs`, real
`endMs` for ASR, original `text`, and a hashed `frameId` for OCR when present.
Ordering is deterministic by time, source kind, ordinal, and ID. The page limit
is 1–200. The API reads only the currently persisted media context, so an
overwritten/reprocessed context cannot mix old and new revisions. A new
analysis goal for the same media normally reuses the current context rather
than rerunning extraction. Historic contexts expose `available: false` and an
empty observation page; they are never silently upgraded or rewritten.

`GET /analysis/temporal-windows?id=...&limit=100&offset=0` returns an owned,
read-only page with `available`, `granularity`, `total`, `limit`, `offset`, and
`items`. Each item contains only persisted `segmentId`, `sourceRevision`,
`startMs`, `endMs`, `transcript`, and `ocrTexts`. The page limit is 1–200. The
client loads the first 200 windows and observations, then offers explicit
additional pages. The timeline uses bounded pixel bins for dense observations;
detailed rows load progressively. New records seek to their source timestamp.
Historic rows seek to the window start and remain labelled as such. No source
frame paths or raw digests are exposed by the window endpoint.
Until every observation page has loaded, the timeline keeps the loaded window
overview instead of presenting a partial observation page as a complete
fine-grained history. If the windows themselves are incomplete, the UI says
that additional windows have not loaded. The detailed record list separately
shows how many observations are loaded out of the total.

The final real-browser acceptance used the existing 5.8-minute validation
media and an explicit 100,000 estimated-token ceiling for its local worker.
The application default remains 50,000. Successful single-pass runs had
already consumed more than that default in aggregate because the current
budget guard runs at stage boundaries; a provider-format fallback or Critic
rewrite can make this representative long-video run exceed the default before
the next stage. The acceptance override is local runtime configuration, not a
change to frozen model profiles or benchmark policy.

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

All three temporal/citation read endpoints apply the same media ownership
check as existing analysis reads. They do not mutate checkpoints or require a
data migration. All returned ASR/OCR
text is rendered as Vue text, not unsafe HTML. The observation page currently
loads and sorts the in-checkpoint collection before slicing. This is bounded
on the wire but scales linearly with observation count and requires decoding
the full context checkpoint; a dedicated indexed store is a later option only
if measured long-video workloads justify it. The timeline keeps the
`VideoSegment` overview for older media and uses exact observation positions
for new media. Answer-level citations remain derived from verified evidence;
the UI does not fabricate sentence-level attribution from observations.

## Compatibility identifiers

The public product name is VideoMind. The Python import package `dovideo`,
`DOVIDEO_*` environment variables, existing routes and DTO fields, durable
checkpoint/queue names, and `dovideo:` browser draft keys remain unchanged for
compatibility. Historical X3 reports retain their original DOVideo name.
The frozen model/Jev system instructions also retain their original internal
name so the exact benchmark request contract is unchanged by a visual rename.
