import test from 'node:test'
import assert from 'node:assert/strict'
import { mediaLibraryView, mediaStatusLabel } from './mediaLibraryState.js'

test('media library transitions from loading to list, empty, or retryable error', () => {
  assert.equal(
    mediaLibraryView({ loading: true, error: '', items: [] }),
    'loading',
  )
  assert.equal(
    mediaLibraryView({ loading: false, error: '', items: [{ id: 1 }] }),
    'list',
  )
  assert.equal(
    mediaLibraryView({ loading: false, error: '', items: [] }),
    'empty',
  )
  assert.equal(
    mediaLibraryView({ loading: false, error: 'HTTP 500', items: [] }),
    'error',
  )
  assert.equal(
    mediaLibraryView({ loading: true, error: '', items: [{ id: 1 }] }),
    'list',
  )
  assert.equal(
    mediaLibraryView({
      loading: false,
      error: 'refresh failed',
      items: [{ id: 1 }],
    }),
    'list',
  )
})

test('media status distinguishes backend processing from active analysis and transcription', () => {
  assert.equal(mediaStatusLabel('COMPLETED'), '就绪')
  assert.equal(mediaStatusLabel('PROCESSING'), '处理中')
  assert.equal(mediaStatusLabel('FAILED'), '失败')
  assert.equal(mediaStatusLabel('QUEUED'), '排队中')
  assert.equal(mediaStatusLabel('COMPLETED', 'ai'), '分析中')
  assert.equal(mediaStatusLabel('COMPLETED', 'transcription'), '转录中')
})
