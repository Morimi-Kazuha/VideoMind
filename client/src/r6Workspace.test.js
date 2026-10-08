import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { useAnalysisWorkspace } from './useAnalysisWorkspace.js'
import { setAuthToken, captureAuthSession } from './api.js'

const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const response = data => new Response(JSON.stringify({ code: 0, message: '', data }), { headers: { 'content-type': 'application/json' } })
const flush = () => new Promise(resolve => setImmediate(resolve))

test('actual component deferred timestamp seek loses ownership on workspace or player replacement', () => {
  const source = readFileSync(new URL('./AnalysisWorkspace.vue', import.meta.url), 'utf8')
  const start = source.indexOf('function seekVideo(seconds) {')
  const end = source.indexOf('\nfunction selectEvidence', start)
  assert.ok(start > 0 && end > start)
  const callbacks = []
  const oldPlayer = { readyState: 0, duration: 100, currentTime: 0, addEventListener: (_, callback) => callbacks.push(callback), play: () => Promise.resolve() }
  const videoPlayer = { value: oldPlayer }, currentTime = { value: 0 }
  const props = { sidebar: { generation: 1, visible: true }, actions: { showMessage: () => {} } }
  globalThis.localStorage = { getItem: () => 'seek-session' }
  // Evaluate the production function, injecting only its Vue refs and auth port.
  const seek = Function('props', 'videoPlayer', 'currentTime', 'captureAuthSession', `${source.slice(start, end)}; return seekVideo`)(props, videoPlayer, currentTime, captureAuthSession)
  seek(12)
  oldPlayer.readyState = 1
  callbacks.shift()()
  assert.equal(oldPlayer.currentTime, 12)
  oldPlayer.readyState = 0
  seek(24)
  props.sidebar = { generation: 2, visible: true }
  videoPlayer.value = { ...oldPlayer, readyState: 1, currentTime: 0 }
  callbacks.shift()()
  assert.equal(videoPlayer.value.currentTime, 0)
  assert.equal(currentTime.value, 12)
})

async function setup(overrides = {}) {
  const storage = new Map()
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  const streams = []
  const messages = []
  const pending = new Map()
  globalThis.fetch = url => {
    const path = String(url).split('?')[0]
    if (pending.has(path)) return pending.get(path).promise
    return Promise.resolve(response(path === '/analysis/analysis-status' ? { state: 'COMPLETED', result: 'V1' } : path === '/media/playback' ? '/playback' : null))
  }
  const workspace = useAnalysisWorkspace({ demoMode: false, taskStreams: { has: () => false, start: (...args) => streams.push(args), stop: () => {}, stopMedia: () => {} }, showMessage: (...args) => messages.push(args), refreshMediaList: async () => {}, findMediaItem: () => ({ id: 1 }), ...overrides })
  await workspace.openAgent({ id: 1, filename: 'one.mp4' })
  await flush()
  return { workspace, pending, streams, messages }
}

for (const operation of ['followUp', 'feedback', 'evidence']) {
  test(`late ${operation} cannot mutate same media/goal/mode revision`, async () => {
    const { workspace: w, pending } = await setup()
    const endpoint = { followUp: '/analysis/follow-up', feedback: '/analysis/agent-feedback', evidence: '/analysis/evidence-search' }[operation]
    const late = deferred()
    pending.set(endpoint, late)
    w.sidebar.value.followUp = '问题'
    w.sidebar.value.evidenceQuery = '证据'
    const operationPromise = operation === 'followUp' ? w.submitFollowUp() : operation === 'feedback' ? w.sendFeedback(1) : w.searchEvidence()
    const previousGeneration = w.sidebar.value.generation
    w.sidebar.value.planDraft = ['新计划']
    await w.rerunWithPlan()
    assert.ok(w.sidebar.value.generation > previousGeneration)
    w.sidebar.value.content = 'V2'
    late.resolve(response(operation === 'followUp' ? 'OLD' : operation === 'evidence' ? [{ snippet: 'OLD' }] : null))
    await operationPromise
    assert.equal(w.sidebar.value.content, 'V2')
    assert.equal(w.sidebar.value.feedback, null)
    assert.deepEqual(w.sidebar.value.evidenceResults, [])
    assert.equal(w.sidebar.value.followUpLoading, false)
  })
}

test('stale evidence and late HTTP error cannot affect another media workspace', async () => {
  const { workspace: w, pending, messages } = await setup()
  const late = deferred()
  pending.set('/analysis/evidence-search', late)
  w.sidebar.value.evidenceQuery = 'query'
  const request = w.searchEvidence()
  await w.openAgent({ id: 2, filename: 'two.mp4' })
  late.resolve(new Response('OLD error', { status: 503 }))
  await request
  assert.equal(w.sidebar.value.mediaId, 2)
  assert.equal(w.sidebar.value.evidenceError, '')
  assert.deepEqual(messages, [])
})

test('old SSE callback cannot mutate a reopened same-key workspace', async () => {
  const { workspace: w, streams } = await setup()
  w.startNewAnalysis()
  await w.submitAgent()
  const callback = streams.at(-1)[4]
  w.closeSidebar()
  await w.openAgent({ id: 1, filename: 'one.mp4' })
  w.sidebar.value.content = 'REOPENED'
  await callback({ state: 'COMPLETED', result: 'OLD STREAM' })
  assert.equal(w.sidebar.value.content, 'REOPENED')
})

