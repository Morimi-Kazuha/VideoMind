import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope } from 'vue'
import { useAnalysisWorkspace } from './useAnalysisWorkspace.js'
import { setAuthToken } from './api.js'
import { linkVideoTimestamps } from './markdown.js'

const response = (data, status = 200) => new Response(JSON.stringify({ code: status === 200 ? 0 : status, message: status === 200 ? '' : '会话暂不可用', data }), { status, headers: { 'content-type': 'application/json' } })
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

function setup() {
  const storage = new Map([['authToken', 'account-A'], ['user', JSON.stringify({ id: 1 })]])
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key), key: index => [...storage.keys()][index], get length() { return storage.size } }
  const histories = new Map(), posts = [], pending = new Map()
  let failPost = false, failHistory = false
  globalThis.fetch = async (url, options) => {
    const path = String(url).split('?')[0]
    const params = new URL(String(url), 'http://local').searchParams
    if (pending.has(path)) return pending.get(path).promise
    if (path === '/analysis/analysis-status') return response({ state: 'COMPLETED', result: '原始分析 [00:01] ASR' })
    if (path === '/media/playback') return response('/playback')
    if (path === '/analysis/follow-up/history') {
      if (failHistory) return response(null, 503)
      return response({ turns: histories.get(params.get('conversationId')) || [], summary: null, sourceRevision: 'r1' })
    }
    if (path === '/analysis/follow-up') {
      const values = Object.fromEntries(params)
      posts.push(values)
      if (failPost) throw new Error('connection lost')
      const answer = 'Redis 使用内存。\n\n视频证据\n- [00:00–00:10] ASR：“Redis 使用内存。”'
      const turns = histories.get(values.conversationId) || []
      if (!turns.some(turn => turn.turn_id === values.requestId))
        turns.push({ turn_id: values.requestId, question: values.question, answer })
      histories.set(values.conversationId, turns)
      return response(answer)
    }
    return response(null)
  }
  const scopes = []
  const create = () => {
    const scope = effectScope()
    scopes.push(scope)
    return scope.run(() => useAnalysisWorkspace({ demoMode: false, taskStreams: { has: () => false, start: () => {}, stopMedia: () => {} }, showMessage: () => {}, refreshMediaList: () => {}, findMediaItem: () => ({ id: 42 }) }))
  }
  return { create, storage, histories, posts, pending, failPost: value => { failPost = value }, failHistory: value => { failHistory = value }, dispose: () => scopes.forEach(scope => scope.stop()) }
}
const open = workspace => workspace.openAgent({ id: 42, filename: 'memory.mp4' })
const ask = async (workspace, text = 'Redis 的特点是什么？') => { workspace.sidebar.value.followUp = text; await workspace.submitFollowUp() }

