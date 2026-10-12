import test from 'node:test'
import assert from 'node:assert/strict'
import { linkVideoTimestamps } from './markdown.js'

test('MIXED source headings retain original citation seek target and inference stays separate', () => {
  const html = linkVideoTimestamps('## 视频事实\n\n算法只遍历一次。\n\n## 视频证据\n\n- [00:01 in 00:00–00:10] ASR: 算法只遍历一次。\n\n## 通用知识与推理\n\n通用知识：单次遍历通常是线性复杂度。\n\n推论：仍需检查其他操作。')
  assert.match(html, /<h2>视频事实<\/h2>/)
  assert.match(html, /href="#video-t=1"/)
  const knowledge = html.split('<h2>通用知识与推理</h2>')[1]
  assert.match(knowledge, /通用知识：/)
  assert.match(knowledge, /推论：/)
  assert.doesNotMatch(knowledge, /#video-t=/)
})
