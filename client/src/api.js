const API_BASE = (import.meta.env?.VITE_API_BASE_URL || '').replace(/\/$/, '')
const TOKEN_KEY = 'authToken'
let sessionGeneration = 0
let observedToken
const sessionListeners = new Set()

function syncSession(force = false) {
  const token = localStorage.getItem(TOKEN_KEY)
  if (force || token !== observedToken) {
    observedToken = token
    sessionGeneration += 1
    for (const listener of [...sessionListeners]) listener()
  }
  return token
}

export function captureAuthSession() {
  const token = syncSession()
  const generation = sessionGeneration
  return { token, isCurrent: () => syncSession() === token && sessionGeneration === generation }
}

export function onAuthSessionChange(listener) {
  sessionListeners.add(listener)
  return () => sessionListeners.delete(listener)
}

function requireCurrent(session) {
  if (!session.isCurrent()) throw new DOMException('登录会话已变化', 'AbortError')
}

globalThis.window?.addEventListener?.('storage', event => {
  if (event.key === TOKEN_KEY || event.key === null) syncSession(true)
})

export function hasAuthToken() {
  return Boolean(localStorage.getItem(TOKEN_KEY))
}

export function setAuthToken(token) {
  if (!token) throw new Error('登录接口未返回有效令牌')
  localStorage.setItem(TOKEN_KEY, token)
  syncSession(true)
}

export function clearAuthToken() {
  localStorage.removeItem(TOKEN_KEY)
  syncSession(true)
}

/**
 * 后端已统一响应体为 { code, message, data }（code === 0 表示成功；业务码放 body，
 * HTTP 状态只表达传输语义）。为了不让每个调用点都去关心这层信封，这里集中解包：
 *
 *   res.json()  ->  信封里的 data（数组 / 对象 / 字符串原样返回）
 *   res.text()  ->  成功时是 data 的文本形式；失败时是 message，
 *                   让现有的 `throw new Error(await res.text())` 直接拿到可读文案
 *   res.ok / res.status / res.headers  ->  保持原始 HTTP 语义（202 受理仍然 ok）
 *
 * 重要：只对 application/json 响应解包。SSE（text/event-stream，taskEvents.js 依赖
 * response.body 流式读取）和音频下载等非 JSON 响应必须原样透传——一旦在这里把 body
 * 读掉，流式传输会直接失效。
 */
// 以 code + message 判定信封，刻意不依赖 data 键：错误响应的 data 恒为 null，
// 若后端将来配置了 non_null 序列化（data 键被省略），依赖 data 会让所有错误响应
// 退回透传，用户就会看到整串原始 JSON。
function isEnvelope(payload) {
  return payload !== null
    && typeof payload === 'object'
    && !Array.isArray(payload)
    && typeof payload.code === 'number'
    && 'message' in payload
}

/** 把 data 转成文本：字符串直接用，空值给空串，其余序列化。 */
function dataAsText(data) {
  if (data === null || data === undefined) return ''
  return typeof data === 'string' ? data : JSON.stringify(data)
}

function unwrap(response, envelope) {
  const payload = envelope.data ?? null

  return {
    ok: response.ok,
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
    redirected: response.redirected,
    url: response.url,
    json: async () => payload,
    text: async () => (response.ok ? dataAsText(payload) : (envelope.message || '')),
    raw: response
  }
}

export async function apiRequest(path, options = {}) {
  const headers = new Headers(options.headers || {})
  const session = captureAuthSession()
  const token = session.token
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  } catch (error) {
    requireCurrent(session)
    if (error?.name === 'AbortError') throw error
    throw new Error('无法连接后端服务，请确认后端已启动且地址配置正确', { cause: error })
  }
  requireCurrent(session)
  let responseSession = session
  if (response.status === 401 && token && !path.startsWith('/user/')) {
    clearAuthToken()
    window.dispatchEvent(new Event('auth-expired'))
    // The error can finish reading in the expired session, but a subsequent
    // login must still invalidate it (including delayed JSON body reads).
    responseSession = captureAuthSession()
    if (responseSession.token) throw new DOMException('登录会话已变化', 'AbortError')
  }

  // 非 JSON（SSE / 音频流 / 空响应）原样返回，绝不触碰 body。
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) return response

  let envelope
  try {
    envelope = await response.clone().json()
  } catch {
    requireCurrent(responseSession)
    // 声明是 JSON 却解析不了（例如空 body），退回原始响应交给调用方处理。
    return response
  }
  requireCurrent(responseSession)
  if (!isEnvelope(envelope)) return response

  return unwrap(response, envelope)
}


