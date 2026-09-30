import test from 'node:test'
import assert from 'node:assert/strict'
import { uploadVideoInChunks, hasUploadProgress, validateVideoFile } from './chunkUpload.js'
import { setAuthToken } from './api.js'

const file = () => Object.assign(new Blob(['video']), { name: 'clip.mp4', lastModified: 7 })
const legacy = 'upload:clip.mp4:5:7'
const scoped = user => `upload:${user}:clip.mp4:5:7`
const response = (data, status = 200, message = '') => new Response(JSON.stringify({ code: status < 400 ? 0 : status, data, message }), { status, headers: { 'content-type': 'application/json' } })
function storageEnvironment(entries = []) {
  const storage = new Map(entries)
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  return storage
}

test('upload credentials are user scoped and account B never resumes scoped A credential', async () => {
  const storage = storageEnvironment([[scoped(1), 'A']])
  const paths = []
  globalThis.fetch = async url => {
    paths.push(String(url))
    return response(String(url).includes('init-upload') ? { uploadId: 'B' } : String(url).includes('complete-upload') ? { id: 22 } : {})
  }
  assert.equal(hasUploadProgress(file(), 2), false)
  assert.equal((await uploadVideoInChunks(file(), () => {}, undefined, 2)).id, 22)
  assert.equal(storage.get(scoped(1)), 'A')
  assert.ok(paths.every(path => !path.includes('uploadId=A')))
})

test('legacy credential migrates only after server ownership verification', async () => {
  const storage = storageEnvironment([[legacy, 'A']])
  globalThis.fetch = async url => {
    if (String(url).includes('upload-status')) {
      assert.equal(storage.has(scoped(1)), false)
      return response({ uploadedChunks: [0], completedMediaId: null })
    }
    assert.equal(storage.get(scoped(1)), 'A')
    assert.equal(storage.has(legacy), false)
    return response(null, 503, 'temporary')
  }
  await assert.rejects(uploadVideoInChunks(file(), () => {}, undefined, 1), /temporary/)
  assert.equal(storage.get(scoped(1)), 'A')
})

test('legacy ownership failure preserves original record and uses a fresh user scoped session', async () => {
  const storage = storageEnvironment([[legacy, 'A']])
  const paths = []
  globalThis.fetch = async url => {
    paths.push(String(url))
    if (String(url).includes('upload-status')) return response(null, 403, 'not owner')
    return response(String(url).includes('init-upload') ? { uploadId: 'B' } : { id: 22 })
  }
  assert.equal((await uploadVideoInChunks(file(), () => {}, undefined, 2)).id, 22)
  assert.equal(storage.get(legacy), 'A')
  assert.equal(storage.has(scoped(2)), false)
  assert.ok(paths.filter(path => !path.includes('upload-status')).every(path => !path.includes('uploadId=A')))
})

test('failed scoped storage migration retains legacy resumability', async () => {
  const storage = storageEnvironment([[legacy, 'A']])
  globalThis.localStorage.setItem = () => { throw new Error('quota') }
  globalThis.fetch = async url => response(String(url).includes('upload-status') ? { uploadedChunks: [0] } : null, String(url).includes('upload-status') ? 200 : 503, 'temporary')
  await assert.rejects(uploadVideoInChunks(file(), () => {}, undefined, 1))
  assert.equal(storage.get(legacy), 'A')
})

for (const fault of ['5xx', 'network']) {
  test(`temporary upload-status ${fault} preserves credentials`, async () => {
    const storage = storageEnvironment([[scoped(1), 'A']])
    globalThis.fetch = async () => { if (fault === 'network') throw new TypeError('offline'); return response(null, 503, 'temporary') }
    await assert.rejects(uploadVideoInChunks(file(), () => {}, undefined, 1))
    assert.equal(storage.get(scoped(1)), 'A')
  })
}

test('lost completion response recovers completed media without reupload or duplicate init', async () => {
  const storage = storageEnvironment()
  let chunkCalls = 0
  let initCalls = 0
  let completeCalls = 0
  globalThis.fetch = async url => {
    if (String(url).includes('init-upload')) { initCalls++; return response({ uploadId: 'A' }) }
    if (String(url).includes('upload-chunk')) { chunkCalls++; return response({ uploadedChunks: [0] }) }
    if (String(url).includes('upload-status')) return response({ uploadedChunks: [], completedMediaId: 42 })
    if (++completeCalls === 1) throw new TypeError('response lost after commit')
    return response({ id: 42 })
  }
  await assert.rejects(uploadVideoInChunks(file(), () => {}, undefined, 1))
  assert.equal(storage.get(scoped(1)), 'A')
  assert.equal((await uploadVideoInChunks(file(), () => {}, undefined, 1)).id, 42)
  assert.equal(initCalls, 1)
  assert.equal(chunkCalls, 1)
  assert.equal(completeCalls, 2)
})

test('account switch prevents old upload progress and credential writes', async () => {
  const storage = storageEnvironment([[scoped(1), 'A']])
  setAuthToken('A')
  let resolve
  globalThis.fetch = () => new Promise(done => { resolve = done })
  const progress = []
  const upload = uploadVideoInChunks(file(), value => progress.push(value), undefined, 1)
  setAuthToken('B')
  resolve(response({ uploadedChunks: [0] }))
  await assert.rejects(upload)
  assert.deepEqual(progress, [])
  assert.equal(storage.get(scoped(1)), 'A')
})

test('video MIME cannot bypass supported extension validation', () => {
  assert.match(validateVideoFile({ name: 'malicious.exe', size: 1, type: 'video/mp4' }), /不支持/)
  for (const ext of ['MP4', 'MOV', 'MKV', 'AVI', 'WEBM', 'M4V']) assert.equal(validateVideoFile({ name: `clip.${ext}`, size: 1, type: '' }), '')
})

test('invalid user identities cannot read or create shared resumable credentials', async () => {
  const storage = storageEnvironment([[legacy, 'A']])
  globalThis.fetch = () => { throw new Error('must not send an upload') }
  for (const userId of [undefined, null, 'null', 'undefined', '', 0, -1, NaN]) {
    assert.equal(hasUploadProgress(file(), userId), false)
    await assert.rejects(uploadVideoInChunks(file(), () => {}, undefined, userId), /请先登录/)
  }
  assert.equal(storage.get(legacy), 'A')
  assert.equal(storage.size, 1)
})
