import test from "node:test";
import assert from "node:assert/strict";
import { reactive } from "vue";
import { answerPresentation, useClaimEvidence } from "./claimEvidence.js";
import { timelineMarkers } from "./analysisTimeline.js";

const citation = (extra = {}) => ({ id: "a", claim: "结论 A", content: "原文",
  source: "ASR", timestampMs: 312000, sourceRevision: "r1",
  segmentId: "segment-a", sourceItemIds: ["item-a"], ...extra });
const payload = (citations = [citation()]) => ({ sourceRevision: "r1",
  conclusions: ["结论 A", "结论 B", "结论 C"], citations });
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };
const response = (data) => ({ ok: true, json: async () => data });
const sidebarState = () => reactive({ mediaId: 1, generation: 1, goal: "目标",
  analysisMode: "GENERAL", content: "原始 Markdown", type: "ai", visible: true, loading: false });

test("exact claim equality allows multiple citations and leaves missing conclusions unbound", () => {
  const p = answerPresentation(payload([citation(), citation({ id: "b", source: "OCR" }),
    citation({ id: "c", claim: "结论 B" }), citation({ id: "d", claim: "结论" })]));
  assert.deepEqual(p.claims.map((c) => c.citations.length), [2, 1, 0]);
  assert.equal(p.citations.length, 3);
  const markers = timelineMarkers([], 600, [], p.citations);
  assert.equal(markers[0].seconds, 312);
  assert.equal(markers[0].key, "citation:a");
});

test("malformed and stale citations never become clickable bindings; legacy stays readable", () => {
  for (const change of [{ timestampMs: null }, { timestampMs: "312000" }, { timestampMs: -1 },
    { timestampMs: Infinity }, { sourceRevision: "r0" }, { segmentId: "" },
    { sourceItemIds: [] }, { sourceItemIds: [null] }, { content: "" }, { source: "HISTORY" }, { id: "" }]) {
    assert.equal(answerPresentation(payload([citation(change)])).citations.length, 0);
  }
  assert.deepEqual(answerPresentation([citation()]), { claims: [], citations: [] });
  assert.equal(answerPresentation(payload([])).claims.length, 3);
});

test("untrusted claim text is preserved as text without inferring a binding", () => {
  const text = '<img src=x onerror="alert(1)">';
  const p = answerPresentation({ ...payload(), conclusions: [text] });
  assert.equal(p.claims[0].claim, text);
  assert.deepEqual(p.claims[0].citations, []);
});

for (const change of ["mediaId", "generation", "goal", "analysisMode", "content", "visible", "type"]) {
  test(`late citation response is discarded after ${change} changes`, async () => {
    const sidebar = sidebarState();
    const late = deferred();
    const state = useClaimEvidence(() => sidebar, { request: () => late.promise,
      captureSession: () => ({ isCurrent: () => true }), subscribe: () => () => {} });
    const pending = state.refresh();
    sidebar[change] = typeof sidebar[change] === "number" ? 2 : typeof sidebar[change] === "boolean" ? false : "changed";
    late.resolve(response(payload()));
    await pending;
    assert.deepEqual(state.claims.value, []);
    state.dispose();
  });
}

test("newer request wins even after delayed JSON; revision and missing metadata clear prior links", async () => {
  const sidebar = sidebarState();
  const late = deferred();
  let next = { ok: true, json: () => late.promise };
  let path;
  const state = useClaimEvidence(() => sidebar, { request: async (p) => { path = p; return next; },
    captureSession: () => ({ isCurrent: () => true }), subscribe: () => () => {} });
  const pending = state.refresh();
  await Promise.resolve();
  next = response({ ...payload(), sourceRevision: "r2", citations: [citation({ sourceRevision: "r2" })] });
  await state.refresh();
  late.resolve(payload());
  await pending;
  assert.equal(state.citations.value[0].sourceRevision, "r2");
  assert.equal(new URL(path, "http://localhost").searchParams.get("includeConclusions"), "true");
  next = response([]);
  await state.refresh();
  assert.deepEqual(state.claims.value, []);
  assert.equal(sidebar.content, "原始 Markdown");
  state.dispose();
});

test("auth switch clears visible citations and rejects an outstanding response", async () => {
  const sidebar = sidebarState();
  let auth = 1, listener, next = response(payload());
  const state = useClaimEvidence(() => sidebar, { request: async () => next,
    captureSession: () => { const original = auth; return { isCurrent: () => auth === original }; },
    subscribe: (fn) => { listener = fn; return () => {}; } });
  await state.refresh();
  assert.equal(state.citations.value.length, 1);
  auth = 2; listener();
  assert.deepEqual(state.citations.value, []);
  const late = deferred(); next = late.promise;
  const pending = state.refresh();
  auth = 3; listener(); late.resolve(response(payload()));
  await pending;
  assert.deepEqual(state.citations.value, []);
  state.dispose();
});

test("unmount prevents a delayed response and unsubscribes from auth", async () => {
  const late = deferred(), sidebar = sidebarState();
  let removed = false;
  const state = useClaimEvidence(() => sidebar, { request: () => late.promise,
    captureSession: () => ({ isCurrent: () => true }), subscribe: () => () => { removed = true; } });
  const pending = state.refresh(); state.dispose(); late.resolve(response(payload()));
  await pending;
  assert.equal(removed, true);
  assert.deepEqual(state.claims.value, []);
});
