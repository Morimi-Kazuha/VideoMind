import assert from 'node:assert/strict'
import test from 'node:test'
import { apiRequest, captureAuthSession, clearAuthToken, setAuthToken } from './api.js'

test('API network failures produce an actionable message', async () => {
  globalThis.localStorage = { getItem: () => null }
  globalThis.fetch = async () => {
    throw new TypeError('fetch failed')
  }

  await assert.rejects(apiRequest('/health'), /请确认后端已启动且地址配置正确/)
})

function authEnvironment() {
  const storage = new Map()
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  globalThis.window = new EventTarget()
  let resolve
  globalThis.fetch = () => new Promise(done => { resolve = done })
  let expired = 0
  window.addEventListener('auth-expired', () => expired++)
  return { storage, resolve: value => resolve(value), expired: () => expired }
}

test('stale 401 cannot log out a newer login even when the token text repeats', async () => {
  const env = authEnvironment()
  setAuthToken('A')
  const request = apiRequest('/media/list')
  clearAuthToken()
  setAuthToken('A')
  env.resolve(new Response('expired', { status: 401 }))
  await assert.rejects(request, { name: 'AbortError' })
  assert.equal(env.storage.get('authToken'), 'A')
  assert.equal(env.expired(), 0)
})

test('current authenticated 401 expires current session once', async () => {
  const env = authEnvironment()
  setAuthToken('A')
  const request = apiRequest('/media/list')
  env.resolve(new Response('expired', { status: 401 }))
  assert.equal((await request).status, 401)
  assert.equal(env.storage.has('authToken'), false)
  assert.equal(env.expired(), 1)
})

test('late 401 JSON body cannot survive a new login after expiration', async () => {
  const env = authEnvironment()
  let resolveBody
  let bodyStarted
  const started = new Promise(resolve => { bodyStarted = resolve })
  const body = new Promise(resolve => { resolveBody = resolve })
  setAuthToken('A')
  const request = apiRequest('/media/list')
  env.resolve({ status: 401, headers: new Headers({ 'content-type': 'application/json' }),
    clone: () => ({ json: () => { bodyStarted(); return body } }) })
  await started
  setAuthToken('B')
  resolveBody({ code: 401, message: 'old expiration', data: null })
  await assert.rejects(request, { name: 'AbortError' })
  assert.equal(env.storage.get('authToken'), 'B')
  assert.equal(env.expired(), 1)
})

test('logout and cross-tab token change invalidate outstanding successful responses', async () => {
  for (const mutate of [() => clearAuthToken(), env => env.storage.set('authToken', 'B')]) {
    const env = authEnvironment()
    setAuthToken('A')
    const session = captureAuthSession()
    const request = apiRequest('/media/list')
    mutate(env)
    env.resolve(new Response('[]', { headers: { 'content-type': 'application/json' } }))
    await assert.rejects(request, { name: 'AbortError' })
    assert.equal(session.isCurrent(), false)
  }
})


