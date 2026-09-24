# DOVideo J1-B EXECUTION REPORT

## 1. Status

PASS. J1-B is implemented and verified offline. No live Jev or paid Provider call was made.

## 2. Files Changed

- `src/dovideo/infrastructure/providers/jev.py`
- `src/dovideo/infrastructure/model_routing.py`
- `src/dovideo/infrastructure/r4_runtime.py`
- `src/dovideo/infrastructure/__init__.py`
- `src/dovideo/infrastructure/providers/__init__.py`
- `src/dovideo/application/model_routing.py`
- `src/dovideo/application/checkpoint_service.py`
- `src/dovideo/application/ports/checkpoint.py`
- `src/dovideo/application/ports/__init__.py`
- `src/dovideo/application/__init__.py`
- `.env.example`
- `tests/infrastructure/test_jev_model_routing_j1b.py`

No X2 execution-record schema or event was changed.

## 3. Jev Adapter Architecture

`JevModelRouter` implements the provider-neutral `ModelRouterPort`. It projects `TaskRoutingContext` into one bounded System One request, parses one `model_lane` ChoiceAnswer, and returns `RoutingSuggestion`.

Jev has no access to DOVideo provider clients, downstream model profiles, ToolPolicy, AgentLoop, Planner, Executor, Critic, or X2 persistence.

## 4. Official API Contract Used

The adapter uses:

- `POST /v1/systemone`
- `Authorization: Bearer <server-side configured key>`
- request fields `model`, `state`, and exactly one `questions.model_lane` choice question
- response fields `answers.model_lane.type`, `choice`, and `confidence`

The response `model` and `usage.input_tokens/output_tokens` fields are optional bounded diagnostics only.

## 5. HTTP / Dependency Strategy

The existing dependency-free asynchronous JSON HTTP boundary is reused: `AsyncJsonPostClient`, `post_json`, and `response_parts`. No SDK or dependency upgrade was added.

## 6. Jev Configuration

Supported server-side settings are:

- `DOVIDEO_JEV_ENDPOINT`, defaulting to `https://api.typesafe.ai/v1/systemone`
- `DOVIDEO_JEV_MODEL`
- `DOVIDEO_JEV_API_KEY`
- `DOVIDEO_JEV_TIMEOUT_SECONDS`, default `2.0`, bounded to `30.0`
- `DOVIDEO_JEV_MAX_ATTEMPTS`, default `1`, bounded to `2`
- `DOVIDEO_JEV_RETRY_DELAY_SECONDS`, bounded to `2.0`

The API key is secret-bearing infrastructure state with `repr=False`; it is never placed in routing DTOs, checkpoints, trace diagnostics, exceptions, or API responses.

## 7. Feature Flag

`DOVIDEO_MODEL_ROUTING_ENABLED` was added. Its default is `false`.

Disabled mode does not construct or call `JevModelRouter`, and the existing BALANCED/final-only production path remains the active path.

## 8. Jev Choice Request

The request exposes only:

- `FAST`
- `BALANCED`
- `DEEP`

The bounded state projection contains only user goal, concrete mode, duration, segment/chunk counts, and ASR/OCR availability. It does not contain `media_id`, provider configuration, transcript/OCR bodies, VideoContext, vectors, ToolResults, prompts, or credentials.

## 9. Response Parsing

Parsing is strict:

- `answers.model_lane` must exist
- `type` must equal `choice`
- `choice` must be one of the three lanes
- `confidence` must be finite and within `[0, 1]`

Missing answers, malformed JSON/envelopes, wrong answer types, unknown choices, and invalid confidence become `INVALID_SUGGESTION` fallback.

## 10. Confidence Mapping

Official Jev `ChoiceAnswer.confidence` maps directly to `RoutingSuggestion.confidence`.

`probabilities` are not used to override the selected choice or to invent a second-place decision.

## 11. Failure / Fallback Semantics

The application policy remains authoritative:

- timeout, network failure, 429, or 5xx → `ROUTER_UNAVAILABLE` → BALANCED
- 401/403 or other rejected HTTP response → `ROUTER_ERROR` → BALANCED
- malformed/invalid response → `INVALID_SUGGESTION` → BALANCED
- low confidence → `LOW_CONFIDENCE` → BALANCED
- missing/disabled FAST or DEEP profile → `LANE_DISABLED` → BALANCED

These runtime routing failures do not fail the analysis task.

## 12. Timeout / Retry Policy

The default Jev timeout is short and bounded at two seconds. The default retry count is zero retries (`max_attempts=1`); configuration permits at most one bounded retry. No unbounded retry or expensive generation timeout is inherited.

## 13. Real Model Profile Mapping

Infrastructure-owned settings map:

- `FAST` → `DOVIDEO_FAST_MODEL`
- `BALANCED` → `DOVIDEO_BALANCED_MODEL`, defaulting to the existing `DOVIDEO_MODEL`
- `DEEP` → `DOVIDEO_DEEP_MODEL`

Each configured lane receives an `OpenAICompatibleModelAdapter` backed by its own configured `ProviderConfig`. Provider/model identity never enters `RoutingSuggestion`, `ModelRoutingDecision`, `TaskRoutingContext`, or X2 records.

## 14. BALANCED Backward Compatibility

When routing is disabled, `create_r4_provider_stack` uses the original provider configuration and the original single `AgentLoopService`. No Jev call, routing checkpoint, lane-specific client, or routing wrapper is introduced on that path.

The disabled path therefore preserves the pre-J1-B Planner/Executor/Critic model configuration and behavior.

## 15. FAST Production Path

