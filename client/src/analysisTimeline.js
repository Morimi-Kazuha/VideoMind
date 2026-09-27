export function validEvidenceTime(hit) {
  return (
    hit?.startMs !== null &&
    hit?.startMs !== undefined &&
    Number.isFinite(Number(hit.startMs)) &&
    Number(hit.startMs) >= 0
  );
}

export function evidenceKey(hit, index) {
  return `${hit?.startMs ?? "unknown"}:${hit?.endMs ?? "unknown"}:${hit?.chunkId || hit?.segmentId || index}`;
}

export function timelineMarkers(
  hits,
  durationSeconds,
  windows = [],
  citations = [],
  observations = [],
  observationTotal = observations.length,
) {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) return [];
  const evidence = [
    ...hits,
    ...citations.map((citation) => ({
      ...citation,
      startMs: citation.timestampMs,
      endMs: citation.timestampMs,
      snippet: citation.content,
      cited: true,
    })),
  ];
  const evidenceMarkers = evidence.flatMap((hit, index) => {
    if (!validEvidenceTime(hit)) return [];
    const seconds = Number(hit.startMs) / 1000;
    if (seconds > durationSeconds) return [];
    const end = Math.max(seconds, Number(hit.endMs) / 1000 || seconds);
    const left = Math.min(100, (seconds / durationSeconds) * 100);
    const width = Math.max(
      0.65,
      Math.min(100 - left, ((end - seconds) / durationSeconds) * 100),
    );
    const base = {
      key: hit.cited ? `citation:${hit.id}` : evidenceKey(hit, index),
      hit,
      seconds,
      left,
      width,
    };
    const markers = [{ ...base, lane: "evidence" }];
    if (!windows.length && hit.transcript?.trim())
      markers.push({ ...base, lane: "asr" });
    if (
      !windows.length &&
      Array.isArray(hit.ocrTexts) &&
      hit.ocrTexts.some((text) => text?.trim())
    ) {
      markers.push({ ...base, lane: "ocr" });
    }
    return markers;
  });
  const useObservations =
    observations.length > 0 && observations.length >= observationTotal;
  const temporalSources = useObservations ? observations : windows;
  if (!temporalSources.length) return evidenceMarkers;
  const temporalMarkers = temporalSources.flatMap((record) => {
    if (!validEvidenceTime(record)) return [];
    const seconds = Number(record.startMs) / 1000;
    if (seconds > durationSeconds) return [];
    const left = Math.min(100, (seconds / durationSeconds) * 100);
    const end = Math.max(seconds, Number(record.endMs) / 1000 || seconds);
    const width = Math.max(
      0.65,
      Math.min(100 - left, ((end - seconds) / durationSeconds) * 100),
    );
    const base = {
      key: useObservations
        ? `observation:${record.id}`
        : `window:${record.segmentId || record.startMs}`,
      hit: record,
      seconds,
      left,
      width,
      count: 1,
    };
    const result = [];
    if (
      useObservations
        ? record.kind === "ASR" && record.text?.trim()
        : record.transcript?.trim()
    )
      result.push({ ...base, lane: "asr" });
    if (
      useObservations
        ? record.kind === "OCR"
        : record.ocrTexts?.some((text) => text?.trim())
    )
      result.push({ ...base, lane: "ocr" });
    return result;
  });
  return [...aggregateDenseWindows(temporalMarkers), ...evidenceMarkers];
}

function aggregateDenseWindows(markers) {
  const byBin = new Map();
  for (const marker of markers) {
    const bin = Math.min(119, Math.floor(marker.left * 1.2));
    const key = `${marker.lane}:${bin}`;
    const previous = byBin.get(key);
    if (previous) {
      previous.count += 1;
      previous.width = Math.max(
        previous.width,
        Math.min(
          100 - previous.left,
          marker.left + marker.width - previous.left,
        ),
      );
    } else {
      byBin.set(key, { ...marker });
    }
  }
  return [...byBin.values()];
}

export function formatMediaTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const remainder = String(total % 60).padStart(2, "0");
  return total >= 3600
    ? `${String(Math.floor(total / 3600)).padStart(2, "0")}:${minutes}:${remainder}`
    : `${minutes}:${remainder}`;
}
