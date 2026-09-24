# DOVideo J1-C EXECUTION REPORT

## 1. Status

PASS. J1-C durable model-route history and X2 replay integration are implemented and verified offline. No live Jev or paid Provider call was made.

## 2. Files Changed

- `src/dovideo/application/model_routing.py`
- `src/dovideo/application/execution_record.py`
- `src/dovideo/application/historical_replay.py`
- `src/dovideo/application/replay_access.py`
- `src/dovideo/application/__init__.py`
- `src/dovideo/infrastructure/model_routing.py`
- `src/dovideo/infrastructure/r4_runtime.py`
- `src/dovideo/infrastructure/__init__.py`
- `src/dovideo/presentation/api/r4_runtime.py`
- `tests/application/test_j1c_model_route_history.py`

No retrieval, checkpoint architecture, Jev request behavior, Provider behavior, X3 evaluation, or canonical media pipeline was redesigned.

## 3. Historical Route Event Contract

Added the X2 semantic event:

```text
MODEL_ROUTE_RECORDED
```

The event is emitted once per route-aware execution and represents the final policy-owned model route actually selected for the Agent execution.

## 4. Execution Contract Versioning

The new route-aware contract is:

```text
agent-execution-semantics-v2
```

The generic `x2-b-record-v1` record schema remains unchanged because the existing ordered event envelope already supports this additive semantic event. Existing `agent-execution-semantics-v1` records remain replayable.

For compatibility, direct legacy `ExecutionRecordService` callers retain a v1 default unless they explicitly opt into v2. R4 production composition explicitly constructs v2 execution recording, including when adaptive routing is disabled.

## 5. Route Event Payload

The bounded payload contains only:

```text
lane
confidence
fallbackUsed
reasonCode
suggestedLane
routingContractVersion
profileId
resolvedModelId
```

`kind` is a bounded event discriminator. No route probabilities, raw router state, raw Jev request/response, prompt, transcript/OCR body, endpoint, header, credential, or user secret is stored.

## 6. Resolved Historical Model Identity

`resolvedModelId` is taken from the infrastructure-owned DOVideo model profile configuration. Jev can propose only `FAST`, `BALANCED`, or `DEEP`; it cannot supply or override the resolved provider model identity.

The route event also stores the static logical `profileId`, allowing replay to distinguish a historical profile from a later configuration mapping.

## 7. Durable Ordering Before Planner

R4 route-aware execution now follows:

```text
route decision
  → operational modelRouting checkpoint
  → EXECUTION_STARTED
  → MODEL_ROUTE_RECORDED
  → retrieval / PLAN_RECORDED
  → Planner provider call
```

The route event is durable before the delegated AgentLoop can make its first Planner call. A persistence error stops execution before the Provider path.

## 8. modelRouting Checkpoint Role

The existing `modelRouting` checkpoint remains operational crash-recovery state. It is not replay authority and does not replace the semantic event.

The durable execution record is the historical source of truth. Redis and checkpoint projections are not used by X2 replay.

## 9. Recovery Precedence

Recovery precedence is:

```text
MODEL_ROUTE_RECORDED
  → modelRouting checkpoint
  → fresh Jev route
```

If the event exists, the exact recorded decision is reused and the routing policy is not re-evaluated. If only the checkpoint exists, it is reused and the route event is appended without calling Jev. A fresh route is allowed only when neither durable route fact exists.

## 10. Crash Window Handling

- Route produced before checkpoint: no durable route fact exists; a later attempt may route again.
- Checkpoint durable before event: checkpoint is reused, Jev is not called, and the event is appended.
- Event durable before Planner: the event is reused; Jev is not called.
- Event present while checkpoint is missing: the historical event is authoritative; the operational checkpoint may be safely rebuilt.

Removed lanes, missing profiles, current model mapping drift, malformed route events, and persistence failures fail closed. They never silently switch to `BALANCED` and never resample Jev.

## 11. Event / Checkpoint Integrity

All authority-relevant decision fields are compared:

```text
lane
confidence
fallbackUsed
reasonCode
suggestedLane
routingContractVersion
```

A checkpoint/event conflict raises a typed fail-closed error before a Provider lane runs. A v2 record with a Planner-side event but no route event is incomplete.

## 12. Route Event Idempotency

The canonical logical identity is:

```text
model.route
```

The existing durable repository idempotency boundary is reused. Re-appending the same logical event and payload succeeds without a second event. A conflicting payload or a second route-event identity fails closed. Replay rejects any durable record that actually contains multiple route events.

## 13. X2 Historical Replay Integration

`HistoricalAgentReplayService` now accepts both execution contract v1 and v2. It parses `MODEL_ROUTE_RECORDED` into the additive `historical_model_route` projection and never constructs or calls:

```text
Jev
ModelRouterPort
ModelRoutingPolicy
ModelProfileResolver
Provider adapters
```

The replay path remains read-only and providerless.

## 14. V1 Replay Compatibility

