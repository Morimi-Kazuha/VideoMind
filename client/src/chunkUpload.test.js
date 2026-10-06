import test from 'node:test'
import assert from 'node:assert/strict'
import { uploadVideoInChunks, hasUploadProgress, validateVideoFile, MAX_UPLOAD_BYTES } from './chunkUpload.js'
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

function immediateBackoff(t) {
  const delays = []
  t.mock.method(globalThis, 'setTimeout', (callback, delay) => {
    delays.push(delay)
    queueMicrotask(callback)
    return 1
  })
  return delays
}

for (const status of ['network', 408, 429, 500, 503]) {
  test(`recoverable chunk failure ${status} stops at four attempts and preserves credential`, async t => {
    const storage = storageEnvironment()
    const delays = immediateBackoff(t)
    let attempts = 0
    globalThis.fetch = async url => {
      if (String(url).includes('init-upload')) return response({ uploadId: 'retry' })
      assert.ok(String(url).includes('upload-chunk'))
      attempts++
      if (status === 'network') throw new TypeError('response lost')
      return response(null, status, 'transient')
    }
    await assert.rejects(uploadVideoInChunks(file(), () => {}, undefined, 1), /分片 1\/1 上传失败/)
    assert.equal(attempts, 4)
    assert.equal(delays.length, 3)
    delays.forEach((delay, index) => {
      const base = 800 * 2 ** index
      assert.ok(delay >= base * 0.75 && delay <= base * 1.25)
    })
    assert.equal(storage.get(scoped(1)), 'retry')
  })
}

for (const status of [400, 401, 403, 404, 409, 413, 415]) {
  test(`permanent chunk HTTP ${status} is attempted only once`, async t => {
    storageEnvironment()
    const delays = immediateBackoff(t)
    let attempts = 0
    globalThis.fetch = async url => {
      if (String(url).includes('init-upload')) return response({ uploadId: 'permanent' })
      attempts++
      return response(null, status, 'permanent')
    }
    await assert.rejects(uploadVideoInChunks(file(), () => {}, undefined, 1))
    assert.equal(attempts, 1)
    assert.deepEqual(delays, [])
  })
}

test('lost chunk response retries the same identity and progress counts one confirmed chunk', async t => {
  storageEnvironment()
  immediateBackoff(t)
  const confirmed = new Set()
  const identities = []
  const progress = []
  globalThis.fetch = async (url, options) => {
    if (String(url).includes('init-upload')) return response({ uploadId: 'lost-chunk' })
    if (String(url).includes('complete-upload')) return response({ id: 39 })
    const identity = `${options.body.get('uploadId')}:${options.body.get('chunkIndex')}`
    identities.push(identity)
    confirmed.add(identity)
    if (identities.length === 1) throw new TypeError('HTTP response lost after persistence')
    return response({})
  }
  assert.equal((await uploadVideoInChunks(file(), value => progress.push(value), undefined, 1)).id, 39)
  assert.deepEqual(identities, ['lost-chunk:0', 'lost-chunk:0'])
  assert.equal(confirmed.size, 1)
  assert.equal(progress.at(-1).completedChunks, 1)
  assert.equal(progress.at(-1).uploadedBytes, 5)
})

test('five MiB logical bounds and at most three simultaneous requests skip server-confirmed parts', async () => {
  const chunkBytes = 5 * 1024 * 1024
  const large = { name: 'clip.mp4', size: chunkBytes * 5 + 7, lastModified: 7,
    slice(start, end) { bounds.push([start, end]); return new Blob(['chunk']) } }
  const key = `upload:1:${large.name}:${large.size}:7`
  storageEnvironment([[key, 'resume']])
  const bounds = [], indexes = [], pending = []
  let active = 0, maximum = 0
  globalThis.fetch = async (url, options) => {
    if (String(url).includes('upload-status')) return response({ uploadedChunks: [0, 2] })
    if (String(url).includes('complete-upload')) return response({ id: 39 })
    assert.ok(String(url).includes('upload-chunk'))
    indexes.push(Number(options.body.get('chunkIndex')))
    maximum = Math.max(maximum, ++active)
    await new Promise(resolve => pending.push(resolve))
    active--
    return response({})
  }
  const upload = uploadVideoInChunks(large, () => {}, undefined, 1)
  while (pending.length < 3) await new Promise(resolve => setImmediate(resolve))
  assert.equal(active, 3)
  assert.deepEqual(indexes, [1, 3, 4])
  pending.splice(0).forEach(resolve => resolve())
  while (pending.length < 1) await new Promise(resolve => setImmediate(resolve))
  pending.shift()()
  assert.equal((await upload).id, 39)
  assert.equal(maximum, 3)
  assert.deepEqual(indexes, [1, 3, 4, 5])
  assert.deepEqual(bounds, [[chunkBytes, 2 * chunkBytes], [3 * chunkBytes, 4 * chunkBytes],
    [4 * chunkBytes, 5 * chunkBytes], [5 * chunkBytes, large.size]])
  assert.match(validateVideoFile({ name: 'clip.mp4', size: MAX_UPLOAD_BYTES + 1 }), /上限/)
})

test('cancellation during retry backoff preserves upload ID and sends no complete', async () => {
  const storage = storageEnvironment()
  const controller = new AbortController()
  let attempts = 0
  globalThis.fetch = async url => {
    if (String(url).includes('init-upload')) return response({ uploadId: 'cancelled' })
    assert.ok(String(url).includes('upload-chunk'))
    attempts++
    return response(null, 503, 'temporary')
  }
  await assert.rejects(uploadVideoInChunks(file(), progress => {
    if (progress.retryingCount) controller.abort()
  }, controller.signal, 1), error => error.aborted === true)
  assert.equal(attempts, 1)
  assert.equal(storage.get(scoped(1)), 'cancelled')
})

for (const status of [404, 410]) {
  test(`explicit dead session HTTP ${status} creates a fresh attempt`, async () => {
    const storage = storageEnvironment([[scoped(1), 'expired']])
    let init = 0, chunks = 0
    globalThis.fetch = async url => {
      if (String(url).includes('upload-status')) return response(null, status, 'expired')
      if (String(url).includes('init-upload')) {
        assert.equal(storage.has(scoped(1)), false)
        init++
        return response({ uploadId: 'fresh' })
      }
      if (String(url).includes('upload-chunk')) chunks++
      return response({ id: 39 })
    }
    assert.equal((await uploadVideoInChunks(file(), () => {}, undefined, 1)).id, 39)
    assert.equal(init, 1)
    assert.equal(chunks, 1)
  })
}
