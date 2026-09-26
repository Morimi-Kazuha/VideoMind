export function validEvidenceTime(hit) {
  return (
    hit?.startMs !== null &&
    hit?.startMs !== undefined &&
    Number.isFinite(Number(hit.startMs)) &&
    Number(hit.startMs) >= 0
  )
}

export function evidenceKey(hit, index) {
  return `${hit?.startMs ?? 'unknown'}:${hit?.endMs ?? 'unknown'}:${hit?.chunkId || hit?.segmentId || index}`
}

export function timelineMarkers(hits, durationSeconds) {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) return []
  return hits.flatMap((hit, index) => {
    if (!validEvidenceTime(hit)) return []
    const seconds = Number(hit.startMs) / 1000
    if (seconds > durationSeconds) return []
    const end = Math.max(seconds, Number(hit.endMs) / 1000 || seconds)
    const left = Math.min(100, (seconds / durationSeconds) * 100)
    const width = Math.max(
      0.65,
      Math.min(100 - left, ((end - seconds) / durationSeconds) * 100),
    )
    const base = { key: evidenceKey(hit, index), hit, seconds, left, width }
    const markers = [{ ...base, lane: 'evidence' }]
    if (hit.transcript?.trim()) markers.push({ ...base, lane: 'asr' })
    if (
      Array.isArray(hit.ocrTexts) &&
      hit.ocrTexts.some((text) => text?.trim())
    ) {
      markers.push({ ...base, lane: 'ocr' })
    }
    return markers
  })
}

export function formatMediaTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0')
  const remainder = String(total % 60).padStart(2, '0')
  return total >= 3600
    ? `${String(Math.floor(total / 3600)).padStart(2, '0')}:${minutes}:${remainder}`
    : `${minutes}:${remainder}`
}
