import { computed, ref } from "vue";
import { apiRequest, captureAuthSession, onAuthSessionChange } from "./api.js";

// This only adapts the server's verified projection. It never infers support
// from Markdown, similarity, search hits, or conversation history.
export function answerPresentation(payload) {
  const conclusions = Array.isArray(payload?.conclusions)
    ? payload.conclusions.filter((claim) => typeof claim === "string" && claim.trim())
    : [];
  const revision = payload?.sourceRevision;
  const citations = typeof revision === "string" && revision
    && Array.isArray(payload?.citations)
    ? payload.citations.filter((item) =>
      typeof item?.id === "string" && item.id.length > 0
      && conclusions.includes(item.claim)
      && typeof item.content === "string" && item.content.trim().length > 0
      && typeof item.source === "string" && /ASR|OCR/i.test(item.source)
      && Number.isSafeInteger(item.timestampMs) && item.timestampMs >= 0
      && item.sourceRevision === revision
      && typeof item.segmentId === "string" && item.segmentId.length > 0
      && Array.isArray(item.sourceItemIds) && item.sourceItemIds.length > 0
      && item.sourceItemIds.every((id) => typeof id === "string" && id.length > 0))
    : [];
  return {
    citations,
    claims: conclusions.map((claim) => ({
      claim, citations: citations.filter((item) => item.claim === claim),
    })),
  };
}

export function useClaimEvidence(getSidebar, {
  request = apiRequest, captureSession = captureAuthSession,
  subscribe = onAuthSessionChange,
} = {}) {
  const snapshot = ref(null);
  let requestId = 0;
  let active = true;
  const clear = () => { requestId += 1; snapshot.value = null; };
  const unsubscribe = subscribe(clear);
  const presentation = computed(() => snapshot.value?.current()
    ? snapshot.value.presentation : { claims: [], citations: [] });

  async function refresh() {
    clear();
    const id = requestId;
    const sidebar = getSidebar();
    if (!active || !sidebar.visible || sidebar.type !== "ai"
      || sidebar.loading || !sidebar.content || !sidebar.mediaId) return;
    const session = captureSession();
    const scope = [sidebar.mediaId, sidebar.generation, sidebar.goal,
      sidebar.analysisMode, sidebar.content, sidebar.loading, sidebar.visible, sidebar.type];
    const current = () => active && id === requestId && session.isCurrent()
      && getSidebar() === sidebar && scope.every((value, index) => value === [
        sidebar.mediaId, sidebar.generation, sidebar.goal, sidebar.analysisMode,
        sidebar.content, sidebar.loading, sidebar.visible, sidebar.type,
      ][index]);
    const params = new URLSearchParams({ id: String(sidebar.mediaId),
      goal: sidebar.goal, mode: sidebar.analysisMode, includeConclusions: "true" });
    try {
      const response = await request(`/analysis/agent-citations?${params}`);
      if (!response.ok) return;
      const payload = await response.json();
      if (current()) snapshot.value = { current, presentation: answerPresentation(payload) };
    } catch {
      // Legacy Markdown remains available if structured metadata is absent.
    }
  }
  return {
    claims: computed(() => presentation.value.claims),
    citations: computed(() => presentation.value.citations),
    refresh,
    dispose() { active = false; clear(); unsubscribe(); },
  };
}