test('close reopen and fresh workspace restore server history once and keep timestamp links', async () => {
  const h = setup()
  try {
    const w = h.create()
    await open(w)
    const id = w.sidebar.value.conversationId
    await ask(w)
    w.closeSidebar()
    await open(w)
    assert.equal(w.sidebar.value.conversationId, id)
    assert.equal(w.sidebar.value.content.split('## 追问').length - 1, 1)
    await w.restoreConversation()
    assert.equal(w.sidebar.value.content.split('## 追问').length - 1, 1)
    const refreshed = h.create()
    await open(refreshed)
    assert.equal(refreshed.sidebar.value.conversationId, id)
    assert.match(refreshed.sidebar.value.content, /Redis 使用内存/)
    assert.match(linkVideoTimestamps(refreshed.sidebar.value.content), /#video-t=0/)
    const saved = [...h.storage].filter(([key]) => key.startsWith('videomind:conversation:'))
    assert.equal(saved.length, 1)
    assert.equal(saved[0][1], id)
    assert.ok(!JSON.stringify(saved).includes('Redis 使用内存'))
  } finally { h.dispose() }
})

test('new conversation rejects delayed answer and starts empty over the original analysis', async () => {
  const h = setup()
  try {
    const w = h.create()
    await open(w)
    await ask(w)
    const oldId = w.sidebar.value.conversationId
    const late = deferred()
    h.pending.set('/analysis/follow-up', late)
    w.sidebar.value.followUp = '它呢？'
    const request = w.submitFollowUp()
    w.startNewConversation()
    assert.notEqual(w.sidebar.value.conversationId, oldId)
    late.resolve(response('迟到旧回答'))
    await request
    assert.equal(w.sidebar.value.content, '原始分析 [00:01] ASR')
    assert.equal(w.sidebar.value.followUpLoading, false)
  } finally { h.dispose() }
})

test('account switch clears old visible history and uses another user scope', async () => {
  const h = setup()
  try {
    const w = h.create()
    await open(w)
    await ask(w)
    const oldId = w.sidebar.value.conversationId
    h.storage.set('user', JSON.stringify({ id: 2 }))
    setAuthToken('account-B')
    assert.equal(w.sidebar.value.content, '')
    assert.equal(w.sidebar.value.visible, false)
    await open(w)
    assert.notEqual(w.sidebar.value.conversationId, oldId)
    assert.equal(w.sidebar.value.content, '原始分析 [00:01] ASR')
  } finally { h.dispose() }
})

test('goal concrete mode and media each choose isolated conversation identifiers', async () => {
  const h = setup()
  try {
    const w = h.create()
    await open(w)
    const ids = [w.sidebar.value.conversationId]
    for (const patch of [{ goal: '不同目标' }, { analysisMode: 'REVIEW' }, { mediaId: 43 }]) {
      Object.assign(w.sidebar.value, patch)
      await w.restoreConversation()
      ids.push(w.sidebar.value.conversationId)
    }
    assert.equal(new Set(ids).size, 4)
    assert.ok(h.storage.has(`videomind:conversation:1:43:REVIEW:${encodeURIComponent('不同目标')}`))
    w.sidebar.value.analysisMode = 'AUTO'
    const count = h.storage.size
    await w.restoreConversation()
    assert.equal(h.storage.size, count)
  } finally { h.dispose() }
})

test('late restoration cannot overwrite a newer conversation or a newly appended answer', async () => {
  const h = setup()
  try {
    const w = h.create()
    await open(w)
    const late = deferred()
    h.pending.set('/analysis/follow-up/history', late)
    const restore = w.restoreConversation()
    w.startNewConversation()
    late.resolve(response({ turns: [{ question: 'old', answer: '旧历史' }] }))
    await restore
    assert.equal(w.sidebar.value.content, '原始分析 [00:01] ASR')
    assert.equal(w.sidebar.value.conversationHistoryLoading, false)
  } finally { h.dispose() }
})

test('lost response retry reuses request UUID and temporary history failures preserve conversation ID', async () => {
  const h = setup()
  try {
    const w = h.create()
    await open(w)
    const id = w.sidebar.value.conversationId
    h.failPost(true)
    await ask(w)
    h.failPost(false)
    await w.submitFollowUp()
    assert.equal(h.posts[0].requestId, h.posts[1].requestId)
    assert.equal(h.histories.get(id).length, 1)
    h.failHistory(true)
    await w.restoreConversation()
    assert.equal(w.sidebar.value.conversationId, id)
    assert.match(w.sidebar.value.conversationError, /暂不可用/)
  } finally { h.dispose() }
})

test('delete removes only current user and media conversation identifiers', async () => {
  const h = setup()
  try {
    const w = h.create()
    await open(w)
    h.storage.set('videomind:conversation:2:42:GENERAL:other', 'other-user')
    w.discardMediaWorkspace(42)
    assert.ok(![...h.storage.keys()].some(key => key.startsWith('videomind:conversation:1:42:')))
    assert.equal(h.storage.get('videomind:conversation:2:42:GENERAL:other'), 'other-user')
  } finally { h.dispose() }
})
