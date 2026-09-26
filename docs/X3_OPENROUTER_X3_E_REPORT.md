# X3 OpenRouter/NextBit evaluation closeout

Status: X3-C completed structurally; X3-D read-only paired routing analyses
completed; X3-E interpretation below. The preregistered quality gate passed
zero cases. This is a measured negative result, not an adaptive-routing win.
Earlier direct-DeepSeek campaigns remain separate historical diagnostics and
are not pooled with these OpenRouter/NextBit results.

## Frozen experiment identity

| Item | Identity |
| --- | --- |
| Benchmark-ready source SHA | `d0c8480c12b4bee1c43acbc4d647af415148a656` |
| Freeze artifact | `work/x3-openrouter-nextbit-freeze-d0c8480.json` |
| Dataset | `golden-dataset-v2`, 16 cases, `DATASET_INCOMPLETE` |
| Dataset logical digest | `7f374396c5eb002ba158717afaffa8a65f5ee64ed79663f1f6b6047960dae8e4` |
| Canonical source revision | `45d7e5513af70f05c36ffeb04d981948d2cec85c874ff6f4ac5b5f0e83ca47bd` |
| Prepared media | media ID 17, two canonical chunks; ingestion excluded from benchmark latency |
| Provenance | `x2-a-v3`; Whisper span extraction contract `video-context-whisper-spans-v2` |
| Execution transport | OpenRouter OpenAI-compatible API, one model slug per request, `nextbit/fp8` only |
| Provider policy | fallback disabled, required parameters, data collection denied, ZDR required |
| Rule router | `rule-router-v1`, digest `358089a27926ba86653b6ea2330696594c828e31d06ae7fdda53ec3bf0f737d9` |
| Quality gate | `routing-quality-gate-v1`, digest `e90a1aa26e2bba6187902928e31b92356e80f759cfeb2533b13818322258be9c` |
| Pricing | `pricing-x3c-openrouter-v1`, digest `a3cc738d2369722b48e20aa7a4b3ee9239c63f42b3ff0b6036f0fec79ce79be6` |
| Jev | `typesafe/jev-1.13` through OpenRouter, advisory, confidence threshold `0.70` |

FAST and BALANCED use `deepseek/deepseek-v4.1-flash`. FAST sends
`reasoning_effort=none`; BALANCED omits the reasoning field. DEEP uses
`deepseek/deepseek-v4-pro-0813` with `reasoning_effort=max` and
`max_tokens=65536`. Their transport-aware fingerprints are, respectively,
`bd605d01eade256e6d3412b43fa4b376444c08e24cfb013027d45eb67b4de55b`,
`44dae58f205e22f929484dd5ddce4580653e356c70bb61122fcb65c56edfa4e0`,
and `8f03c705bb0a084982e1339d958a80e6bf0d70bbf96e9051d65ddff5bf0932f2`.
These identities cannot be confused with the earlier direct-DeepSeek profiles.

The provider selection, official endpoint metadata, routing syntax, privacy
checks, and synthetic non-golden smokes are recorded in
`docs/X3_OPENROUTER_TRANSPORT_MIGRATION.md`. CoreWeave was region-blocked;
NextBit passed one FAST, one BALANCED, and exactly one DEEP/Pro validation
smoke. The smokes are excluded from benchmark counts and cost.

## Measured campaigns

The fresh C2 artifact is
`work/x3-campaigns/c2-v2-or-nextbit-d0c8480-01/`. It contains one
`long-general-001` result for each of the five strategies. All five runs
completed structurally, and the diagnostic oracle recorded
`NO_PASSING_LANE`. FAST, DEEP, RULE_ROUTER, and JEV_ROUTER recorded budget
failures; BALANCED recorded a schema failure. Provider-reported cost was
available on all five C2 cases and totaled USD 0.074354358. That amount is
separate from the full campaign and the non-golden transport smokes. C2 is a
connectivity and orchestration check, not a quality estimate.

The full campaign is
`work/x3-campaigns/x3c-v2-or-nextbit-d0c8480-full-01/`. Five independent
strategy-specific `EvaluationRun`s each persisted 16 results at the same
source SHA, dataset digest, source revision, pricing version, and clean
working-tree state. The campaign status is `COMPLETED`; all 80 results and
the 16-case oracle were reparsed through the evaluation contracts and checked
by `tools/analyze_x3_campaign.py`. All 16 oracle entries are
`NO_PASSING_LANE` under the frozen gate.

| Strategy | Agent successes | Recorded failures | Gate passes | Provider cost, USD (known cases) | Median observed end-to-end latency | Reported token subtotal (known cases) |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| ALWAYS_FAST | 0/16 | 16 budget | 0 | 0.229294814 (16/16) | 54.5 s | 925,486 (16/16) |
| ALWAYS_BALANCED | 5/16 | 11 budget | 0 | 0.213886798 (15/16) | 120.0 s | 644,420 (15/16) |
| ALWAYS_DEEP | 1/16 | 15 budget | 0 | 0.379346736 (15/16) | 120.1 s | 290,980 (15/16) |
| RULE_ROUTER | 7/16 | 9 budget | 0 | 0.198318366 (16/16) | 107.8 s | 700,394 (16/16) |
| JEV_ROUTER | 9/16 | 6 budget, 1 instrumentation label | 0 | 0.149543738 (11/16) | 102.5 s | 568,971 (11/16) |

