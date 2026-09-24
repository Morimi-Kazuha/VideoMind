# DOVideo X3-B EXECUTION REPORT

## 1. Status

PASS. X3-B evaluation-only execution and deterministic metric collection are implemented and verified offline.

No Provider, Jev, Celery, Docker, media ingestion, FFmpeg, ASR, OCR, or canonical benchmark execution was started.

## 2. Files Changed

- `src/dovideo/application/evaluation_contracts.py`
- `src/dovideo/application/evaluation_runner.py`
- `src/dovideo/application/__init__.py`
- `src/dovideo/evaluation/__init__.py`
- `tests/application/test_x3b_evaluation_runner.py`
- `X3_B_EXECUTION_REPORT.md`

The X3-A contracts received only additive fields: the explicit X3-B baseline strategy values, `notRunCount`/execution order, bounded retrieval-by-K values, exact fact coverage, call counts, and execution identity metadata. No production AgentLoop, retrieval, Provider, Critic, or Evidence Guard behavior was changed.

## 3. Runner Architecture

```text
EvaluationDataset
        ↓
Run preflight + config fingerprint + Git identity
        ↓
Prepared EvaluationSourceArtifact
        ↓
CURRENT_PRODUCTION / FIXED_BALANCED strategy
        ↓
AgentLoopEvaluationAdapter
        ↓
Injected production AgentLoop boundary
        ↓
existing context/retrieval → Planner → Executor → Critic → Evidence Guard
        ↓
bounded EvaluationExecutionObservation
        ↓
deterministic metrics / usage / latency / failure mapping
        ↓
results.jsonl + summary.json
```

The adapter is provider-neutral and evaluation-only. It receives the runtime projection returned by `EvaluationCase.execution_input()` and never receives gold annotations.

## 4. Evaluation Execution Boundary

`EvaluationRunner` performs one fresh adapter call for each selected case × strategy × trial. The adapter can wrap the normal `AgentLoopService` or a controlled fake. It is not a normal user API/Celery dispatch path and does not use X2 historical replay.

Prepared `VideoContext`, chunks, vectors, and provenance are represented by `EvaluationSourceArtifact`. Ingestion, FFmpeg, ASR, and OCR are outside the measured benchmark boundary.

## 5. Prepared Media / Source Preflight

Each attempt checks artifact availability, media identity, the X3 evaluation contract version, and exact `source_revision` equality before the adapter is called. Missing or incompatible artifacts become typed `EXCLUDED` results; a revision mismatch is `SOURCE_REVISION_MISMATCH` and produces zero adapter/provider calls.

## 6. Dataset Incomplete Handling

`DATASET_INCOMPLETE` datasets remain runnable. The summary carries both `DATASET_INCOMPLETE` and `OCR provenance coverage incomplete` caveats and does not upgrade dataset classification or completeness.

The current X3-A dataset remains illustrative/incomplete and is not presented as a formal benchmark.

## 7. Run / Strategy / Trial Identity

The supported X3-B baseline is `CURRENT_PRODUCTION`; `FIXED_BALANCED` and the legacy `ALWAYS_BALANCED` value are also accepted as bounded fixed-baseline aliases. Routing-matrix values such as `JEV_ROUTER`, `RULE_ROUTER`, `ALWAYS_FAST`, and `ALWAYS_DEEP` are rejected by X3-B preflight.

Every result records `runId`, `caseId`, `strategy`, `trialIndex`, and execution order. The writer protects the `(run, case, strategy, trial)` identity; the X3-A logical result identity remains `(case, strategy, trial)` within one result set.

## 8. Runner Configuration

`EvaluationRunnerConfig` is strict and versioned. It includes:

- strategy and bounded trial count;
- case filter;
- cold/warm marker;
- positive bounded timeout;
- tool and Critic settings;
- optional pricing version and artifact output;
- retrieval K values, defaulting to `1, 3, 5`;
- an optional bounded `maxExecutions` control for deterministic partial-run recovery tests.

All numeric limits are validated before execution. The normalized config is serialized canonically and hashed into `configFingerprint`.

## 9. Git / Working Tree Identity

