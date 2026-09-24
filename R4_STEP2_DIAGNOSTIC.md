# LOOPRAIL R4 — Diagnostic Propagation Closure

## 1. Status

`R4 CRITIC INCIDENT CONTAINED — INTERMITTENT ROOT CAUSE UNKNOWN — DIAGNOSTIC PROPAGATION VERIFIED`

The historical Critic DTO failure remains an intermittent observed incident.
Its field-level model-response cause was not reproduced and is not inferred.
The observability defect that could turn a recurrence into a type-only report is
contained by the existing failure boundary.

## 2. Exact diagnostic loss point

The path is:

`OpenAICompatibleChatClient.complete()` → `_extract_content()` →
`CriticModelAdapter.critique()` → `decode_structured_model()` →
`CriticResult.model_validate()` → `ValidationError` →
`ModelResponseError` → R4 observed client/AgentLoop/TaskWorker →
durable failure and dead-letter reporting.

The Critic decoder already created a safe structural summary in the
`ModelResponseError` text. The Critic adapter, R4 observed client, AgentLoop,
and TaskWorker did not replace that exception. The existing MySQL failure row
and pending handoff also used bounded `str(error)`, so the text was available
there.

The exact loss in the previous final evidence was at the report boundary: the
live harness reduced the caught exception to `type(exc).__name__` and did not
read its message. Independently, the existing
`RabbitMQDeadLetterPublisher._error_document()` exposed only `type` and a
512-character message, with no separate structural-diagnostic field. A
message-only path could therefore be truncated or discarded by a type-only
harness.

## 3. Minimal implementation

The following bounded propagation was added:

- `ModelResponseError` now accepts an optional `diagnostic` string while
  retaining the same external exception class and default construction.
- The Critic DTO `ValidationError` path attaches the already-safe diagnostic to
  that exception and keeps the existing message and `from exc` cause.
- Diagnostic construction reads at most 64 top-level payload entries, records
  at most 32 validation errors, and retains at most 8 location parts per
  error. Top-level keys, field types, locations, error types, and messages
  remain the only structural data selected.
- The existing dead-letter error document carries an optional separately
  bounded `diagnostic` field (maximum 2048 characters), while preserving its
  existing `type` and `message` fields.

No AgentLoop, TaskWorker retry policy, DTO schema, prompt, parser, coercion,
fallback, or provider behavior was changed.

## 4. Privacy and bounding

The propagation does not carry the response payload, field values, transcript,
provider response body, or credentials. Diagnostic labels are control-character
sanitized and the transport boundary applies the existing secret-pattern
redaction plus a second length cap. The MySQL failure message and pending
handoff continue to use their existing bounds; the new DLQ field is additive
and JSON-safe.

## 5. Synthetic malformed DTO

The offline fixture returned valid JSON with `feedback` as a string while the
frozen `CriticResult` DTO requires a collection. This synthetic field-shape
violation is only a deterministic propagation probe. It is **not** the
historical root cause and is not evidence about the historical Provider
response.

## 6. Offline E2E propagation evidence

The new deterministic test drives the closest production path with an
in-process fake HTTP response:

`ProviderHttpResponse` → OpenAI-compatible content extraction →
`_ObservedChatClient` → `CriticModelAdapter` → Critic DTO validation →
`ModelResponseError` → `PendingDeadLetterHandoff`/failed-task record →
existing dead-letter error document.

The final report boundary proved all of the following:

- external classification is `ModelResponseError`;
- the message identifies the `CRITIC` DTO stage;
- diagnostic payload type is `dict`;
- bounded top-level keys and field types are present;
- Pydantic `loc`, `type`, and `msg` are present for `feedback`;
- the synthetic value and the full malformed response are absent;
- the durable failed-task record retains the diagnostic-bearing bounded error
  message;
- no network, live Provider, media, worker, FFmpeg, Whisper, OCR, embedding,
  or Qdrant operation was used.

## 7. Exact commands and results

The PowerShell prefix used for each command was:

`$env:PYTHONPATH = 'D:\Agent Learning\dovideo-python\src;D:\Agent Learning\dovideo-python\work\r1-venv\Lib\site-packages;D:\Agent Learning\dovideo-python\tools\asr\python-packages'; & 'D:\python\python.exe' -m pytest -q <test-path>`

The resulting commands and results were:

- `D:\python\python.exe -m pytest -q tests\infrastructure\test_r4_diagnostic_propagation.py` — **1 passed**
- `D:\python\python.exe -m pytest -q tests\infrastructure\test_provider_adapters_10a.py` — **13 passed**
- `D:\python\python.exe -m pytest -q tests\application\test_dead_letter_handoff_9b_fix2.py` — **2 passed**
- `D:\python\python.exe -m pytest -q tests\application\test_task_worker_9b.py` — **15 passed**
- `D:\python\python.exe -m pytest -q tests\infrastructure\test_celery_r3.py` — **7 passed**

`Provider/LLM calls: 0`

## 8. Root-cause boundary

Historical field-level root cause remains unknown. No speculative behavioral fix was implemented.

## 9. Changed files

- `D:\Agent Learning\dovideo-python\src\dovideo\infrastructure\providers\errors.py`
- `D:\Agent Learning\dovideo-python\src\dovideo\infrastructure\providers\model.py`
- `D:\Agent Learning\dovideo-python\src\dovideo\infrastructure\celery_transport.py`
- `D:\Agent Learning\dovideo-python\tests\infrastructure\test_r4_diagnostic_propagation.py`
- `D:\Agent Learning\dovideo-python\R4_STEP2_DIAGNOSTIC.md`

The four final R4 documents were not changed. The existing Qdrant fix was not
changed. No Provider key was printed, written to this report, or persisted.

## 10. R4 continuation

The historical intermittent Critic DTO incident no longer blocks R4. The repository is ready to continue the originally planned R4 work.
