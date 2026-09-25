# X3 v2 evaluation interruption report

Status as of 2026-09-26 (Asia/Shanghai): C2 structurally complete; full X3-C
attempt invalidated by external provider access; X3-D and a conclusive X3-E
evaluation remain blocked. This report preserves negative results. It does not
claim a routing winner.

## Source and benchmark identities

| Item | Frozen identity |
| --- | --- |
| Measured source SHA | `56252c9e86ba18dc016dc4111da8cc439f0b5ac7` |
| Dataset | `golden-dataset-v2`, 16 cases, `DATASET_INCOMPLETE` |
| Logical dataset digest | `7f374396c5eb002ba158717afaffa8a65f5ee64ed79663f1f6b6047960dae8e4` |
| Canonical source revision | `45d7e5513af70f05c36ffeb04d981948d2cec85c874ff6f4ac5b5f0e83ca47bd` |
| Provenance | `x2-a-v3`, Whisper span extraction contract `video-context-whisper-spans-v2` |
| Pricing | `pricing-x3c-v1`, digest `d6d6902af7e0d4e9f9906ec21768fe27eb8cf5aa69168d4dd7dce4127b653a25` |
| Rule router | `rule-router-v1`, digest `358089a27926ba86653b6ea2330696594c828e31d06ae7fdda53ec3bf0f737d9` |
| Quality gate | `routing-quality-gate-v1`, digest `e90a1aa26e2bba6187902928e31b92356e80f759cfeb2533b13818322258be9c` |
| Jev | `typesafe/jev-1.13`, confidence threshold `0.70` |

The FAST/BALANCED/DEEP model profile fingerprints are, respectively,
`ebebf692e007162672670b434efef7b91a0f261edb381584cfc4060bd96fba6e`,
`7e2427adb87e3072a6a6b0f4127f05b27d8949133da1417905922e5196b04326`,
and `904e40f9774155797df2c2d13115bcc076d2f7a5adbe8b7559baead644a21a6a`.
BALANCED omits `reasoning_effort` and uses the provider default.

Dataset v1 remains immutable. Its source revision was orphaned from reachable
prepared state. Dataset v2 retains the 16 questions and rebinds evidence by
temporal provenance after deterministic OCR identity and Whisper span fixes.
One ambiguous old reference became a temporal region rather than an invented
source item. The prepared media is media ID 17 with 53 source items and two
canonical chunks; ingestion was excluded from benchmark latency.

## C2 measured smoke

Artifact: `work/x3-campaigns/c2-v2-56252c9-01/`. One case
(`long-general-001`), one trial, five separate `EvaluationRun`s, fixed lanes
first. FAST and BALANCED ended at the production token budget. DEEP and
RULE_ROUTER returned successful Agent results, but their lexical required fact
exact coverage was `0.0`. JEV_ROUTER returned a successful Agent result after
an unavailable Jev response caused the deterministic BALANCED fallback; its
exact coverage was also `0.0`. The empirical oracle recorded
`NO_PASSING_LANE`. This smoke proves orchestration and failure persistence,
not broad strategy quality. Earlier C2 attempts remain in separate artifact
directories as historical diagnostics and were not cherry-picked into this
result.

## Full X3-C attempt

Artifact: `work/x3-campaigns/x3c-v2-56252c9-full-01/`. All five runs recorded
the same source SHA, clean working tree, dataset digest, pricing version, and
16 of 16 executed cases. Thus 80 case results are durable, but a completed
run status only describes execution bookkeeping. It does not certify a valid
comparison after provider access failed.

| Strategy | Successful Agent results | Recorded failures | Frozen gate passes | Provider reported token subtotal | Median observed latency |
| --- | ---: | --- | ---: | ---: | ---: |
| ALWAYS_FAST | 2/16 | 14 budget | 0 | 953,065 across 16 cases | 25.2 s |
| ALWAYS_BALANCED | 9/16 | 6 budget, 1 invalid model DTO | 0 | 870,169 across 16 cases | 48.9 s |
| ALWAYS_DEEP | 3/16 | 2 budget, 1 direct provider error, 10 provider fallbacks recorded under the old instrumentation category | 0 | 251,735 across 6 cases | 0.53 s, dominated by immediate HTTP 402 failures |
| RULE_ROUTER | 0/16 | 16 provider fallbacks recorded under the old instrumentation category | 0 | unavailable | 0.51 s, HTTP 402 failure latency |
| JEV_ROUTER | 0/16 | 16 provider fallbacks recorded under the old instrumentation category | 0 | 5,604 across 11 router responses; no complete Agent totals | 1.92 s, routing plus HTTP 402 failure latency |

