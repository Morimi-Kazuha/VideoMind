# X1-D2 EXECUTION REPORT

## 1. Status

PASS

X1-D2 production wiring, rollback safety, durable recovery, bounded
observability, and regression verification are complete.

## 2. Files Changed

- `src/dovideo/application/agent.py`
- `src/dovideo/application/__init__.py`
- `src/dovideo/application/video_tools.py`
- `src/dovideo/infrastructure/x1_config.py`
- `src/dovideo/infrastructure/__init__.py`
- `src/dovideo/infrastructure/r4_runtime.py`
- `src/dovideo/infrastructure/providers/model.py`
- `src/dovideo/presentation/api/r4_runtime.py`
- `.env.example`
- `tests/infrastructure/test_x1_d2_production_wiring.py`

The implementation is limited to X1-D2 composition, configuration,
observability, bounded result projection, rollback safety, and focused tests.
No dependency upgrade or unrelated refactor was made.

## 3. Production Configuration

The production rollout flag is:

```text
DOVIDEO_AGENT_TOOL_CALLING_ENABLED=false
```

The default is disabled. Optional production limits are:

```text
DOVIDEO_AGENT_TOOL_REQUEST_LIMIT_PER_ROUND
DOVIDEO_AGENT_TOOL_REQUEST_LIMIT_TOTAL
```

They are positive integers and cannot widen the existing X1-A/B bounds of
`4` per round and `12` total requests. The ToolResult hard ceiling remains the
existing frozen `MAX_TOOL_RESULT_PAYLOAD_BYTES = 65536`; it is not given a
second environment-controlled bound.

Invalid flag or limit values fail during production composition before the
tool-aware AgentLoop is constructed. The API factory validates before opening
R2 infrastructure, and the worker composition validates before provider and
worker construction.

## 4. Disabled-Mode Compatibility

With the flag disabled, production passes only the existing `ExecutorPort`
path. `AgentLoopService` does not evaluate `ToolPolicy`, execute a
`ToolExecutor`, request a continuation, or save a new tool ledger.

The existing checkpoint is retained only as a read-only rollback guard. If an
unfinished durable X1 ledger is found, execution fails closed with an explicit
configuration error instead of silently starting a fresh final-only run.

## 5. Enabled Production Composition

With the flag enabled, the common R4 composition root constructs and injects:

- the existing provider `ExecutorTurnPort` adapter;
- a static `ToolRegistry`;
- a `ToolPolicy` bound to that registry;
- the real `VideoReadOnlyToolExecutor`;
- the existing `AgentCheckpointService` as the D1 durable tool-state port;
- the configured per-round and total request limits.

The API `ProductionR4Services` and Celery `R4WorkerRuntime` both use this same
composition function, so they cannot silently diverge on the feature flag or
tool dependencies.

## 6. Production Tool Dependency Graph

```text
Production AgentLoop
        ↓
ExecutorTurnPort (provider adapter)
        ↓
deterministic ToolPolicy
        ↓
static ToolRegistry
        ↓
VideoReadOnlyToolExecutor
        ↓
existing LongVideoContextService / current VideoContext
```

The provider has no direct tool-execution capability. The provider can only
return `FINAL` or `TOOL_REQUEST`; application code owns policy, identity,
durability, execution, and continuation.

## 7. Production Tool Budget

The production AgentLoop receives explicit limits from
`X1ToolCallingSettings`:

- per round: `4` maximum by the existing X1 source of truth;
- total: `12` maximum by the existing X1 source of truth;
- serialized ToolResult: `65536` bytes maximum by the existing X1 contract.

The model cannot choose any of these limits. The concrete video executor also
applies the configured result ceiling before returning a typed result.

## 8. Same-Video / Trusted Identity Boundary

`ToolExecutionContext` is built only from the current authorized `TaskKey`,
current worker-supplied media ID, current concrete mode profile, and the
current or recovered `VideoContext`. The model request has no media ID, owner,
user ID, task key, role, or mode fields.

