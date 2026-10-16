import { captureAuthSession } from './api.js'

export function createWorkspaceConversation(sidebar, onChange) {
  const userId = () => {
    try {
      if (!captureAuthSession().token) return null
      const id = JSON.parse(localStorage.getItem('user') || 'null')?.id
      return Number.isSafeInteger(id) && id > 0 ? id : null
    } catch { return null }
  }
  const conversationStorageKey = () => {
    const id = userId()
    if (!id || !sidebar.value.mediaId || sidebar.value.analysisMode === 'AUTO') return null
    return `videomind:conversation:${id}:${sidebar.value.mediaId}:${sidebar.value.analysisMode}:${encodeURIComponent(sidebar.value.goal.trim())}`
  }
  const ensureConversation = (fresh = false) => {
    const scope = conversationStorageKey()
    if (!scope) return null
    if (!fresh && sidebar.value.conversationScope === scope && sidebar.value.conversationId)
      return sidebar.value.conversationId
    let id
    try { if (!fresh) id = localStorage.getItem(scope) } catch { /* storage is optional */ }
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id || ''))
      id = globalThis.crypto.randomUUID()
    sidebar.value.conversationScope = scope
    sidebar.value.conversationId = id
    onChange()
    try { localStorage.setItem(scope, id) } catch { /* in-memory session still works */ }
    return id
  }
  return { userId, ensureConversation }
}