**External failure evidence.** Redis structural traces show 43 DeepSeek HTTP
`402` responses: 11 during ALWAYS_DEEP, 16 during RULE_ROUTER, and 16 during
JEV_ROUTER. One DEEP failure surfaced directly as a provider request error;
42 retrieval planner failures were wrapped by the strict production fallback
and persisted as `INSTRUMENTATION_FAILURE` at the measured SHA. Commit
`0f30cc6` fixes future classification to `PROVIDER_FAILURE` without rewriting
these historical records. No further provider calls were made after the
outage was diagnosed.

The rule router chose BALANCED for all 16 cases. Jev decisions resolved to
BALANCED for 15 and FAST for one: 10 were `LOW_CONFIDENCE`, five
`ROUTER_UNAVAILABLE`, and one `ROUTER_ACCEPTED`. All 32 routed Agent
executions then encountered DeepSeek HTTP 402, so their output quality and
end-to-end latency cannot be compared with the fixed lanes.

All 14 successful fixed-lane Agent results had `0.0` lexical required fact
exact coverage. The frozen gate therefore selected no fixed lane for any of
the 16 case/trial pairs. A saved C2 answer paraphrases the required facts but
does not reproduce the annotation descriptions verbatim. This shows the
known limitation of the preregistered lexical metric; it is not a semantic
correctness judgment. The gate threshold was not changed. Retrieval Recall@K
is `NOT_MEASURED` because this execution adapter did not emit ranked retrieval
hits; final evidence and provenance checks are separate measurements.

## Usage and cost

Across the full attempt, 169 successful DeepSeek chat calls reported
2,074,969 tokens. Another 43 DeepSeek requests returned HTTP 402. DeepSeek
did not report exact billed cost, and the frozen tariff depends on billing
period and cache hit/miss conditions that were not measured. No scalar
DeepSeek or campaign total cost is asserted.

Sixteen Jev routing attempts were recorded. Eleven returned provider usage
and provider reported cost totaling **USD 0.000215544**; five had no reported
cost. That amount is the sum of those eleven reports, not the total Jev or
campaign charge. Prepared-state provider calls are documented separately in
`docs/X3_PREPARED_STATE_V2.md` and are excluded from these benchmark counts.

## X3-D and interpretation limit

The planned paired routing analysis matched each router case to the fixed
lane it selected. There are 16 structural pairs for each router, but zero
pairs with a successful routed Agent result after the 402 outage. The data
cannot estimate whether adaptive routing improved quality, cost, or latency.
No additional ablation calls were made. X3-D remains blocked rather than
being filled with a post-hoc threshold change or fabricated comparison.

Dataset v2 covers one media file, four modes with four cases each, and ASR
backed annotations. It lacks OCR-heavy and multi-media coverage. Provider
stochasticity is visible across the separate C2 attempts. Exact provider
costs and ranked retrieval metrics are incomplete. These constraints prevent
an overall victory claim even before the HTTP 402 outage.

## Verification

After the provider-fallback classification fix: 836 first-party Python tests
passed, `compileall` passed, both client tests passed, and the client build
passed. The benchmark artifacts were reparsed through the evaluation contract;
all five runs recorded `CLEAN` at the measured SHA. The local repository was
not pushed.

## Resume gate

Restore DeepSeek API access and verify it outside the formal benchmark. Then
create a new clean benchmark freeze containing the post-run classification
fix, restart C2 from zero, and rerun the affected full X3-C scope under a new
campaign ID. Preserve all existing artifacts. Proceed to useful X3-D
comparisons and a conclusive X3-E report only after that measured run is
valid. No push or external account change was performed by Codex.
