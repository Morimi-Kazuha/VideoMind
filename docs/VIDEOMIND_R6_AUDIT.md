# R6 audit before patch (2026-09-30)

Baseline: main, 6eaaa6111527dd43fb7cee4cb9b60b330492d076, clean tree;
project Python 3.12.14: 864 passed; Node 24.15.0: 16 passed, build passed.
System Python 3.13.3 has no pytest; use .venv/Scripts/python.exe.

Upstream commits were unavailable locally and via the browser. This matrix
audits the supplied architectural issues against the current Python/Vue code;
it does not claim verification of those upstream commits.

| Item | Affected / protection | Action |
|---|---|---|
| A auth | unconditional 401 clears latest token | IMPLEMENT |
| B workspace | identity comparison misses same-key rerun/reopen; metadata checks before JSON await | IMPLEMENT |
| C SSE | buffered callbacks after stop; terminal reader not cancelled; old Redis list includes earlier executions | IMPLEMENT |
| D identity | TaskKey and Redis task locks use media/goal/mode, not content hash | TEST_ONLY |
| E revision | result-first query; HTTP revision only saves feedback then returns duplicate | IMPLEMENT |
| F hydration | read/read_payload warm payload and historical stage together | IMPLEMENT |
| G reuse | task result checkpoints are per media; no cross-media final result reuse | TEST_ONLY |
| H upload | browser key omits user; App has user check but progress/finally unguarded | IMPLEMENT |
| I merge loss | backend completed marker + lock + owner check already implemented; client misreads structured status/init | IMPLEMENT client; TEST_ONLY backend |
| J upload | client accepts any video MIME; production input exceptions lack distinct 413/415 mapping | IMPLEMENT |
| K markdown | global regex before marked.parse | IMPLEMENT |
| L memory | grounding strong; no persistent follow-up memory port or shared storage policy | DEFER (evaluate separately; retain verification) |
| M budget | provider usage/retry observed; follow-up has no enclosing deadline | IMPLEMENT |
| N transcription | production R4 explicitly unsupported; local projection can retain earlier terminal state | IMPLEMENT local; NOT_APPLICABLE production worker |
| O media cache | DB list reads directly, no server list cache | NOT_APPLICABLE |
| P UI stage | existing display map; unknown trace stages display raw internals | IMPLEMENT small mapping |
| Q faults | only 16 client tests; substantial backend invariants already tested | IMPLEMENT deterministic races |

No deployment, push, release, key edits, or product redesign planned.

Audit clarification after inspecting the existing validation handler: missing
multipart fields already return 400. Preserve that behavior and lock it with
an HTTP regression test; the initial 422 observation was incorrect.