The video executor reads only the supplied current context. It does not load a
media ID from model arguments or resolve another user's/task's video.

## 9. Durable Ordering In Production

The enabled path uses the D1 ordering unchanged:

```text
REQUESTED durable
→ policy decision durable
→ AUTHORIZED or DENIED durable
→ EXECUTING durable
→ concrete read-only execution
→ RESULT_STORED durable
→ Executor continuation
→ draft durable
→ CONSUMED durable
```

If the durable request write fails, no tool execution occurs. If the result
write succeeds, continuation recovery reuses the stored typed result rather
than executing the tool again.

## 10. Production Recovery

The production composition passes the existing `AgentCheckpointService`, not a
runtime-only ledger. The focused composition tests cover:

- `RESULT_STORED` recovery with zero concrete tool calls;
- `EXECUTING` recovery with the same stable call identity and at-least-once
  physical read execution;
- request persistence failure before execution;
- continuation after a typed result;
- durable consumption after the Executor draft is saved.

## 11. Feature Rollback / In-Flight State Behavior

New tasks return to the legacy final-only path when the feature is disabled.
An existing unfinished durable state is never ignored: the AgentLoop raises
`ToolCallingDisabledWithInFlightStateError` before legacy Executor execution.
Terminal and draft checkpoint precedence remains authoritative as defined by
D1, so an already durable higher-stage checkpoint is not needlessly replayed.

## 12. Tool Telemetry

The existing telemetry abstraction and existing Redis trace counter document
are reused. The durable production path records bounded counters with these
semantics:

```text
toolRequests          one logical durable model TOOL_REQUEST
toolAllowed           one durable ALLOW decision
toolDenied            one durable DENY decision
toolExecuted          each physical concrete executor invocation
toolFailures          each stored typed FAILED result
toolResultTruncated   each stored TRUNCATED/truncated result
toolRecovered         one recovered incomplete logical tool turn
toolResultReused      one durable-result reuse
```

Logical request/authorization counters are not incremented again during
recovery. Physical execution is intentionally not exactly-once.

## 13. Telemetry Privacy

X1-D2 telemetry writes only bounded counter names and values. It does not
write raw arguments, query text, ToolResult payloads, transcript/OCR bodies,
prompts, provider output, credentials, or Provider Keys. Existing structured
provider diagnostics remain value-bounded and structural.

The durable tool ledger is a correctness checkpoint and is separate from the
Redis trace telemetry surface; it is not exposed through the trace API or SSE
payload.

## 14. Telemetry Failure Behavior

Tool counter writes are best-effort diagnostics. A telemetry/Redis exception
is swallowed by the X1 counter hook and cannot alter policy, tool identity,
durable ordering, deduplication, ToolResult content, or Evidence Guard
behavior. Checkpoint persistence remains a correctness dependency and still
fails closed according to D1.

## 15. Provider Prompt / Tool Description Boundary

The existing X1-B tool-aware prompt contract is used. It now explicitly
describes only:

- `video.search_evidence` with bounded query and result-limit arguments;
- `video.get_segment` with a timestamp argument;
- `video.get_context_window` with bounded timestamp/before/after arguments.

The prompt exposes no Python paths, database/Qdrant/Redis/RabbitMQ details,
filesystem paths, worker identity, credentials, or application internals.
ToolResult remains typed untrusted data, never a system instruction and never
verified evidence.

## 16. Evidence Guard Verification

The enabled production-like path still runs:

```text
ToolResult
→ Executor continuation
→ AnalysisResult
→ Critic
→ existing Evidence Guard
→ final AgentState
```

A focused test supplies a successful tool path followed by invalid evidence;
the existing Evidence Guard still rejects the final evidence. Successful tool
execution does not grant evidence authority.

## 17. Tests Added

Added `tests/infrastructure/test_x1_d2_production_wiring.py` with `14` tests
covering configuration, disabled mode, enabled FINAL, successful tools,
policy denial, persistence failure, result reuse, in-flight recovery,
rollback safety, telemetry failure/privacy, typed failure/truncation metrics,
Evidence Guard, and provider-free production composition.

