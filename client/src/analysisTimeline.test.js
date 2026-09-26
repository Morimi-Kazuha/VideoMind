import test from 'node:test'
import assert from 'node:assert/strict'
import {
  evidenceKey,
  formatMediaTime,
  timelineMarkers,
  validEvidenceTime,
} from './analysisTimeline.js'

test('timeline uses only real seekable evidence timestamps and source fields', () => {
  const hits = [
    { startMs: 0, endMs: 3000, transcript: 'hello', ocrTexts: ['slide'] },
    { startMs: 5000, endMs: 7000, transcript: '', ocrTexts: [] },
    { startMs: null, endMs: null, transcript: 'missing timestamp' },
    { startMs: 12000, endMs: 13000, transcript: 'outside duration' },
  ]
  assert.equal(validEvidenceTime(hits[2]), false)
  const markers = timelineMarkers(hits, 10)
  assert.deepEqual(
    markers.map((marker) => marker.lane),
    ['evidence', 'asr', 'ocr', 'evidence'],
  )
  assert.equal(markers[0].left, 0)
  assert.equal(markers[3].left, 50)
  assert.equal(markers[0].width, 30)
  assert.equal(timelineMarkers(hits, 0).length, 0)
  assert.equal(evidenceKey(hits[0], 3), '0:3000:3')
})

test('time formatting supports minute and hour ranges', () => {
  assert.equal(formatMediaTime(0), '00:00')
  assert.equal(formatMediaTime(125), '02:05')
  assert.equal(formatMediaTime(3661), '01:01:01')
})
