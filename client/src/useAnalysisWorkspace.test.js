import test from 'node:test'
import assert from 'node:assert/strict'
import { useAnalysisWorkspace } from './useAnalysisWorkspace.js'
import { DEMO_ITEM } from './demoData.js'

test('media selection, AI answer, evidence search, and return preserve one workspace identity', async () => {
  const storage = new Map()
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  }
  const workspace = useAnalysisWorkspace({
    demoMode: true,
    taskStreams: { has: () => false, stopMedia: () => {} },
    showMessage: () => {},
    refreshMediaList: () => {},
    findMediaItem: (id) => (id === DEMO_ITEM.id ? DEMO_ITEM : null),
  })
  await workspace.openAgent(DEMO_ITEM)
  assert.equal(workspace.sidebar.value.visible, true)
  assert.equal(workspace.sidebar.value.mediaId, DEMO_ITEM.id)
  assert.equal(workspace.sidebar.value.mode, 'compose')

  workspace.sidebar.value.goal = '分析二叉树遍历'
  await workspace.submitAgent()
  assert.equal(workspace.sidebar.value.loading, true)
  await new Promise((resolve) => setTimeout(resolve, 475))
  assert.equal(workspace.sidebar.value.loading, false)
  assert.match(workspace.sidebar.value.content, /二叉树遍历/)

  workspace.sidebar.value.evidenceQuery = '迭代遍历'
  await workspace.searchEvidence()
  assert.equal(workspace.sidebar.value.evidenceResults[0].startMs, 522000)
  assert.equal(workspace.sidebar.value.evidenceResults[0].ocrTexts.length, 1)

  workspace.closeSidebar()
  assert.equal(workspace.sidebar.value.visible, false)
  assert.equal(storage.get(`dovideo:goal:${DEMO_ITEM.id}`), '分析二叉树遍历')
})

test('a failed prior analysis remains recoverable without losing selected media', async () => {
  const originalFetch = globalThis.fetch
  globalThis.localStorage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  }
  globalThis.fetch = async (url) => {
    const data = String(url).startsWith('/media/playback')
      ? '/media/playback-file/1001?access_token=test'
      : { state: 'FAILED', result: null, message: '分析任务失败，请重新提交' }
    return new Response(JSON.stringify({ code: 0, message: '', data }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  try {
    const workspace = useAnalysisWorkspace({
      demoMode: false,
      taskStreams: { has: () => false, stopMedia: () => {} },
      showMessage: () => {},
      refreshMediaList: () => {},
      findMediaItem: () => DEMO_ITEM,
    })
    await workspace.openAgent(DEMO_ITEM)
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(workspace.sidebar.value.mediaId, DEMO_ITEM.id)
    assert.equal(workspace.sidebar.value.mode, 'compose')
    assert.equal(workspace.sidebar.value.error, '分析任务失败，请重新提交')
    assert.match(workspace.sidebar.value.playbackUrl, /playback-file/)
  } finally {
    globalThis.fetch = originalFetch
  }
})