All cost figures are sums of provider-reported values on the indicated cases,
not tariff estimates. The known-case subtotal across strategies is
**USD 1.170390452 on 73/80 cases**. Seven case totals are unavailable, so no
scalar full-campaign charge is asserted. BALANCED and DEEP each have one
budget failure with no provider-usage record. Five Jev fallback cases have a
router record without usage/cost, preventing a complete combined case total
even when model-execution records contain values. The reported token subtotal
is 3,130,251 across records with complete case totals. Median latency includes
failed executions and reflects the frozen budgets and timeouts; it is not
successful-answer latency.

All 22 successful Agent results passed schema, mode-section, evidence-guard,
and final Critic checks. Their lexical `requiredFactExactCoverage` was 0.0,
below the preregistered 0.90 threshold. The metric checks exact annotated
phrases and is not a semantic correctness judgment. Among successful results,
median ranked retrieval Recall@K was 0 for each strategy with successes;
median final evidence support rate was 1.0. Final evidence recall medians were
1.0 for ALWAYS_BALANCED and JEV_ROUTER, 0.5 for RULE_ROUTER, and 0 for
ALWAYS_DEEP. These are different trace-derived measures and should not be
collapsed into one quality score. Human and judge ratings were not measured.
Tool calling was disabled for this frozen routing comparison.

## X3-D bounded routing ablations

The five measured strategies already provide the preregistered fixed-lane
controls. The read-only X3-D analysis pairs each routed case with the
separately executed fixed-lane result for the lane it selected. No additional
model calls or altered gate were used. `x3d-analysis.json` in the full
campaign directory records the exact derived counts and coverage.

| Paired comparison | Selected lane | Routed Agent successes | Corresponding fixed-lane successes | Gate passes, both sides | Paired provider-cost delta | Median observed latency delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| RULE_ROUTER vs selected fixed lane | BALANCED 16/16 | 7/16 | 5/16 | 0 | -$0.038950016 on 15 cost-complete pairs | -5.96 s on 16 pairs |
| JEV_ROUTER vs selected fixed lane | BALANCED 16/16 | 9/16 | 5/16 | 0 | -$0.000504598 on 10 cost-complete pairs | -13.36 s on 16 pairs |

RULE_ROUTER chose BALANCED for all 16 cases. Jev suggested a lane but all
final routes were BALANCED: 11 `LOW_CONFIDENCE` fallbacks and five
`ROUTER_UNAVAILABLE` fallbacks. No Jev suggestion cleared the frozen policy
and changed the executed lane. Thus this run did not demonstrate adaptive
lane selection in the two router arms, despite preserving heterogeneous
FAST/BALANCED/DEEP fixed-lane controls. The paired success differences arise
from separate stochastic executions of the same BALANCED profile. With one
trial per case, zero gate passes, and incomplete paired cost telemetry, they
do not establish a routing quality or cost advantage. The cost deltas are
subtotals on the stated pairs, not whole-strategy savings.

## Diagnostics and limits

One JEV_ROUTER case is frozen as `INSTRUMENTATION_FAILURE`; its local
`failure.json` identifies an `InvalidPlanError` at the production plan
validator. This generic category is an instrumentation-taxonomy limitation,
not evidence that the model-execution provider failed. The measured artifact
has not been patched or rerun. The remaining recorded Agent failures are
budget exhaustion. Jev unavailability is represented separately in the five
router fallback decisions.

`golden-dataset-v2` preserves 16 provenance-backed questions over one media
file and four modes. Its v1 predecessor remains immutable; v2 changed the
provenance binding after stable OCR frame identity and finer Whisper span
provenance were established. The dataset remains incomplete: it has limited
OCR-heavy and multi-media coverage. Provider generation is stochastic, one
trial per case was run, and the exact lexical gate is a narrow quality proxy.
The result supports a conclusion about this frozen NextBit/OpenRouter
configuration, not other OpenRouter providers or direct DeepSeek transport.
No threshold, router boundary, golden annotation, provenance contract, or
benchmark label was changed after observing outcomes.

## Reproducibility and disposition

`campaign.json` SHA-256 is
`e12afcb002d5ef6e5d2fd0ae8e03dc0cbb5e4d431426a77ba17d34a09f62cfc7`.
The five `results.jsonl` SHA-256 values in strategy order are
`0dd2315cb1984080254c0ab54e602194b21e11809edcbc363147fda68d7f3e14`,
`eb01a6d0225261e92a582286affd0369c4f869375512f7ee49485abe193e9c12`,
`9fe3f3042c9065d37e2a10d5516c6531a9c3622e9b8eb9273329c7c46caad016`,
`8bb545b6df57a5c949449a208f0a63e31d8ec7d599fd8e8ef828f5711b5922cb`,
and `d70fdf8dbebe2fd4ab2654c265f19e5e3125ab7560388eef3a83d68cb6830acf`.
The analysis artifact SHA-256 is
`f70b754698e8c34506c7e531191655b09648f57f2fd4d7295e7faf5ed749576d`.

The experiment is complete as a bounded negative evaluation. Improving the
lexical gate, budget, or router would require a separately justified,
versioned future study; these observed results do not authorize changing the
frozen X3 comparison. The old direct-DeepSeek campaigns, both OpenRouter C2
attempts, non-golden provider diagnostics, and this full campaign remain in
separate local artifact directories. No remote push was performed.
