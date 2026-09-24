# Phase 1 domain contract

This document is the wire/domain contract frozen from the Java records and
enums.  JSON examples use Java/Jackson names.  Python code normally uses the
snake_case field names shown in parentheses.  Every model is a frozen Pydantic
v2 model and every collection is a tuple after validation.  The tuple choice
provides the Java `List.copyOf` behavior (caller mutations do not change a
parsed object) while remaining naturally JSON-serializable as an array.

## Naming and serialization

`model_validate()` accepts both snake_case and the explicit camelCase aliases;
`model_dump_json(by_alias=True)` emits the Java names.  The base model also
defaults serialization to aliases for migration payloads.  A Python-only
consumer may request `by_alias=False`.  Enum values are their uppercase Java
names.  Unknown JSON fields are ignored, matching the default Jackson DTO
behavior used by the baseline.  When both spellings of one field are present,
equal values are accepted (list and tuple forms compare by content), while
conflicting values—including `None` versus a non-`None` value—raise a
validation error.  A `None` supplied under only one spelling is normalized on
that spelling; omitted fields use the model default.  This explicit
reject-on-conflict policy prevents Pydantic's normal alias precedence from
silently discarding a migration value.

## Video models

### `VideoSegment` (`VideoContext.VideoSegment` in Java)

| JSON field | Python field/type | Default | Contract |
| --- | --- | --- | --- |
| `startMs` | `start_ms: int` | `0` when omitted | Must be ≥ 0. |
| `endMs` | `end_ms: int` | `0` when omitted | Must be strictly greater than `start_ms`. |
| `transcript` | `transcript: str` | `""` | Null becomes empty; surrounding whitespace is trimmed. |
| `ocrTexts` | `ocr_texts: tuple[str, ...]` | `()` | Null becomes empty; immutable defensive copy. |
| `evidenceFrames` | `evidence_frames: tuple[str, ...]` | `()` | Null becomes empty; immutable defensive copy. |

`start_time`/`end_time` properties are compatibility conveniences.  No
non-empty-text requirement is added: OCR-only and ASR-only windows are valid.

### `VideoContext`

| JSON field | Python field/type | Default | Contract |
| --- | --- | --- | --- |
| `source` | `source: str` | required | Must be non-null and non-blank; unlike `userGoal`, it is not trimmed. |
| `userGoal` | `user_goal: str` | `""` | Null becomes empty and surrounding whitespace is trimmed. |
| `segments` | `segments: tuple[VideoSegment, ...]` | `()` | Null becomes empty; nested nulls are rejected. |

`transcript_text()` joins non-blank segment transcripts with `\n`, in stored
order.  Java construction does not sort segments, so the model does not sort
them either.

### `VideoChunk` and `ChunkSummary`

`VideoChunk` uses Python `start_ms`/`end_ms` with JSON aliases `startTime` and
`endTime`, because those are the Java record fields.  `start_time` and
`start_ms` input spellings are both accepted.  `start_ms >= 0` and
`end_ms > start_ms` are required.  `segmentSummary` is null-to-empty and
trimmed; `keywords`, `rawSegments`, and `embedding` default to empty immutable
tuples.  `ChunkSummary` has the same null-to-empty/trim behavior for
`segmentSummary` and immutable `keywords`.

### `VideoEvidenceHit`

Fields are `startMs`, `endMs`, `source`, `snippet`, `transcript`, and
`ocrTexts`.  Text nulls become empty strings and `ocrTexts` null becomes an
empty tuple.  The Java DTO performs no timestamp-range validation, so Phase 1
intentionally performs none either; retrieval/application code may impose
range checks at its boundary.

### `VideoRetrievalIntent`

`semanticQuery` is null-to-empty and trimmed.  Each keyword list removes null,
trims strings, removes blanks, deduplicates while preserving first-seen order,
and keeps at most 16 terms.  This is case-sensitive, matching Java
`distinct()`.

## Analysis models

### `AnalysisResult`

| JSON field | Python field/type | Default | Contract |
| --- | --- | --- | --- |
| `title` | `title: str` | `"未命名分析"` | Null becomes that title; non-null text is trimmed, blank remains blank. |
| `conclusions` | `tuple[str, ...]` | `()` | Null becomes empty; values are otherwise preserved. |
| `evidence` | `tuple[AnalysisEvidence, ...]` | `()` | Null becomes empty; nested values validated. |
| `suggestions` | `tuple[str, ...]` | `()` | Null becomes empty; values are otherwise preserved. |
| `sections` | `tuple[AnalysisSection, ...]` | `()` | Null becomes empty; mode-specific sections remain optional. |

