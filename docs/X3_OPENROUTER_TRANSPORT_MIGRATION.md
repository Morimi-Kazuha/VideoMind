# X3 OpenRouter model-execution transport

The benchmark keeps heterogeneous execution lanes: FAST and BALANCED use
`deepseek/deepseek-v4.1-flash`; DEEP uses `deepseek/deepseek-v4-pro-0813`.
FAST sends `reasoning_effort=none`, BALANCED omits that field, and DEEP sends
`reasoning_effort=max` plus `max_tokens=65536`.

The execution provider is fixed to OpenRouter's `nextbit/fp8` endpoint on
both models. Every request sends:

```json
{"provider":{"only":["nextbit/fp8"],"allow_fallbacks":false,"require_parameters":true,"data_collection":"deny","zdr":true}}
```

Only one model slug is sent per request, so there is no model fallback list.
Model-profile fingerprints record transport, provider pin, disabled provider
and model fallback, required parameters, and privacy controls. Jev remains
`typesafe/jev-1.13` on its separate OpenRouter decision path. It advises one
lane; DOVideo deterministic policy owns final routing. J1's 0.70 threshold,
rule-router-v1, the 0.90 lexical quality gate, golden-dataset-v2, provenance
contract, and benchmark labels remain unchanged.

## Provider selection and non-golden validation

On 2026-09-26, the live OpenRouter model endpoint API and authenticated ZDR
endpoint listing showed ten shared, active provider tags supporting both
models, `response_format`, `tools`, `tool_choice`, `reasoning_effort`,
`max_tokens`, and `temperature`, with Pro output capacity above 65,536.
NextBit was chosen after sequential account and region testing:

| Provider | FAST | BALANCED | DEEP | Decision |
| --- | --- | --- | --- | --- |
| DeepSeek | HTTP 404, account paid-training privacy restriction | not sent | not sent | Ineligible under account privacy policy |
| `coreweave/fp8` | HTTP 403 | HTTP 403, `unsupported_country_region_territory` | not sent | Region blocked |
| `wafer` | HTTP 200 | TLS handshake timeout, then read timeout on one retry | not sent | Unreliable on provider-default lane |
| `nextbit/fp8` | HTTP 200 | HTTP 200 | HTTP 200 | Selected |

Each NextBit success returned a JSON object matching the synthetic schema,
the requested model, provider identity `NextBit`, token usage, and
provider-reported cost. The one Pro request accepted `reasoning_effort=max`,
`max_tokens=65536`, `response_format=json_object`, a no-op tool definition,
and `tool_choice=none`; the tool was not invoked. Pro reported 227 prompt
tokens, 33 completion tokens, and $0.000344256. FAST reported $0.00001302;
BALANCED reported $0.00003402. No golden case was used for connectivity.
The individual diagnostic records are in ignored
`work/x3-openrouter-provider-smokes.jsonl`.

Source metadata and routing documentation:

- https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints
- https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-pro-0813/endpoints
- https://openrouter.ai/api/v1/endpoints/zdr
- https://openrouter.ai/docs/guides/routing/provider-selection

Earlier direct-DeepSeek campaigns are historical evidence. New
OpenRouter/NextBit campaigns must use new IDs and cannot be pooled with them
as one formal comparison.
