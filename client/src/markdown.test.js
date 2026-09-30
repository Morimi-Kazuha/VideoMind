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
    '<ul>\n<li><a href="#video-t=125">[02:05 in 02:00–02:10]</a> ASR: 原文</li>\n</ul>\n',
  )
  assert.equal(
    linkVideoTimestamps('[01:02:03] OCR'),
    '<p><a href="#video-t=3723">[01:02:03]</a> OCR</p>\n',
  )
  assert.equal(
    linkVideoTimestamps('[02:05](https://example.com)'),
    '<p><a href="https://example.com">02:05</a></p>\n',
  )
})

test('timestamps transform only prose tokens including nested lists and tables', () => {
  const html = linkVideoTimestamps('段落 [01:02]\n\n**加粗 [123:45]**\n\n- *嵌套 [2:03:04]*\n\n| 时刻 |\n| --- |\n| [00:03] |')
  for (const seconds of [62, 7425, 7384, 3]) assert.match(html, new RegExp(`#video-t=${seconds}`))
  assert.match(html, /<table>/)
})

test('code, escaped text, markdown links/images, raw HTML anchors and pre remain literal', () => {
  const cases = ['`[01:02]`', '```\n[01:02]\n```', '[01:02](https://example.com)', '![01:02](image.png)', '\\[01:02]', '<a href="/x">[01:02]</a>', '<pre>[01:02]</pre>', '<code>[01:02]</code>']
  for (const input of cases) assert.doesNotMatch(linkVideoTimestamps(input), /#video-t=/, input)
})

test('invalid timestamp fields and unsafe integers never become seek links', () => {
  for (const input of ['[01:99]', '[1:60:00]', '[999999999999999999999:00]', '[1:23:99]']) {
    assert.doesNotMatch(linkVideoTimestamps(input), /#video-t=/)
  }
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