When enabled and configured, FAST uses the infrastructure-owned FAST provider profile for Planner, Executor, Executor continuations, and Critic through one lane-specific AgentLoop.

If FAST is absent or explicitly disabled, policy marks it unavailable and falls back to BALANCED before provider execution.

## 16. DEEP Production Path

When enabled and configured, DEEP uses the infrastructure-owned DEEP provider profile for Planner, Executor, Executor continuations, and Critic through one lane-specific AgentLoop.

If DEEP is absent or explicitly disabled, policy marks it unavailable and falls back to BALANCED before provider execution.

## 17. One-Route-Per-Execution

`ProductionModelRoutingAgentLoop` routes after concrete mode/profile resolution and before delegating to the first Planner call. It selects one lane-specific AgentLoop and reuses that lane for the complete execution.

There is no per-stage routing and no Critic-triggered escalation.

## 18. Crash / Recovery Routing Stability

J1-B adds a separate `modelRouting` checkpoint namespace through the existing `AgentCheckpointService`. The immutable `ModelRoutingDecision` is saved before the lane AgentLoop starts.

On recomposition/retry, an existing decision is reused without calling Jev. This temporary recovery fact is not an X2 execution event and will be promoted to historical route recording only by J1-C.

If an existing FAST/DEEP decision cannot be executed after a rollback because its profile is gone, the wrapper fails closed with `RoutingProfileUnavailableError`; it does not silently resample or switch lanes.

## 19. AgentLoop / Production Wiring

Enabled composition is:

```text
Concrete Mode
  → TaskRoutingContext
  → JevModelRouter
  → ModelRoutingPolicy
  → ModelRoutingDecision
  → infrastructure lane profile
  → lane AgentLoopService
  → Planner / Executor / Critic
```

All lane AgentLoops reuse the existing LongVideoContextService, checkpoint, event publisher, budget, ToolPolicy, read-only tools, execution record service, and Evidence Guard boundaries.

## 20. Security Boundary

Jev can return only a logical lane. It cannot choose a provider, arbitrary model, endpoint, credential, tool, media, user, task, or execution mode.

FAST/BALANCED/DEEP share identical ownership, ModeProfile, ToolPolicy, X1 recovery, X2 recording, rate-limit, and Evidence Guard semantics.

## 21. Routing Privacy / Secret Safety

Tests verify that bounded Jev state excludes transcript/OCR bodies and that routing DTOs do not expose model or credential fields. The API key exists only in the server-side HTTP authorization header.

Trace diagnostics contain only bounded lane/reason/confidence, status, latency, optional router model identity, and bounded usage counts. Raw request/response bodies, headers, prompts, goals beyond the bounded state, credentials, and source content are not recorded.

## 22. Operational Observability

The existing `R4AgentTelemetry` / Redis trace path receives additive structural diagnostics only:

```text
modelRoutingEnabled
modelRouteLane
modelRouteFallback
modelRouteReason
modelRouteConfidence
```

Jev transport diagnostics are bounded and do not create an X2 historical event.

## 23. Tests Added

`tests/infrastructure/test_jev_model_routing_j1b.py` adds 23 offline tests covering:

- official request shape and bearer boundary
- FAST/BALANCED/DEEP ChoiceAnswer mapping
- strict invalid response handling
- timeout, 401/403, 429, and 5xx fallback
- probability non-authority
- feature/config defaults and startup validation
- missing FAST/DEEP lane disablement
- bounded routing context privacy
- production route-once behavior
- durable decision reuse after recomposition
- disabled mode with zero Jev calls
- low-confidence fallback
- removed-profile fail-closed behavior
- infrastructure-only provider profile mapping
- R4 enabled three-profile composition without network

## 24. Focused Test Results

```text
23 J1-B tests passed
151 affected application/provider/checkpoint/worker tests passed
```

No test required live Jev, a provider key, Docker, RabbitMQ, Redis, MySQL, Qdrant, or media processing.

## 25. Full First-Party Test Result

```text
694 passed / 0 failed / 0 skipped
```

This is the previous `671 passed` baseline plus the 23 J1-B tests. Two pre-existing dependency deprecation warnings remain.

## 26. Build / Smoke Results

```text
python -m compileall -q src tests       PASS
PUBLIC_IMPORT_SMOKE                    PASS
Vue npm test                            2 passed
Vue npm run build                       PASS
```

## 27. Live Jev Smoke Status

Not run. It was not required, and no live credentials or paid calls were used.

## 28. Production Behavior / Rollback

Default and rollback behavior is `DOVIDEO_MODEL_ROUTING_ENABLED=false`: no Jev call and the existing BALANCED model path.

When enabled, configuration errors for missing Jev key/model, invalid endpoint, invalid timeout/attempt bounds, invalid threshold, or unresolved BALANCED profile fail during composition. Runtime Jev failures fall back to BALANCED.

## 29. Deferred To J1-C

The following remain intentionally unimplemented:

- `MODEL_ROUTE_RECORDED`
- X2 execution event/schema changes
- durable historical route event
- replay route reconstruction
- route historical integrity checking

The temporary recovery checkpoint exists only to prevent rerouting before J1-C.

## 30. Explicit X3 Boundary

No quality, cost, latency, or model-superiority claim is made. J1-B proves safe routing architecture and fallback behavior only. No benchmark, evaluation, ablation, or cost framework was added.

## 31. Risks / Open Questions

- J1-C must convert the temporary stable route fact into a durable historical execution fact without changing J1-A policy semantics.
- FAST and DEEP operational usefulness remains unmeasured until X3.
- Live Jev availability and production quota behavior were not exercised by this offline acceptance run.

## 32. Final Classification

```text
J1_B_PASS
```
