import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope } from 'vue'
import { useAnalysisWorkspace } from './useAnalysisWorkspace.js'

for (const selected of ['GENERAL', 'LEARNING', 'REVIEW', 'CREATION', 'AUTO']) {
  test(`manual submission preserves ${selected} and avoids route HTTP`, async () => {
    const store = new Map([['authToken', 'local-test'], ['user', JSON.stringify({ id: 1 })]])
    globalThis.localStorage = { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key) }
    const requests = []
    globalThis.fetch = async url => {
      requests.push(new URL(String(url), 'http://local'))
      return new Response(JSON.stringify({ code: 0, data: null, message: '' }), { headers: { 'Content-Type': 'application/json' } })
    }
    const scope = effectScope()
    try {
      const w = scope.run(() => useAnalysisWorkspace({ demoMode: false,
        taskStreams: { has: () => false, start: () => {}, stopMedia: () => {} },
        showMessage: () => {}, refreshMediaList: () => {}, findMediaItem: () => ({ id: 42 }) }))
      Object.assign(w.sidebar.value, { mediaId: 42, goal: 'manual goal', analysisMode: selected })
      await w.submitAgent()
      const submissions = requests.filter(r => r.pathname === '/analysis/ai')
      assert.equal(submissions.length, 1)
      const concrete = selected === 'AUTO' ? 'GENERAL' : selected
      assert.equal(submissions[0].searchParams.get('mode'), concrete)
      assert.equal(w.sidebar.value.analysisMode, concrete)
      assert.equal(requests.some(r => r.pathname === '/analysis/route'), false)
    } finally { scope.stop() }
  })
}
