import { apiRequest, captureAuthSession } from './api.js'

export function createWorkspacePlayback(sidebar) {
  let playbackRequest = 0
  const loadPlayback = async (id) => {
    // Playback belongs to the media view, so changing an analysis goal or
    // revision does not strand its loading flag. A new playback request,
    // media view or auth session still owns its own result and cleanup.
    const request = ++playbackRequest
    const session = captureAuthSession()
    const state = sidebar.value
    const current = () => request === playbackRequest && session.isCurrent()
      && state === sidebar.value && state.visible && state.mediaId === id
    sidebar.value.playbackLoading = true
    sidebar.value.playbackError = ''
    try {
      const response = await apiRequest(`/media/playback?id=${id}`)
      const url = await response.text()
      if (!response.ok) throw new Error(url || '视频加载失败')
      if (current()) {
        sidebar.value.playbackUrl = url
        sidebar.value.playbackError = ''
      }
    } catch (error) {
      console.warn('Video preview unavailable', error)
      if (current()) {
        sidebar.value.playbackUrl = ''
        sidebar.value.playbackError = error.message || '原视频暂时无法加载'
      }
    } finally {
      if (current()) sidebar.value.playbackLoading = false
    }
  }

  return { loadPlayback }
}
