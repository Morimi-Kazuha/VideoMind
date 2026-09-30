import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { ref, computed, nextTick } from 'vue'
import * as api from './api.js'
import * as upload from './chunkUpload.js'
import { createTaskStreams } from './taskEvents.js'
import { useAnalysisWorkspace } from './useAnalysisWorkspace.js'
import { mediaLibraryView, mediaStatusLabel } from './mediaLibraryState.js'
import { DEMO_ITEM } from './demoData.js'

// Execute the real App script's actions without mounting its visual template.
// Only lifecycle/focus watchers are stubbed; async state mutations are original.
const script = (await readFile(new URL('./App.vue', import.meta.url), 'utf8')).split('<script setup>')[1].split('</script>')[0]
  .replace(/^import\s+(?:[\s\S]*?\s+from\s+)?['"][^'"]+['"]\s*;?\s*$/gm, '')
const dependencies = { ...api, ...upload, createTaskStreams, useAnalysisWorkspace, mediaLibraryView, mediaStatusLabel, DEMO_ITEM, ref, computed, nextTick, watch: () => {}, onMounted: () => {}, onUnmounted: () => {} }
const factory = new Function(...Object.keys(dependencies), `${script}\nreturn {fetchList,deleteItem,handleAuth,logout,handleStorage,closeAuthModal,authMode,authForm,authLoading,currentUser,list,listLoading,listError,file,uploadFile,uploadAbort,uploading,uploadProgress,message,dismissMessage,dispose:()=>{clearTimeout(messageTimer);taskStreams.stopAll()}};`)
const applications = []
test.afterEach(() => { for (const app of applications.splice(0)) app.dispose() })
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { resolve, promise } }
const response = data => new Response(JSON.stringify({ code: 0, data, message: '' }), { headers: { 'content-type': 'application/json' } })

function setup() {
  const storage = new Map()
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  globalThis.window = Object.assign(new EventTarget(), { location: { search: '' } })
  globalThis.confirm = () => true
  api.setAuthToken('A')
  const app = factory(...Object.values(dependencies))
  applications.push(app)
  app.currentUser.value = { id: 1 }
  return app
}

test('late media list cannot resurrect media after delete invalidation', async () => {
  const app = setup()
  const late = deferred()
  globalThis.fetch = url => String(url).includes('/media/list') ? late.promise : Promise.resolve(new Response('deleted'))
  app.list.value = [{ id: 1, filename: 'clip.mp4' }]
  const loading = app.fetchList()
  await app.deleteItem(app.list.value[0])
  late.resolve(response([{ id: 1, filename: 'DELETED' }]))
  await loading
  assert.deepEqual(app.list.value, [])
  assert.equal(app.listLoading.value, false)
})

test('latest media list request owns data, error and loading state', async () => {
  const app = setup()
  const first = deferred()
  const second = deferred()
  let calls = 0
  globalThis.fetch = () => (++calls === 1 ? first.promise : second.promise)
  const old = app.fetchList()
  const latest = app.fetchList()
  second.resolve(response([{ id: 2 }]))
  await latest
  first.resolve(new Response('old error', { status: 503 }))
  await old
  assert.deepEqual(app.list.value, [{ id: 2 }])
  assert.equal(app.listError.value, '')
})

test('closing auth modal rejects outstanding login response', async () => {
  const app = setup()
  const late = deferred()
  globalThis.fetch = () => late.promise
  app.authForm.value = { username: 'account', password: 'password' }
  const login = app.handleAuth()
  app.closeAuthModal()
  late.resolve(response({ token: 'B', userInfo: { id: 2 } }))
  await login
  assert.equal(app.currentUser.value.id, 1)
  assert.equal(api.captureAuthSession().token, 'A')
  assert.equal(app.authLoading.value, false)
})

test('old upload finally cannot reset a newer account upload', async () => {
  const app = setup()
  const late = deferred()
  globalThis.fetch = () => late.promise
  app.file.value = Object.assign(new Blob(['video']), { name: 'clip.mp4', lastModified: 1 })
  const uploading = app.uploadFile()
  app.logout()
  api.setAuthToken('B')
  app.currentUser.value = { id: 2 }
  app.uploadAbort.value = new AbortController()
  app.uploading.value = true
  app.uploadProgress.value = { label: 'NEW USER' }
  late.resolve(response({ uploadId: 'OLD' }))
  await uploading
  assert.equal(app.uploading.value, true)
  assert.equal(app.uploadProgress.value.label, 'NEW USER')
})

test('cross-tab login replaces local UI without deleting the new shared user', async () => {
  const app = setup()
  const old = deferred()
  let calls = 0
  globalThis.fetch = () => (++calls === 1 ? old.promise : Promise.resolve(response([{ id: 22 }])))
  const pendingList = app.fetchList()
  localStorage.setItem('user', JSON.stringify({ id: 2, nickname: 'B' }))
  api.setAuthToken('B')
  app.handleStorage({ key: 'authToken' })
  old.resolve(response([{ id: 11 }]))
  await pendingList
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(app.currentUser.value.id, 2)
  assert.equal(JSON.parse(localStorage.getItem('user')).id, 2)
  assert.deepEqual(app.list.value, [{ id: 22 }])
})

test('double upload click retains the first operation and one initialization', async () => {
  const app = setup()
  const late = deferred()
  let calls = 0
  globalThis.fetch = () => { calls++; return late.promise }
  app.file.value = Object.assign(new Blob(['video']), { name: 'clip.mp4', lastModified: 1 })
  const first = app.uploadFile()
  const controller = app.uploadAbort.value
  await app.uploadFile()
  assert.equal(calls, 1)
  assert.equal(app.uploadAbort.value, controller)
  late.resolve(new Response('temporary', { status: 503 }))
  await first
  assert.equal(app.uploading.value, false)
})
