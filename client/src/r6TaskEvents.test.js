import test from 'node:test'
import assert from 'node:assert/strict'
import { consumeStream, createTaskStreams } from './taskEvents.js'
import { setAuthToken } from './api.js'

const frame = event => new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)
const flush = () => new Promise(resolve => setImmediate(resolve))

test('cancelled SSE discards buffered frames and cancels/releases reader', async () => {
  let finishRead
  let cancelled = 0
  let released = 0
  const reader = { read: () => new Promise(resolve => { finishRead = resolve }), cancel: async () => { cancelled++ }, releaseLock: () => { released++ } }
  const controller = new AbortController()
  const events = []
  const consuming = consumeStream({ getReader: () => reader }, event => events.push(event), controller.signal)
  controller.abort()
  finishRead({ value: frame({ state: 'PROCESSING' }), done: false })
  await consuming
  assert.deepEqual(events, [])
  assert.ok(cancelled > 0)
  assert.equal(released, 1)
})

test('terminal SSE finishes and cancels a socket that never sends EOF', async () => {
  let cancelled = false
  const body = new ReadableStream({ start(controller) { controller.enqueue(frame({ state: 'COMPLETED', result: 'done' })); controller.enqueue(frame({ state: 'PROCESSING' })) }, cancel() { cancelled = true } })
  const events = []
  assert.equal(await consumeStream(body, event => events.push(event), new AbortController().signal), true)
  assert.equal(cancelled, true)
  assert.deepEqual(events.map(event => event.state), ['COMPLETED'])
  assert.equal(body.locked, false)
})

test('login switch automatically stops old SSE and cannot reconnect as the new user', async () => {
  const storage = new Map()
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  setAuthToken('A')
  let resolve
  let calls = 0
  globalThis.fetch = () => { calls++; return new Promise(done => { resolve = done }) }
  const events = []
  const streams = createTaskStreams()
  streams.start(1, 'ai', 'goal', '/events', event => events.push(event))
  setAuthToken('B')
  resolve(new Response(new ReadableStream({ start(controller) { controller.enqueue(frame({ state: 'COMPLETED' })); controller.close() } })))
  await flush()
  assert.equal(streams.hasMedia(1), false)
  assert.equal(calls, 1)
  assert.deepEqual(events, [])
})

test('replacing same stream prevents old pending fetch from rejoining the map', async () => {
  globalThis.localStorage = { getItem: () => null }
  const resolvers = []
  globalThis.fetch = () => new Promise(resolve => resolvers.push(resolve))
  const events = []
  const streams = createTaskStreams()
  streams.start(1, 'ai', 'goal', '/events', () => events.push('old'))
  streams.start(1, 'ai', 'goal', '/events', () => events.push('new'))
  for (const resolve of resolvers) resolve(new Response(new ReadableStream({ start(controller) { controller.enqueue(frame({ state: 'COMPLETED' })); controller.close() } })))
  await flush()
  await flush()
  assert.deepEqual(events, ['new'])
  assert.equal(streams.hasMedia(1), false)
})

test('reader cleanup failures preserve the original read error', async () => {
  const failure = new Error('read failed')
  let releases = 0
  const reader = { read: async () => { throw failure }, cancel: () => { throw new Error('cancel failed') }, releaseLock: () => { releases++; throw new Error('release failed') } }
  await assert.rejects(consumeStream({ getReader: () => reader }, () => {}, new AbortController().signal), error => error === failure)
  assert.equal(releases, 1)
})

test('terminal delivery does not await a pending reader cancel', async () => {
  const events = []
  let releases = 0
  const reader = { read: async () => ({ value: frame({ state: 'FAILED' }), done: false }), cancel: () => new Promise(() => {}), releaseLock: () => { releases++ } }
  assert.equal(await consumeStream({ getReader: () => reader }, event => events.push(event), new AbortController().signal), true)
  assert.deepEqual(events.map(event => event.state), ['FAILED'])
  assert.equal(releases, 1)
})

test('terminal handler failure releases the stream and cannot reconnect', async () => {
  globalThis.localStorage = { getItem: () => null }
  let calls = 0
  globalThis.fetch = async () => { calls++; return new Response(new ReadableStream({ start(controller) { controller.enqueue(frame({ state: 'COMPLETED' })) } })) }
  const errors = []
  const streams = createTaskStreams()
  streams.start(1, 'ai', 'goal', '/events', () => { throw new Error('handler failed') }, error => errors.push(error))
  await flush()
  assert.equal(streams.hasMedia(1), false)
  assert.equal(calls, 1)
  assert.deepEqual(errors, [])
})

test('late HTTP error body cannot notify after replacement', async () => {
  globalThis.localStorage = { getItem: () => null }
  let finishBody, bodyStarted
  const started = new Promise(resolve => { bodyStarted = resolve })
  const body = new Promise(resolve => { finishBody = resolve })
  let calls = 0
  globalThis.fetch = async () => ++calls === 1
    ? { status: 403, ok: false, headers: new Headers(), text: () => { bodyStarted(); return body } }
    : new Response(new ReadableStream({ start(controller) { controller.enqueue(frame({ state: 'COMPLETED' })); controller.close() } }))
  const errors = [], events = []
  const streams = createTaskStreams()
  streams.start(1, 'ai', 'goal', '/events', () => {}, error => errors.push(error))
  await started
  streams.start(1, 'ai', 'goal', '/events', event => events.push(event))
  finishBody('old forbidden')
  await flush()
  assert.deepEqual(errors, [])
  assert.equal(events.length, 1)
  assert.equal(streams.hasMedia(1), false)
})
