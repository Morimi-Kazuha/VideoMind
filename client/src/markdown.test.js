import test from 'node:test'
import assert from 'node:assert/strict'
import {
  linkVideoTimestamps,
  localizeAnalysisLabels,
  stripPrivateReasoning,
} from './markdown.js'

test('backend evidence citation ranges link to the actual cited moment', () => {
  assert.equal(
    linkVideoTimestamps('- [02:05 in 02:00–02:10] ASR: 原文'),
    '- [02:05 in 02:00–02:10](#video-t=125) ASR: 原文',
  )
  assert.equal(
    linkVideoTimestamps('[01:02:03] OCR'),
    '[01:02:03](#video-t=3723) OCR',
  )
  assert.equal(
    linkVideoTimestamps('[02:05](https://example.com)'),
    '[02:05](https://example.com)',
  )
})

test('private reasoning never becomes visible when no public answer remains', () => {
  assert.equal(stripPrivateReasoning('<think>hidden</think>'), '')
  assert.equal(stripPrivateReasoning('<think>hidden'), '')
  assert.equal(
    stripPrivateReasoning('<think>hidden</think>公开结论'),
    '公开结论',
  )
})

test('fixed backend report labels render in Chinese without changing answer text', () => {
  assert.equal(
    localizeAnalysisLabels(
      'Source: `video.mp4`\n## Conclusions\n- finding\n## Evidence',
    ),
    '源视频： `video.mp4`\n## 核心结论\n- finding\n## 视频证据',
  )
})