## 18. Focused Test Results

The X1-A/B/C/D1 plus X1-D2 focused selection passed:

```text
89 passed
```

The affected AgentLoop/provider/checkpoint/R4 selection passed:

```text
144 passed
```

The affected Celery, presentation, P2/P3 selection passed:

```text
19 passed, 2 warnings
```

The warnings are the existing Starlette/httpx and anyio dependency warnings.

## 19. Production Composition Test

`test_production_stack_wires_x1_without_network_or_provider_call` constructs
the common R4 provider stack with fake provider configuration and fake
infrastructure boundaries. It verifies the real `AgentLoopService`, static
registry, policy, video executor, and durable checkpoint wiring without
calling a provider or starting an external service.

## 20. Full First-Party Test Result

Exactly one final full pytest run was executed after implementation freeze:

```text
611 passed / 0 failed / 0 skipped / 2 warnings
```

The previous `597`-test baseline remains green; the difference is the `14`
new X1-D2 tests.

Additional verification:

- Vue `npm test`: `2 passed`;
- Vue `npm run build`: passed;
- `python -m compileall -q src tests`: passed;
- public import smoke: passed.

## 21. Canonical / Live Execution Status

No canonical media pipeline was rerun. No FFmpeg composition, eSpeak NG,
Docker, Redis, MySQL, RabbitMQ, Qdrant, or live Provider execution was started
for X1-D2. R4 canonical evidence remains the prior accepted boundary.

## 22. Final X1 Architecture

```text
Authenticated Analysis Task
        ↓
Production AgentLoop
        ↓
Planner
        ↓
Tool-Aware Executor
        ↓
   ExecutorTurn
    ↙       ↘
 FINAL    TOOL_REQUEST
             ↓
        ToolPolicy
             ↓
      Durable Tool State
             ↓
        ToolRegistry
             ↓
 VideoReadOnlyToolExecutor
             ↓
        ToolResult
             ↓
      Durable Result
             ↓
 Executor Continuation
             ↓
       AnalysisResult
             ↓
           Critic
             ↓
 EvidenceVerificationService / Evidence Guard
             ↓
        Final Result
```

In disabled mode, the `Tool-Aware Executor` branch is replaced by the frozen
final-only `ExecutorPort` path and the durable ledger is not written.

## 23. Final X1 Security Properties

| Question | Answer |
| --- | --- |
| Can the LLM execute arbitrary code? | **NO** |
| Can the LLM choose arbitrary tool names? | **NO** |
| Can the LLM select another media/user/task? | **NO** |
| Can a ToolResult become system instruction? | **NO** |
| Can a ToolResult bypass Evidence Guard? | **NO** |
| Can the LLM call write/mutation tools? | **NO** |
| Are tool calls bounded? | **YES** |
| Are tool calls recoverable? | **YES** |
| Are read-only executions exactly once? | **NO** |

The guaranteed execution property is:

```text
at-least-once physical read execution before durable result
+
result-level deduplication after durable ToolResult
```

No stronger exactly-once claim is made.

## 24. Explicit X2/X3 Boundary

X1-D2 does not implement evidence IDs, segment/chunk provenance,
deterministic provider replay, decision-graph replay, tool replay graphs,
event sourcing, golden datasets, retrieval benchmarks, token/cost evaluation,
or ablation/evaluation frameworks. Those remain X2/X3 scope.

## 25. Risks / Remaining Non-Blockers

- The feature is intentionally disabled by default and requires an explicit
  production rollout setting.
- Physical tool reads remain at-least-once across an `EXECUTING` crash window;
  only durable result reuse is deduplicated.
- Trace telemetry remains operational diagnostics, not a replay source.
- The accepted R4 live/canonical evidence was not repeated for this bounded
  X1 composition ticket.

No X1-D2 acceptance blocker remains.

## 26. Final Classification

```text
X1_D2_PASS
```