`AnalysisEvidence` has `timestampMs` (`timestamp_ms`, default 0, must be
nonnegative), `source` (default `UNKNOWN`, null-to-default, trimmed), `content`
and `claim` (null-to-empty, trimmed).  The DTO intentionally does not enforce
the prompt's ASR/OCR source vocabulary; Phase 6's verifier will do that.

`AnalysisSection` has `key`, `title`, and `items`; all text labels are
null-to-empty and trimmed, and `items` is an immutable tuple.  `to_markdown()`
renders the exact common Java structure (title, conclusions, timestamped
evidence, suggestions) followed by sections.  A section's `key` is for
programmatic profile checks and its `title` is rendered for users.

## Agent models

`AgentState` has `goal` (required, nonblank, trimmed), optional `plan`,
`result`, and `critique`, plus `round` (default 0, nonnegative).  `AgentPlan`
has `understoodGoal` (null-to-empty, trimmed) and `tasks` (null-to-empty
tuple).  The Java record itself accepts an empty/oversized task list so a
malformed model response can be repaired; `is_execution_valid()` and
`execution_violations` expose the `AgentLoopService` rule of one to five
nonblank tasks, each no longer than 500 characters.

`CriticResult` has primitive `passed` (default false when missing/null) and
immutable lists `feedback`, `missingRequirements`, `unsupportedClaims`, and
`requiredTimestamps`, each null-to-empty.  Timestamp sign/range is not checked
by the Java record; the context/evidence service handles that decision.

## Modes, task status, and events

`AnalysisMode` values are `GENERAL`, `LEARNING`, `REVIEW`, and `CREATION`.
`from_nullable()` maps null, blank, and unknown values to GENERAL.  This
matches queue/model compatibility.  `from_request()` maps missing/blank to
GENERAL but raises `ValueError` for an explicit unknown mode, matching the
HTTP boundary's strict behavior.

`ModeProfile` captures `mode`, `displayName`, the three role instructions, and
deduplicated `requiredSectionKeys`.  It is a schema only; Phase 1 does not
instantiate a registry or call a model.

`TaskStatusState` values are `NOT_STARTED`, `QUEUED`, `PROCESSING`,
`COMPLETED`, and `FAILED`.  `TaskStatus` retains `of(state, message)` and
`completed(result)` factory behavior, including the warning-prefixed markdown
when completed from an AgentState whose Critic did not pass.  `TaskStage`
retains all Java values.  `TaskStage.from_value()` returns `None` for null,
blank, or unknown strings and deliberately does not trim before matching.
`TaskEvent.of(status, stage)` copies status fields; `terminal()` is true only
for COMPLETED or FAILED.

Nested Java record spellings remain available as compatibility attributes:
`VideoContext.VideoSegment`, `VideoChunk.ChunkSummary`,
`AnalysisResult.Evidence`, `AnalysisResult.Section`, `AgentState.AgentPlan`,
`AgentState.CriticResult`, and `TaskStatus.State`.

## Deliberate differences and reasons

1. Lists are tuples and models are frozen.  This is a Python-native equivalent
   of Java records plus `List.copyOf`, and prevents accidental state mutation.
2. Python snake_case names coexist with explicit aliases rather than relying on
   a global alias generator.  This makes the wire contract auditable and
   stable when future fields are added.  If both spellings are sent, equal
   values are accepted and conflicting values are rejected before alias
   precedence can hide data.
3. Java nested records are top-level Python classes with nested compatibility
   attributes.  Top-level imports are easier to type-check and reuse while
   old checkpoint payloads retain their shape.
4. DTO parsing and AgentLoop execution validation are separate for
   `AgentPlan`.  This preserves the Java repair path instead of making an
   invalid model response impossible to represent.
5. Evidence source vocabulary and claim/timestamp containment are not Pydantic
   constructor rules.  They belong to the future Evidence Verification
   service; converting them into a prompt-only or parse-only rule would lose
   the baseline behavior's explicit verifier boundary.