test('late metadata JSON cannot overwrite new revision', async () => {
  const { workspace: w, pending, streams } = await setup()
  w.startNewAnalysis()
  await w.submitAgent()
  const late = deferred()
  pending.set('/analysis/agent-plan', late)
  const callback = streams.at(-1)[4]({ state: 'PROCESSING', stage: 'CRITIC_STARTED' })
  w.sidebar.value.planDraft = ['V2']
  await w.rerunWithPlan()
  late.resolve(response({ tasks: ['OLD'] }))
  await callback
  await flush()
  assert.deepEqual(w.sidebar.value.plan.tasks, ['V2'])
})

test('logout/login switch rejects follow-up response even with unchanged workspace identity', async () => {
  const { workspace: w, pending } = await setup()
  setAuthToken('A')
  await w.openAgent({ id: 1, filename: 'one.mp4' })
  const late = deferred()
  pending.set('/analysis/follow-up', late)
  w.sidebar.value.followUp = 'query'
  const request = w.submitFollowUp()
  setAuthToken('B')
  late.resolve(response('OLD'))
  await request
  assert.equal(w.sidebar.value.content, '')
  assert.equal(w.sidebar.value.visible, false)
})

test('double rerun cannot invalidate the accepted revision response', async () => {
  const { workspace: w, pending, streams } = await setup()
  const late = deferred()
  pending.set('/analysis/agent-revise', late)
  w.sidebar.value.planDraft = ['V2']
  const first = w.rerunWithPlan()
  const generation = w.sidebar.value.generation
  await w.rerunWithPlan()
  assert.equal(w.sidebar.value.generation, generation)
  late.resolve(response(null))
  await first
  assert.equal(streams.length, 1)
  assert.equal(w.sidebar.value.loading, true)
  assert.equal(w.sidebar.value.rerunLoading, false)
})

test('analysis generation changes do not strand media playback loading', async () => {
  const { workspace: w, pending } = await setup()
  const late = deferred()
  pending.set('/media/playback', late)
  await w.openAgent({ id: 1, filename: 'one.mp4' })
  assert.equal(w.sidebar.value.playbackLoading, true)
  w.startNewAnalysis()
  await w.submitAgent()
  late.resolve(response('/current-video'))
  await flush()
  assert.equal(w.sidebar.value.playbackUrl, '/current-video')
  assert.equal(w.sidebar.value.playbackLoading, false)
})

test('old playback finally cannot clear the new media request loading', async () => {
  const { workspace: w, pending } = await setup()
  const first = deferred(), second = deferred()
  pending.set('/media/playback', first)
  await w.openAgent({ id: 1, filename: 'one.mp4' })
  pending.set('/media/playback', second)
  await w.openAgent({ id: 2, filename: 'two.mp4' })
  first.resolve(response('/old-video'))
  await flush()
  assert.equal(w.sidebar.value.playbackLoading, true)
  assert.equal(w.sidebar.value.playbackUrl, '')
  second.resolve(response('/new-video'))
  await flush()
  assert.equal(w.sidebar.value.playbackLoading, false)
  assert.equal(w.sidebar.value.playbackUrl, '/new-video')
})

test('pending metadata does not block terminal state or release', async () => {
  const stops = []
  const { workspace: w, pending, streams } = await setup({ taskStreams: { has: () => false, start: (...args) => streams.push(args), stop: (...args) => stops.push(args), stopMedia: () => {} } })
  w.startNewAnalysis()
  await w.submitAgent()
  const late = deferred()
  pending.set('/analysis/agent-plan', late)
  const onEvent = streams.at(-1)[4]
  await onEvent({ state: 'PROCESSING', stage: 'CRITIC_STARTED' })
  await onEvent({ state: 'COMPLETED', result: 'FINAL' })
  assert.equal(w.sidebar.value.loading, false)
  assert.equal(w.sidebar.value.content, 'FINAL')
  assert.equal(stops.length, 1)
  late.resolve(response({ tasks: ['final plan'] }))
  await flush()
})

test('closed workspace keeps background completion notification and list refresh', async () => {
  let refreshed = 0
  const { workspace: w, streams, messages } = await setup({ refreshMediaList: async () => { refreshed++ }, findMediaItem: () => ({ filename: 'one.mp4' }) })
  w.startNewAnalysis()
  await w.submitAgent()
  const onEvent = streams.at(-1)[4]
  w.closeSidebar()
  await onEvent({ state: 'COMPLETED', result: 'background' })
  assert.equal(w.sidebar.value.visible, false)
  assert.notEqual(w.sidebar.value.content, 'background')
  assert.equal(refreshed, 1)
  assert.match(messages.at(-1)[0], /完成.*one\.mp4/)
})

test('failed transcription retains the prior text without reporting new success', async () => {
  const { workspace: w, pending } = await setup()
  pending.set('/analysis/transcription-status', { promise: Promise.resolve(response({ state: 'FAILED', result: 'OLD TRANSCRIPT', message: 'new extraction failed' })) })
  await w.transcribe(1)
  assert.equal(w.sidebar.value.content, 'OLD TRANSCRIPT')
  assert.equal(w.sidebar.value.error, 'new extraction failed')
  assert.equal(w.sidebar.value.loading, false)
})