For `agent-execution-semantics-v1` records, `historical_model_route` is `null`. The absence of a route fact is treated as historical unavailability, not corruption.

The existing X2-B/X2-C/X2-D replay tests continue to pass.

## 15. V2 Replay Semantics

For v2 records, replay requires exactly one route event. It rejects:

- zero route events;
- more than one route event;
- a route event after the first plan event;
- an unsupported model-routing contract;
- a profile identity inconsistent with its lane;
- malformed or non-bounded route payloads.

Replay returns the recorded lane, confidence, fallback flag, reason, suggested lane, logical profile, and resolved model identity.

## 16. Threshold / Configuration Drift Behavior

Replay consumes the recorded policy decision. It does not apply the current confidence threshold, so historical `FAST`/`DEEP` decisions remain unchanged when the current threshold changes.

## 17. Model Mapping Drift Behavior

Replay reports the historical `resolvedModelId` from the event. It does not resolve the current `FAST`/`BALANCED`/`DEEP` mapping and therefore cannot rewrite a historical model identity.

Live recovery validates that the currently available lane/profile still matches the durable historical identity; mapping drift or removal fails closed.

## 18. X2-D API Projection

The existing authenticated replay endpoint is unchanged. Its additive response projection now includes:

```text
historicalModelRoute
```

The existing execution metadata response also exposes bounded `routeRecorded` and `modelRouteLane` metadata. Ownership and operator authorization paths are unchanged.

## 19. Redis Observability

The existing R4 telemetry path remains the operational projection and retains the J1-B bounded routing diagnostics:

```text
modelRoutingEnabled
modelRouteLane
modelRouteFallback
modelRouteReason
modelRouteConfidence
```

The durable X2 event is the historical authority. Redis outages cannot change route identity, replay behavior, or Provider execution semantics.

## 20. Privacy / Secret Boundary

Route history and replay projections contain no credential material, raw router body, raw Provider body, prompt, transcript/OCR content, full routing state, or unrestricted user input. Only bounded route facts and application-resolved model identity are persisted/projected.

No credential value was printed, stored in this report, or used by the offline verification.

## 21. Tests Added

`tests/application/test_j1c_model_route_history.py` adds 11 offline tests covering:

- accepted route event ordering and real AgentLoop Planner ordering;
- route-event recovery without checkpoint and without Jev;
- checkpoint/event conflict;
- disabled routing with `ROUTING_DISABLED` historical fallback;
- removed-profile fail-closed recovery;
- route event idempotency and conflicting duplicate payload;
- v2 replay projection and providerless behavior;
- v2 missing-event incompleteness;
- v1 replay compatibility;
- route-after-plan and multiple-route integrity failures;
- disabled R4 production composition wiring.

## 22. Focused Test Results

```text
J1-C focused                         11 passed
Affected application/infrastructure  632 passed
```

## 23. Full First-Party Test Result

Exactly one final full pytest run was executed after implementation freeze:

```text
705 passed / 0 failed / 0 skipped
```

The total is the previous 694-test baseline plus 11 J1-C tests. Two existing dependency deprecation warnings remain.

## 24. Build / Smoke Results

```text
python -m compileall -q src tests    PASS
PUBLIC_IMPORT_SMOKE                  PASS
Vue npm test                          2 passed
Vue npm run build                     PASS
```

No Docker, database, broker, Qdrant, media, or live model service was required for J1-C verification.

## 25. Live Jev Status

Not run. No live Jev call, paid Provider call, or credential-bearing request was made.

## 26. Final J1 Architecture

```text
Concrete Mode
     ↓
Jev Router (optional, proposal only)
     ↓
RoutingSuggestion
     ↓
Deterministic ModelRoutingPolicy
     ↓
ModelRoutingDecision
     ↓
Operational modelRouting checkpoint
     ↓
Durable MODEL_ROUTE_RECORDED (v2)
     ↓
Resolved FAST / BALANCED / DEEP model profile
     ↓
Planner / Executor / Critic
     ↓
Durable X2 execution history
     ↓
Providerless historical replay
     ↓
Recorded historical model route
```

Routing is once per Agent execution. There is no per-stage routing.

## 27. Explicit X3 Boundary

J1-C does not evaluate routing usefulness. No routing accuracy, quality score, cost-savings claim, latency comparison, lane success rate, rule router, golden dataset, ablation, or evaluation framework was added.

Jev remains limited to model routing. It is not used for retrieval, Evidence Guard, Critic remediation, ToolPolicy, replay, or AUTO mode.

## 28. Risks / Remaining Non-Blockers

- FAST and DEEP production usefulness remains unmeasured and belongs to X3.
- Live Jev availability/quota behavior was intentionally not exercised.
- Direct non-production execution-record callers retain the v1 default for backward compatibility; R4 production is explicitly v2 route-aware.

## 29. Final Classification

```text
J1_C_PASS
```

J1 is closed. Do not start X3 automatically.
