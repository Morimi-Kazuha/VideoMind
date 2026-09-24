# DOVideo J1-A EXECUTION REPORT

## 1. Status

PASS. J1-A routing contracts and deterministic application policy are implemented offline.

## 2. Files Changed

- `src/dovideo/application/model_routing.py`
- `src/dovideo/application/__init__.py`
- `tests/application/test_model_routing_j1a.py`
- `J1_A_EXECUTION_REPORT.md`

No production composition, Provider adapter, AgentLoop, X2 record, or frontend code was changed.

## 3. Routing Architecture

```text
TaskRoutingContext
        ↓
ModelRouterPort
        ↓
RoutingSuggestion
        ↓
ModelRoutingPolicy
        ↓
ModelRoutingDecision
        ↓
ModelProfileRegistry
        ↓
logical ModelProfile
```

The router proposes; the application policy decides; the static registry resolves the logical profile.

## 4. Routing Granularity

`ModelRoutingService.route_once()` models one route per Agent execution. The immutable decision can be supplied again on recovery without calling the router a second time. Per-stage, per-round, Critic escalation, and tool-specific routing are not implemented.

## 5. Model Route Lanes

The fixed V1 allowlist is:

```text
FAST / BALANCED / DEEP
```

No dynamic lane or commercial model name is part of the contract.

## 6. TaskRoutingContext

The bounded context contains trusted `TaskKey`, concrete mode, bounded goal, optional duration, segment/chunk counts, ASR/OCR availability, source revision, and provenance version. It contains no VideoContext body, transcript, OCR dump, vectors, ToolResult, Redis detail, database path, worker identity, or credential.

## 7. ModelRouterPort

`ModelRouterPort.route(context)` is provider-neutral and accepts a synchronous or asynchronous adapter result. J1-A supplies fakes in tests; no Jev client or network dependency exists.

## 8. RoutingSuggestion Contract

`RoutingSuggestion` is strict and immutable at the model boundary with `suggestedLane`, `confidence`, `routingContractVersion`, and bounded optional classification metadata. Extra fields are rejected; provider/model/endpoint/key/temperature metadata is rejected.

## 9. Confidence Semantics

Confidence must be finite and satisfy `0.0 <= confidence <= 1.0`. NaN, infinity, missing values, booleans, and out-of-range values are invalid. The default policy threshold is `0.70` and is application-controlled.

## 10. Deterministic Routing Policy

`ModelRoutingPolicy` owns the final decision. The result depends only on trusted context, the validated suggestion, and immutable configuration; it uses no clock, randomness, or unordered decision process.

## 11. BALANCED Fallback

The following all select `BALANCED` with `fallbackUsed=true`:

- routing disabled
- router unavailable
- router timeout/error
- invalid suggestion
- unsupported suggestion contract
- low confidence
- disabled suggested lane

The valid original suggestion confidence is retained on fallback where available.

## 12. Reason Codes

Typed bounded reason codes include `ROUTER_ACCEPTED`, `LOW_CONFIDENCE`, `ROUTER_UNAVAILABLE`, `ROUTER_ERROR`, `INVALID_SUGGESTION`, `MODE_NOT_ALLOWED`, `LANE_DISABLED`, and `ROUTING_DISABLED`.

## 13. ModelRoutingDecision

The decision contains `lane`, `confidence`, `fallbackUsed`, `reasonCode`, `routingContractVersion`, and optional `suggestedLane`. Fallback decisions are structurally constrained to `BALANCED` and are stable JSON-serializable DTOs.

## 14. ModelProfile Registry

`ModelProfileRegistry` is a static explicit mapping:

```text
FAST      → fast-profile
BALANCED  → balanced-profile
DEEP      → deep-profile
```

`ModelProfile` contains only logical `profileId` and lane. Provider, model, endpoint, and credential fields do not exist. Runtime registration is rejected.

## 15. Disabled-Lane Behavior

FAST and DEEP can be independently disabled by application configuration. A suggestion for a disabled lane deterministically falls back to BALANCED. BALANCED is always available.

## 16. AUTO / Mode Boundary

J1-A accepts only concrete `GENERAL`, `LEARNING`, `REVIEW`, and `CREATION` modes. `AUTO` is rejected before policy evaluation. P2 remains the sole AUTO→concrete-mode boundary.

## 17. Security Boundary

Routing affects only performance/cost profile selection. It cannot supply or change TaskKey, user/media identity, mode authority, tool permissions, Evidence Guard rules, rate limits, ownership, or replay authorization. All security boundaries remain independent of lane.

## 18. Recovery Semantics

J1-A exposes immutable decision reuse through `route_once(existing_decision=...)`. Recovery does not call the router again and does not silently select a new lane. Durable X2 decision recording remains deferred to J1-C.

## 19. Contract Versioning

The application contract version is fixed at:

```text
model-routing-v1
```

It is distinct from any future router/Jev model version.

## 20. Serialization / Bounds

Contracts use strict extra-field rejection, bounded text/metadata/counts, finite numeric validation, concrete-mode validation, and JSON aliases. Context serialization contains identity and routing signals only, not video content.

## 21. Tests Added

`tests/application/test_model_routing_j1a.py` adds 23 tests covering lane contracts, AUTO rejection, confidence boundaries, accepted routes, fallback reasons, router failures, invalid output, disabled routing, route-once recovery, static profiles, context bounds, and unsupported decision versions.

## 22. Focused Test Results

```text
23 passed
```

Compileall and public import smoke also passed.

## 23. Full First-Party Test Result

```text
671 passed / 0 failed / 0 skipped
```

This preserves the previous 648-test baseline and adds the J1-A tests. The existing two dependency deprecation warnings remain.

## 24. Production Behavior Status

Production behavior is unchanged. No production routing flag was added, no current Planner/Executor/Critic provider selection changed, and no live router call is reachable from the production composition.

## 25. Deferred To J1-B

- Jev HTTP/client adapter
- Jev credentials or network calls
- production feature flag
- real provider/model mapping
- AgentLoop production routing integration

## 26. Deferred To J1-C

- `MODEL_ROUTE_RECORDED`
- X2 execution-record schema/event changes
- durable route replay semantics
- routing telemetry/observability

## 27. Explicit X3 Boundary

No routing benchmark, golden dataset, quality/cost/latency evaluation, accuracy measurement, ablation, or rule-vs-Jev comparison was added.

## 28. Risks / Open Questions

The default policy is disabled and therefore resolves BALANCED until J1-B defines controlled production configuration. Threshold and enabled-lane values are currently application contract inputs, not environment wiring. No live router quality claim is made.

## 29. Final Classification

```text
J1_A_PASS
```