The workspace has no usable Git repository identity. The run records `gitSha = null` and `workingTreeState = UNKNOWN`; `publishable` is therefore `false`. Local development runs are allowed. Publish mode would fail preflight without a commit identity.

## 10. Config Fingerprint

`EvaluationRunnerConfig.fingerprint()` uses canonical JSON and SHA-256. Equivalent normalized configurations produce the same fingerprint; field ordering does not affect it.

## 11. Fresh Execution Semantics

The runner does not copy a final result, Planner output, Executor output, Critic result, route decision, or provider response between strategy/trial attempts. Immutable prepared source artifacts may be shared. Every attempt passes through the adapter boundary independently.

## 12. Latency Measurement

The runner owns its own `time.monotonic()` span from case execution start to final result or terminal failure. It excludes dataset loading and artifact serialization. Adapter-supplied routing, retrieval, Planner, Executor, Critic, and tool spans are retained only when explicitly measured; unavailable stages remain `NOT_MEASURED`, never zero.

## 13. Token Usage Measurement

Provider-reported usage can be passed through the observation, including bounded per-role usage. A total-only observation records only `totalTokens`; input/output are not inferred. Missing usage remains `NOT_MEASURED`. Non-Jev baseline router usage is explicitly `NOT_APPLICABLE`.

## 14. Cost Measurement

`PricingCatalog` and `PricingEntry` are evaluation-only data contracts keyed by provider/model and pricing version. No vendor-specific `if` logic was added. Calculated cost is emitted only when measured input/output tokens and a matching catalog entry exist. Provider-reported cost and calculated cost remain separate. Missing pricing does not block quality metrics and leaves calculated cost `NOT_MEASURED`.

## 15. Retrieval Metrics

`calculate_retrieval_metrics()` implements provenance-backed Recall@K, Precision@K, and MRR for configured K values. Matching respects `SOURCE_ITEM`, `SEGMENT`, and `TEMPORAL_REGION` semantics. Temporal output reports hit and overlap coverage. Every match requires source-revision equality; equal IDs from a different revision are not hits.

## 16. Evidence Metrics

Final `AnalysisResult.evidence` is projected into a bounded provenance shape and compared with expected evidence independently from Evidence Guard. Evidence precision/recall therefore do not equate Guard PASS with gold correctness. Evidence support rate is supported final evidence divided by total final evidence and remains unmeasured when there is no reliable denominator.

## 17. Required Fact Metrics

`requiredFactExactCoverage` uses exact or normalized acceptable-variant substring matching only. Optional facts are not placed in the required denominator. This is explicitly not semantic fact correctness; human or judge evaluation remains outside X3-B.

## 18. Structural Metrics

The runner reuses existing `is_result_valid()` and mode-profile section validation for `schemaValid` and `modeSectionsValid`. `evidenceGuardPass` is accepted only from the execution observation/normal production boundary and is not synthesized from gold evidence.

## 19. Routing / Tool / Critic Metrics

- Route measurements are passed through from the adapter. Fixed baseline strategies record routing as `NOT_APPLICABLE` when no route is used.
- X1 tool counters are accepted through `ToolMetrics`. Tools disabled is `NOT_APPLICABLE`; no tool is registered or added by X3-B.
- Critic metrics include first/final pass, additional rounds, and bounded Critic call count. Planner and Executor call counts are also available on `EvaluationCaseResult`.

No Jev call, routing comparison, or quality conclusion is produced.

## 20. Failure Mapping

Every adapter/provider/output failure produces a JSONL `EvaluationCaseResult(status=FAILED)` with a bounded X3-A failure category. Unknown exceptions map to `INSTRUMENTATION_FAILURE`. Results contain no raw exception message or traceback.

## 21. Exclusion Semantics

`EXCLUDED` is reserved for pre-execution artifact, source-revision, contract, or evaluation-infrastructure incompatibility. A provider error, timeout, invalid model output, or Evidence Guard failure remains a scored/recorded failed execution rather than disappearing from the denominator.

## 22. JSONL Result Persistence

`EvaluationArtifactWriter` writes each completed result immediately to `results.jsonl`, flushes the record, and separately writes `run.json` and machine-authoritative `summary.json`. A partial process boundary therefore retains completed JSONL rows.

## 23. Result / Dataset Digest

The runner uses the X3-A canonical result digest and reparses a result before writing so an unvalidated `model_copy()` cannot bypass digest validation. Dataset and result serialization remain deterministic JSON/JSONL.

## 24. Partial Run / Duplicate Safety

`maxExecutions` provides a bounded local partial-run test path: planned, executed, terminal, and `notRun` counts remain explicit. The writer treats an identical `(run, case, strategy, trial)` digest as idempotent and rejects a conflicting digest. No completed JSONL row is silently overwritten.

## 25. Summary Aggregation

`summary.json` is the machine-readable authority. It contains terminal counts, dataset metadata, case coverage, per-metric aggregates, and caveats. No global quality score is defined. A controlled partial run is reported as `PARTIAL` rather than being presented as a complete benchmark.

## 26. Metric Denominators

Only values with `measurementState = MEASURED` enter metric aggregates. Each aggregate stores `numerator`, `denominator`, and `value`; `NOT_MEASURED` and `NOT_APPLICABLE` are not converted to zero. Coverage distributions use the selected-case denominator and retain `notRunCount` separately.

## 27. Leakage / Privacy Boundary

Execution receives only `media_ref`, `query`, and `mode`. JSONL/summary artifacts do not contain query text, required facts, expected evidence, reference answer, transcript/OCR bodies, raw ToolResult text, provider output, SQL, credentials, API keys, or tracebacks. Gold is available only through the post-execution judge projection contract, which X3-B does not invoke.

## 28. Synthetic Test Strategy

The deterministic adapter tests use synthetic prepared artifacts and are kept outside the X3-A golden dataset. They exercise runner correctness only and are classified `SYNTHETIC`.

## 29. Tests Added

`tests/application/test_x3b_evaluation_runner.py` covers successful execution, failure persistence, partial/not-run counts, source mismatch exclusion, measurement states, pricing, K metrics, temporal no/partial/full overlap, revision mismatch, stable fingerprints, duplicate identities, incomplete dataset caveat, missing Git identity, privacy, and the AgentLoop adapter boundary.

## 30. Focused Test Result

```text
27 passed / 0 failed / 0 skipped
```

This includes the X3-A contract regression and 16 X3-B runner tests.

## 31. Full First-Party Test Result

```text
732 passed / 0 failed / 0 skipped
```

The existing 715-test baseline remains green; X3-B adds 17 passing tests.

## 32. Build / Smoke Result

- Vue tests: `2 passed`.
- Vue production build: passed.
- `D:\python\python.exe -m compileall -q src tests`: passed.
- Public import smoke: `PUBLIC_IMPORT_SMOKE=PASS`.
- Existing two dependency deprecation warnings remain unchanged.

## 33. Live Provider / Benchmark Status

No live Provider, paid call, Jev call, Docker service, Celery worker, canonical media pipeline, or formal benchmark was run. X3-B only verifies the offline runner and metric infrastructure.

## 34. Dataset Status

The current dataset is the existing 16-case X3-A illustrative dataset with one real source revision and `DATASET_INCOMPLETE` status. X3-B preserves its digest and does not fabricate OCR annotations or upgrade completeness.

## 35. Deferred X3-C

No formal FAST/BALANCED/DEEP/Rule/Jev strategy matrix, routing comparison, superiority claim, or routing quality conclusion was added.

## 36. Deferred X3-D / X3-E

No LLM judge, human-quality automation, ablation framework, benchmark publishing workflow, or evaluation UI was added.

## 37. Risks / Remaining Gaps

- The dataset remains incomplete and is not publishable as a representative quality benchmark.
- Fine-grained stage and per-role usage metrics are populated only when the injected production adapter provides reliable bounded instrumentation; the runner reports `NOT_MEASURED` otherwise.
- The local workspace has no Git commit identity, so local runs are intentionally non-publishable.
- No live Provider result is claimed by this ticket.

These are explicit X3-B boundaries, not silently fabricated measurements.

## 38. Final Classification

```text
X3_B_PASS
```
