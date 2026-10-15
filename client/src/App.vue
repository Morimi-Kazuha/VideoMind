<template>
  <div class="app-stage cyber-app">
    <div class="ambient-noise" aria-hidden="true"></div>
    <div class="ambient-glow" aria-hidden="true"></div>
    <div :inert="sidebar.visible || showAuthModal">
    <header class="navbar">
      <div class="nav-content">
        <a class="brand" href="/" aria-label="VideoMind 媒体库">
          <span class="brand-do">Video</span>
          <span class="brand-video">Mind</span>
          <span class="beta-badge">PRO</span>
        </a>

        <div class="nav-controls">
          <button v-if="!currentUser" class="auth-btn" @click="openAuthModal">
            <span class="btn-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
            </span>
            登录 / 注册
          </button>

          <div v-else class="user-profile">
            <span class="user-name">:: {{ currentUser.nickname }} ::</span>
            <button class="logout-btn" @click="logout" title="退出登录" aria-label="退出登录">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg>
            </button>
          </div>


        </div>
      </div>
    </header>
    <main class="main-container">
      <section id="import-media" class="hero-section">
        <h1 class="slogan-main">DECODE YOUR VIDEO</h1>
        <p class="slogan-sub">让每一帧都有答案</p>
        <div class="upload-wrapper">
          <input type="file" id="file-input" @change="handleFileChange" accept=".mp4,.mov,.mkv,.avi,.webm,.m4v" hidden />
          <div class="upload-magnet" :class="{ processing: uploading, 'is-dragover': isDragOver }"
            @dragenter.prevent="handleDragEnter" @dragover.prevent="isDragOver = true"
            @dragleave.prevent="handleDragLeave" @drop.prevent="handleDrop">
            <UploadPanels v-if="!uploading" :is-drag-over="isDragOver" @choose="chooseLocalFile" @imported="fetchList({ notify: true })" />
            <div v-else class="magnet-content busy ui1-uploading" role="status" aria-live="polite">
              <div class="quantum-loader"></div>
              <strong>{{ uploadProgress.label }}</strong>
              <span
                v-if="uploadProgress.filename"
                class="ui1-uploading__filename"
              >
                {{ uploadProgress.filename }}
              </span>
              <div
                v-if="uploadProgress.percent !== null"
                class="ui1-progress"
                role="progressbar"
                aria-label="视频上传进度"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="uploadProgress.percent"
              >
                <span :style="{ width: `${uploadProgress.percent}%` }"></span>
              </div>
              <div
                v-else
                class="ui1-progress is-indeterminate"
                aria-label="视频导入中"
              >
                <span></span>
              </div>
              <span v-if="uploadProgress.detail">
                {{ uploadProgress.detail }}
              </span>
              <span v-if="uploadProgress.warning" class="ui1-warning">
                {{ uploadProgress.warning }}
              </span>
              <button
                v-if="uploadAbort"
                type="button"
                class="ui1-plain-button"
                @click="cancelUpload"
              >
                取消上传
              </button>
            </div>
            <div class="border-glow"></div>
          </div>
        </div>
                  <div
            v-if="resumableFile && !uploading"
            class="ui1-resume"
            role="status"
          >
            <span>{{ resumeHint }}</span>
            <button type="button" @click="resumeUpload">继续上传</button>
            <button type="button" @click="discardResumableUpload">
              重新开始
            </button>
          </div>
          <div
            v-if="message"
            class="ui1-message"
            :class="{ 'is-error': messageIsError }"
            :role="messageIsError ? 'alert' : 'status'"
            :aria-live="messageIsError ? 'assertive' : 'polite'"
          >
            <span>{{ message }}</span>
            <button
              v-if="messageIsError"
              type="button"
              @click="dismissMessage"
              aria-label="关闭提示"
            >
              ×
            </button>
          </div>
      </section>
      <section id="media-library" class="workspace-section" tabindex="-1" aria-labelledby="library-title">
        <div class="section-header"><h3 id="library-title">工作台</h3><div class="count-chip">{{ list.length }} TASKS</div><span v-if="DEMO_MODE" class="demo-chip">视觉演示 · 非真实任务</span></div>
        <div class="library-tools">
          <input v-model="searchQuery" type="search" placeholder="搜索视频名称" aria-label="搜索视频名称" />
          <button type="button" class="auth-btn" :disabled="listLoading" @click="fetchList({ notify: true })">刷新列表</button>
        </div>
        <div v-if="libraryView === 'loading'" class="ui1-state" role="status">
            <CyberMark />
            <strong>正在加载媒体库</strong>
            <p>正在读取视频记录…</p>
            <div class="ui1-progress is-indeterminate"><span></span></div>
          </div>
          <div
            v-else-if="libraryView === 'error'"
            class="ui1-state is-error"
            role="alert"
          >
            <CyberMark />
            <strong>媒体库加载失败</strong>
            <p>{{ listError }}</p>
            <button
              type="button"
              class="academy-button academy-button--secondary"
              @click="fetchList({ notify: true })"
            >
              重试
            </button>
          </div>
          <div v-else-if="libraryView === 'empty'" class="ui1-state is-empty">
            <CyberMark />
            <strong>{{ currentUser ? '暂无媒体' : '登录后查看媒体库' }}</strong>
            <p>
              {{
                currentUser
                  ? '导入第一个视频，开始整理和分析。'
                  : '登录后可以导入视频，并查看你的媒体文件。'
              }}
            </p>
            <a
              v-if="currentUser"
              href="#import-media"
              class="academy-button academy-button--primary"
            >
              导入第一个视频
            </a>
            <button
              v-else
              type="button"
              class="academy-button academy-button--primary"
              @click="openAuthModal"
            >
              登录 / 注册
            </button>
          </div>

        <template v-else>
          <div class="card-grid" role="list" aria-label="媒体文件">
            <MediaCard v-for="item in visibleList" :key="item.id" :item="item"
              :upload-time="formatTime(item.uploadTime)" :status-class="cardStatusClass(item)"
              :status-label="cardStatusLabel(item)" :status-title="cardStatusTitle(item)"
              :audio-title="actionTitle(item, '下载音频')" :transcript-title="actionTitle(item, '提取文字')"
              :deleting="deletingId === item.id" @audio="downloadAudio" @transcript="enterTranscription"
              @analysis="enterAnalysis" @delete="deleteItem" />
          </div>
          <div v-if="!visibleList.length" class="ui1-search-empty">没有找到“{{ searchQuery }}” <button @click="searchQuery = ''">清除搜索</button></div>
          <p v-if="listError" class="ui1-inline-error" role="alert">刷新失败：{{ listError }} <button @click="fetchList({ notify: true })">重试</button></p>
        </template>
      </section>
    </main>
    </div>
    <Transition name="drawer">
      <div v-if="sidebar.visible" class="sidebar-backdrop" @click.self="closeSidebar">
        <section class="sidebar-panel workspace-drawer is-open" role="dialog" aria-modal="true" aria-label="视频分析工作台" @keydown="trapWorkspaceFocus">
          <div class="sidebar-header"><span class="sidebar-title">{{ sidebar.title || 'VideoMind · 视频分析' }}</span><button class="close-btn" @click="closeSidebar" aria-label="关闭分析工作区">×</button></div>
          <div class="workspace-drawer__body"><AnalysisWorkspace
      ref="analysisWorkspace"
      :sidebar="sidebar"
      :media="activeMedia"
      :actions="workspaceActions"
      :analysis-modes="analysisModes"
      :goal-presets="goalPresets"
      :trace-stages="traceStages"
      :rendered-markdown="renderedMarkdown"
      :loading-headline="loadingHeadline"
      :demo-mode="DEMO_MODE"
    />
</div>
        </section>
      </div>
    </Transition>
    <div
      v-if="showAuthModal"
      class="auth-backdrop"
      @click.self="closeAuthModal"
    >
      <div
        ref="authPanel"
        class="auth-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-title"
        @keydown="trapAuthFocus"
      >
        <div class="auth-header">
          <h2 id="auth-title" class="auth-title">
            {{ authMode === 'login' ? '用户登录' : '新用户注册' }}
          </h2>
          <button
            class="close-btn"
            @click="closeAuthModal"
            aria-label="关闭登录窗口"
          >
            ×
          </button>
        </div>
        <form class="auth-body" @submit.prevent="handleAuth">
          <div class="input-group">
            <label for="auth-username">用户名</label>
            <input
              id="auth-username"
              v-model="authForm.username"
              type="text"
              placeholder="输入账号"
              autocomplete="username"
              autofocus
            />
          </div>
          <div class="input-group">
            <label for="auth-password">密码</label>
            <input
              id="auth-password"
              v-model="authForm.password"
              type="password"
              placeholder="输入密码"
              :autocomplete="
                authMode === 'login' ? 'current-password' : 'new-password'
              "
            />
          </div>
          <div class="input-group" v-if="authMode === 'register'">
            <label for="auth-nickname">昵称</label>
            <input
              id="auth-nickname"
              v-model="authForm.nickname"
              type="text"
              placeholder="设置一个好听的名字"
              autocomplete="nickname"
            />
          </div>
          <div class="auth-action">
            <button type="submit" class="cyber-btn" :disabled="authLoading">
              <span v-if="!authLoading">
                {{ authMode === 'login' ? '立即登录' : '提交注册' }}
              </span>
              <span v-else>请求处理中...</span>
            </button>
          </div>
          <div class="auth-toggle">
            <span class="toggle-text">
              {{ authMode === 'login' ? '没有账号?' : '已有账号?' }}
            </span>
            <button type="button" class="toggle-link" @click="switchAuthMode()">
              {{ authMode === 'login' ? '去注册' : '去登录' }}
            </button>
          </div>
          <p
            v-if="authMessage"
            class="auth-msg"
            :class="{ error: authError }"
            :role="authError ? 'alert' : 'status'"
            aria-live="polite"
          >
            {{ authMessage }}
          </p>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch, onMounted, onUnmounted } from 'vue'
import { apiRequest, captureAuthSession, clearAuthToken, hasAuthToken, setAuthToken } from './api'
import {
  forgetUploadProgress,
  formatBytes,
  formatDurationText,
  hasUploadProgress,
  uploadVideoInChunks,
  validateVideoFile,
} from './chunkUpload'
import { DEMO_ITEM } from './demoData'
import { createTaskStreams } from './taskEvents'
import { useAnalysisWorkspace } from './useAnalysisWorkspace'
import AnalysisWorkspace from './AnalysisWorkspace.vue'
import CyberMark from './components/CyberMark.vue'
import UploadPanels from './components/UploadPanels.vue'
import MediaCard from './components/MediaCard.vue'
import './cyber-theme.css'

import { mediaLibraryView, mediaStatusLabel } from './mediaLibraryState.js'

const chooseLocalFile = () => document.getElementById('file-input')?.click()
const trapWorkspaceFocus = (event) => {
  if (event.key !== 'Tab') return
  const panel = event.currentTarget
  const targets = [...panel.querySelectorAll('button, input, textarea, select, a[href], video[controls], summary, [tabindex]:not([tabindex="-1"])')]
    .filter(element => !element.disabled && element.offsetParent !== null)
  if (!targets.length) return
  const first = targets[0], last = targets[targets.length - 1], active = document.activeElement
  if (event.shiftKey && (active === first || !targets.includes(active))) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && (active === last || !targets.includes(active))) { event.preventDefault(); first.focus() }
}
// --- 变量定义 ---
const DEMO_MODE = (typeof __VIDEOMIND_DEMO_ENABLED__ !== 'undefined' && __VIDEOMIND_DEMO_ENABLED__) && new URLSearchParams(window.location.search).has('demo')
const MESSAGE_TIMEOUT_MS = 4000
const file = ref(null)
const message = ref('')
const messageIsError = ref(false)
const uploading = ref(false)
const uploadProgress = ref({
  label: '准备上传',
  filename: '',
  percent: null,
  detail: '',
  warning: '',
})
const uploadAbort = ref(null)
const resumableFile = ref(null)
const resumableChunks = ref({ done: 0, total: 0 })
let mediaListGeneration = 0
const list = ref([])
const listLoading = ref(false)
const listError = ref('')
const searchQuery = ref('')
const analysisWorkspace = ref(null)
let authOperationGeneration = 0
const selectedMedia = ref(null)
const authPanel = ref(null)
const deletingId = ref(null)
const isOffline = ref(
  typeof navigator !== 'undefined' && navigator.onLine === false,
)
const activeTasks = ref([])
const elapsedSeconds = ref(0)
const visibleList = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase()
  if (!query) return list.value
  return list.value.filter((item) =>
    item.filename?.toLocaleLowerCase().includes(query),
  )
})
const activeMedia = computed(
  () =>
    list.value.find(
      (item) => String(item.id) === String(sidebar.value.mediaId),
    ) || selectedMedia.value,
)
const libraryView = computed(() =>
  mediaLibraryView({
    loading: listLoading.value,
    error: listError.value,
    items: list.value,
  }),
)
const isDragOver = ref(false)
const currentUser = ref(null)
const showAuthModal = ref(false)
const authMode = ref('login')
const authLoading = ref(false)
const authMessage = ref('')
const authError = ref(false)
const authForm = ref({ username: '', password: '', nickname: '' })
const taskStreams = createTaskStreams({
  onActiveChange: (tasks) => {
    activeTasks.value = tasks
  },
})
const VIDEO_EXTENSIONS = new Set(['mp4', 'mov', 'mkv', 'avi', 'webm', 'm4v'])
let dragDepth = 0
let messageTimer = null
let elapsedTimer = null
let lastUploadProgress = {}
let focusBeforeAuth = null
let focusBeforeSidebar = null

// --- 状态展示 ---
const activeTaskOf = (mediaId) =>
  activeTasks.value.find((task) => String(task.id) === String(mediaId))

const cardStatusClass = (item) => {
  if (activeTaskOf(item.id)) return 'processing'
  return mediaStatusClass(item.status)
}
const cardStatusLabel = (item) => {
  const task = activeTaskOf(item.id)
  return mediaStatusLabel(item.status, task?.type)
}
const cardStatusTitle = (item) => {
  const task = activeTaskOf(item.id)
  if (!task) return null
  return task.type === 'ai'
    ? 'AI 分析正在后台执行，完成后会提示你'
    : '文字提取正在后台执行，完成后会提示你'
}
const actionTitle = (item, label) =>
  item.status === 'COMPLETED' ? null : `视频尚未处理完成，暂时无法${label}`

const elapsedLabel = computed(() => {
  const total = elapsedSeconds.value
  if (total < 1) return ''
  const minutes = String(Math.floor(total / 60)).padStart(2, '0')
  return `${minutes}:${String(total % 60).padStart(2, '0')}`
})

const loadingHeadline = computed(() => {
  const fallback =
    sidebar.value.type === 'ai' ? 'Agent 正在分析视频证据' : '正在识别视频语音'
  const headline = sidebar.value.statusMessage || fallback
  // 用“已等待”而不是“已运行”：接管历史任务时计时是从打开面板算起的。
  return elapsedLabel.value
    ? `${headline} · 已等待 ${elapsedLabel.value}`
    : headline
})

const resumeHint = computed(() => {
  const target = resumableFile.value
  if (!target) return ''
  const { done, total } = resumableChunks.value
  const progress = total
    ? `已完成 ${Math.round((done / total) * 100)}%`
    : '已保留上传进度'
  return `${target.name} ${progress}，可继续未完成的上传`
})

// --- 核心业务逻辑 ---

const handleDragEnter = () => {
  dragDepth += 1
  isDragOver.value = true
}

// 拖过子元素也会触发 dragleave，用进出计数避免提示文案反复闪烁。
const handleDragLeave = () => {
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) isDragOver.value = false
}

const resetDragState = () => {
  dragDepth = 0
  isDragOver.value = false
}

/** 统一入口：登录、格式、体积三道校验全部在进入上传态之前完成。 */
const startUpload = async (selectedFile, extraFileCount = 0) => {
  if (uploading.value) {
    showMsg('已有上传任务在进行，请等当前任务结束', true)
    return
  }
  if (!currentUser.value) {
    showMsg('⚠️ 权限受限：请先登录系统', true)
    openAuthModal()
    return
  }
  if (!selectedFile) return
  if (!isSupportedVideo(selectedFile)) {
    showMsg(`⚠️ ${selectedFile.name} 不是受支持的视频格式`, true)
    return
  }
  const invalid = validateVideoFile(selectedFile)
  if (invalid) {
    showMsg(`⚠️ ${invalid}`, true)
    return
  }
  if (extraFileCount > 0) {
    showMsg(
      `一次只处理一个视频，已选择 ${selectedFile.name}，其余 ${extraFileCount} 个已忽略`,
    )
  }
  file.value = selectedFile
  await uploadFile()
}

const handleFileChange = async (e) => {
  const selected = e.target.files
  await startUpload(selected?.[0], Math.max(0, (selected?.length || 0) - 1))
  e.target.value = ''
}

const handleDrop = async (e) => {
  resetDragState()
  const dropped = e.dataTransfer?.files
  if (!dropped?.length) return
  await startUpload(dropped[0], dropped.length - 1)
}

const buildUploadWarning = (progress) => {
  if (progress.retryingCount) {
    return `网络不稳定，正在重试 ${progress.retryingCount} 个分片（第 ${progress.retryAttempt}/${progress.retryMaxAttempts} 次）`
  }
  if (progress.resumedChunks) {
    return `已续传：跳过 ${progress.resumedChunks} 个此前完成的分片`
  }
  return ''
}

const applyUploadProgress = (progress) => {
  lastUploadProgress = progress
  const merging = progress.phase === 'merging'
  const detail = [
    `${formatBytes(progress.uploadedBytes)} / ${formatBytes(progress.totalBytes)}`,
  ]
  detail.push(`分片 ${progress.completedChunks}/${progress.totalChunks}`)
  if (!merging && progress.bytesPerSecond) {
    detail.push(`${formatBytes(progress.bytesPerSecond)}/s`)
    const eta = formatDurationText(progress.etaSeconds)
    if (eta) detail.push(`剩余约 ${eta}`)
  }
  uploadProgress.value = {
    label: merging ? '分片已全部送达，正在服务端合并' : '正在安全上传',
    filename: file.value?.name || uploadProgress.value.filename,
    percent: progress.percent,
    detail: detail.join(' · '),
    warning: buildUploadWarning(progress),
  }
}

const rememberResumableUpload = (target) => {
  if (!target || !hasUploadProgress(target, currentUser.value?.id)) {
    resumableFile.value = null
    return
  }
  resumableFile.value = target
  resumableChunks.value = {
    done: lastUploadProgress.completedChunks || 0,
    total: lastUploadProgress.totalChunks || 0,
  }
}

const uploadFile = async () => {
  const target = file.value
  if (!target || uploading.value) return
  if (DEMO_MODE) {
    showMsg('演示模式：已模拟完成分片上传')
    return
  }

  const controller = new AbortController()
  uploadAbort.value = controller
  uploading.value = true
  resumableFile.value = null
  lastUploadProgress = {}
  const uploadUserId = currentUser.value?.id
  const session = captureAuthSession()
  const current = () => session.isCurrent() && currentUser.value?.id === uploadUserId && uploadAbort.value === controller
  uploadProgress.value = {
    label: hasUploadProgress(target, currentUser.value?.id) ? '正在核对已上传分片' : '准备分片上传',
    filename: target.name,
    percent: 0,
    detail: `0 B / ${formatBytes(target.size)}`,
    warning: '',
  }

  try {
    const uploadedMedia = await uploadVideoInChunks(
      target,
      progress => { if (current()) applyUploadProgress(progress) },
      controller.signal,
      uploadUserId,
    )
    if (!current()) return
    resumableFile.value = null
    showMsg(`✅ ${target.name} 上传完成`)
    await fetchList({ notify: true })
    if (current()) enterAnalysis(uploadedMedia)
  } catch (error) {
    if (!current()) return
    rememberResumableUpload(target)
    if (error?.aborted) {
      showMsg('上传已取消，进度已保留，可点“继续上传”接着传')
      return
    }
    showMsg(
      resumableFile.value
        ? `❌ 上传中断：${error.message}（进度已保留，可继续上传）`
        : `❌ 上传失败：${error.message}`,
      true,
    )
  } finally {
    if (!current()) return
    uploading.value = false
    uploadAbort.value = null
    file.value = null
  }
}

const cancelUpload = () => {
  if (!uploadAbort.value) return
  uploadProgress.value = {
    ...uploadProgress.value,
    label: '正在取消上传',
    warning: '',
  }
  uploadAbort.value.abort()
}

const resumeUpload = async () => {
  const target = resumableFile.value
  if (!target || uploading.value) return
  file.value = target
  await uploadFile()
}

const discardResumableUpload = () => {
  forgetUploadProgress(resumableFile.value, currentUser.value?.id)
  resumableFile.value = null
  resumableChunks.value = { done: 0, total: 0 }
  showMsg('已清除保留的上传进度，下次将从头开始')
}

/** 成功提示自动消失；错误提示保留到用户点掉，避免关键失败原因 4 秒后就没了。 */
const showMsg = (msg, isError = false) => {
  clearTimeout(messageTimer)
  messageTimer = null
  message.value = msg
  messageIsError.value = isError
  if (isError) return
  messageTimer = setTimeout(() => {
    if (message.value !== msg) return
    message.value = ''
    messageIsError.value = false
  }, MESSAGE_TIMEOUT_MS)
}

const dismissMessage = () => {
  if (!messageIsError.value) return
  clearTimeout(messageTimer)
  messageTimer = null
  message.value = ''
  messageIsError.value = false
}

const fetchList = async ({ notify = false } = {}) => {
  const request = ++mediaListGeneration
  const session = captureAuthSession()
  const userId = currentUser.value?.id
  const current = () => request === mediaListGeneration && session.isCurrent() && currentUser.value?.id === userId
  if (DEMO_MODE) return list.value
  if (!currentUser.value) {
    list.value = []
    listError.value = ''
    listLoading.value = false
    return list.value
  }
  listLoading.value = true
  listError.value = ''
  try {
    // 带时间戳绕开浏览器缓存，避免删除/新增之后列表还是旧的。
    const res = await apiRequest(`/media/list?_t=${Date.now()}`)
    if (res.status === 401) return null
    if (!res.ok) throw new Error((await res.text()) || '加载媒体库失败')
    const items = await res.json()
    if (!current()) return null
    list.value = items
  } catch (error) {
    if (!current()) return null
    listError.value = error?.message || '媒体库加载失败'
    if (notify) showMsg('视频资料库加载失败，请稍后刷新', true)
    return null
  } finally {
    if (current()) listLoading.value = false
  }
  return list.value
}

const isSupportedVideo = (selectedFile) => {
  const extension = selectedFile.name?.split('.').pop()?.toLowerCase()
  return VIDEO_EXTENSIONS.has(extension)
}

const mediaStatusClass = (status) =>
  ['COMPLETED', 'PROCESSING', 'FAILED'].includes(status)
    ? status.toLowerCase()
    : 'unknown'
const {
  sidebar,
  goalPresets,
  analysisModes,
  traceStages,
  renderedMarkdown,
  transcribe,
  closeSidebar,
  openAgent,
  submitAgent,
  startNewAnalysis,
  showDemoResult,
  startPlanEdit,
  cancelPlanEdit,
  addPlanTask,
  removePlanTask,
  rerunWithPlan,
  submitFollowUp,
  startNewConversation,
  searchEvidence,
  sendFeedback,
  retryPlayback,
  handlePlaybackError,
  resetWorkspace,
  discardMediaWorkspace,
  formatPercent,
} = useAnalysisWorkspace({
  demoMode: DEMO_MODE,
  taskStreams,
  showMessage: showMsg,
  refreshMediaList: fetchList,
  findMediaItem: (id) => list.value.find((item) => item.id === id),
  onAnswerAppended: () => scrollToLatestAnswer(),
})

/** 追问的答案追加在长文末尾，主动滚过去，否则用户会以为“点了没反应”。 */
const scrollToLatestAnswer = async () => {
  await analysisWorkspace.value?.scrollToLatestAnswer()
}

const enterAnalysis = (item) => {
  selectedMedia.value = item
  openAgent(item)
}
const enterTranscription = (item) => {
  selectedMedia.value = item
  transcribe(item.id)
}

/** Clipboard API 在非 HTTPS 环境不可用，这里保留一条降级路径，避免“复制失败”变成死路。 */
const copyToClipboard = async (text) => {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // 继续走下面的降级方案。
  }
  try {
    const scratch = document.createElement('textarea')
    scratch.value = text
    scratch.setAttribute('readonly', '')
    scratch.style.position = 'fixed'
    scratch.style.top = '0'
    scratch.style.opacity = '0'
    document.body.appendChild(scratch)
    scratch.select()
    const copied = document.execCommand('copy')
    document.body.removeChild(scratch)
    return copied
  } catch {
    return false
  }
}

const copyResult = async () => {
  const content = sidebar.value.content
  if (!content) {
    showMsg('还没有可复制的内容', true)
    return
  }
  const label = sidebar.value.type === 'ai' ? '分析结果' : '转写全文'
  if (await copyToClipboard(content)) showMsg(`${label}已复制`)
  else showMsg('复制失败，请手动选中内容后复制', true)
}

const resultFileBaseName = () => {
  const title = sidebar.value.title || ''
  const raw = title.split(' · ').slice(1).join(' · ') || title
  const cleaned = raw
    .replace(/\.[^/.]+$/, '')
    .replace(/[\\/:*?"<>|]/g, '_')
    .trim()
  return cleaned || (sidebar.value.type === 'ai' ? 'analysis' : 'transcript')
}

const downloadResult = () => {
  const content = sidebar.value.content
  if (!content) {
    showMsg('还没有可导出的内容', true)
    return
  }
  const isMarkdown = sidebar.value.type === 'ai'
  const blob = new Blob([content], {
    type: isMarkdown
      ? 'text/markdown;charset=utf-8'
      : 'text/plain;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${resultFileBaseName()}.${isMarkdown ? 'md' : 'txt'}`
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  // 立刻 revoke 在部分浏览器会导致下载拿不到内容，延后一拍更稳。
  setTimeout(() => URL.revokeObjectURL(url), 0)
  showMsg(`已导出 ${link.download}`)
}

const workspaceActions = {
  closeSidebar,
  refreshMediaList: () => fetchList({ notify: true }),
  showMessage: showMsg,
  searchEvidence,
  retryPlayback,
  handlePlaybackError,
  submitAgent,
  startNewAnalysis,
  copyResult,
  downloadResult,
  startPlanEdit,
  addPlanTask,
  removePlanTask,
  cancelPlanEdit,
  rerunWithPlan,
  submitFollowUp,
  startNewConversation,
  sendFeedback,
  transcribe,
  openAgent: enterAnalysis,
  formatPercent,
}

const deleteItem = async (item) => {
  if (DEMO_MODE) {
    list.value = list.value.filter((i) => i.id !== item.id)
    discardMediaWorkspace(item.id)
    showMsg('演示任务已移除')
    return
  }
  if (deletingId.value) return
  const runningTask = activeTaskOf(item.id)
  const warning = runningTask
    ? '\n\n注意：该视频还有任务正在后台执行，删除后这次的结果会丢失。'
    : ''
  if (!confirm(`确认要永久删除 "${item.filename}" 吗？${warning}`)) return
  const session = captureAuthSession()
  const current = () => session.isCurrent() && deletingId.value === item.id
  mediaListGeneration += 1
  listLoading.value = false
  deletingId.value = item.id
  try {
    const res = await apiRequest(`/media/delete?id=${item.id}`, {
      method: 'DELETE',
    })
    const text = await res.text()
    if (!current()) return
    mediaListGeneration += 1
    listLoading.value = false
    if (res.ok) {
      showMsg(`已删除 ${item.filename}`)
      list.value = list.value.filter((i) => i.id !== item.id)
      discardMediaWorkspace(item.id)
    } else {
      showMsg('❌ ' + text, true)
    }
  } catch (e) {
    if (current()) showMsg('❌ 删除请求失败', true)
  } finally {
    if (current()) deletingId.value = null
  }
}

const formatTime = (timeStr) => {
  if (!timeStr) return '--'
  const date = new Date(timeStr)
  if (Number.isNaN(date.getTime())) return '--'
  return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

const downloadAudio = async (item) => {
  if (DEMO_MODE) {
    showMsg(`演示模式：${item.filename} 音频已准备`)
    return
  }
  let fileName = item.filename || 'audio.mp3'
  const session = captureAuthSession()
  fileName = fileName.replace(/\.[^/.]+$/, '') + '.mp3'
  try {
    showMsg('正在转码并下载...')
    const res = await apiRequest(`/analysis/download?id=${item.id}`)
    // 失败时后端返回的是 JSON 信封，api.js 会把 message 解包给 text()，
    // 这里读出来向上抛，避免把“视频不存在 / 无权访问 / 转码失败”统一显示成同一句话。
    if (!res.ok) throw new Error((await res.text()) || '请稍后重试')
    const blob = await res.blob()
    if (!session.isCurrent()) return
    const downloadUrl = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = downloadUrl
    link.download = fileName
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(downloadUrl)
    showMsg('✅ 下载完成')
  } catch (e) {
    if (!session.isCurrent()) return
    showMsg('音频下载失败：' + (e?.message || '请稍后重试'), true)
  }
}

const restoreFocus = (element) => {
  if (element?.isConnected && typeof element.focus === 'function')
    element.focus()
}

const openAuthModal = () => {
  if (showAuthModal.value) return
  focusBeforeAuth = document.activeElement
  showAuthModal.value = true
  authMessage.value = ''
  authForm.value = { username: '', password: '', nickname: '' }
}
const closeAuthModal = () => {
  authOperationGeneration += 1
  authLoading.value = false
  showAuthModal.value = false
  const target = focusBeforeAuth
  nextTick(() => { if (!showAuthModal.value) restoreFocus(target) })
  focusBeforeAuth = null
}
const closeActiveOverlay = () => {
  if (showAuthModal.value) closeAuthModal()
  else if (sidebar.value.visible) closeSidebar()
}
const handleKeydown = (event) => {
  if (event.key === 'Escape') closeActiveOverlay()
}

/** 弹窗内循环 Tab，键盘用户不会一路跳到被遮住的背景里。 */
const trapAuthFocus = (event) => {
  if (event.key !== 'Tab' || !authPanel.value) return
  const focusable = [
    ...authPanel.value.querySelectorAll(
      'button, input, [tabindex]:not([tabindex="-1"])',
    ),
  ].filter((element) => !element.disabled && element.offsetParent !== null)
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement
  if (
    event.shiftKey &&
    (active === first || !authPanel.value.contains(active))
  ) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

const switchAuthMode = ({ keepMessage = false } = {}) => {
  authOperationGeneration += 1
  authLoading.value = false
  authMode.value = authMode.value === 'login' ? 'register' : 'login'
  if (!keepMessage) authMessage.value = ''
}
const handleAuth = async () => {
  if (authLoading.value) return
  if (!authForm.value.username || !authForm.value.password) {
    authMessage.value = '请输入完整的账号和密码'
    authError.value = true
    return
  }
  authLoading.value = true
  const operation = ++authOperationGeneration
  const session = captureAuthSession()
  const current = () => operation === authOperationGeneration && session.isCurrent()
  const requestedMode = authMode.value
  authMessage.value = ''
  const endpoint = authMode.value === 'login' ? '/user/login' : '/user/register'
  try {
    const res = await apiRequest(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(authForm.value),
    })
    if (!current()) return
    if (!res.ok) {
      const detail = await res.text()
      if (!current()) return
      authMessage.value = detail || `请求失败（HTTP ${res.status}）`
      authError.value = true
      return
    }
    const data = await res.json().catch(() => null)
    if (!current()) return
    if (!data?.userInfo) {
      authMessage.value = '服务端返回异常，请稍后重试'
      authError.value = true
      return
    }
    if (requestedMode === 'login') {
      resetSessionState()
      currentUser.value = data.userInfo
      localStorage.setItem('user', JSON.stringify(data.userInfo))
      setAuthToken(data.token)
      closeAuthModal()
      showMsg(`欢迎回来，${data.userInfo.nickname}`)
      fetchList({ notify: true })
    } else {
      authMessage.value = '注册成功，账号密码已保留，直接点“立即登录”即可'
      authError.value = false
      setTimeout(() => { if (current()) switchAuthMode({ keepMessage: true }) }, 900)
    }
  } catch (e) {
    if (!current()) return
    authMessage.value = e?.message || '网络连接错误'
    authError.value = true
  } finally {
    if (operation === authOperationGeneration) authLoading.value = false
  }
}
/** 退出与登录失效走同一套清理，避免两处漏掉不同的字段。 */
const resetSessionState = ({ clearStoredUser = true } = {}) => {
  authOperationGeneration += 1
  authLoading.value = false
  mediaListGeneration += 1
  uploadAbort.value?.abort()
  uploadAbort.value = null
  taskStreams.stopAll()
  resetWorkspace()
  deletingId.value = null
  currentUser.value = null
  list.value = []
  listError.value = ''
  listLoading.value = false
  searchQuery.value = ''
  file.value = null
  resumableFile.value = null
  resumableChunks.value = { done: 0, total: 0 }
  uploading.value = false
  if (clearStoredUser) localStorage.removeItem('user')
}

const logout = () => {
  if (hasAuthToken()) {
    apiRequest('/user/logout', { method: 'POST' }).catch(() => {})
  }
  resetSessionState()
  clearAuthToken()
  showMsg('已退出系统')
}

const handleAuthExpired = () => {
  resetSessionState()
  showMsg('登录状态已失效，请重新登录', true)
  openAuthModal()
}

const handleStorage = event => {
  if (event.key !== 'authToken' && event.key !== null) return
  // Another tab has already written the new user/token pair. Clear this
  // tab's operations without deleting that tab's shared user metadata.
  resetSessionState({ clearStoredUser: false })
  if (hasAuthToken()) {
    try { currentUser.value = JSON.parse(localStorage.getItem('user')) } catch { /* invalid metadata */ }
  }
  fetchList()
}

const handleOnline = () => {
  isOffline.value = false
  showMsg('网络已恢复，正在同步最新状态')
  if (currentUser.value) fetchList()
}

const handleOffline = () => {
  isOffline.value = true
  showMsg('网络已断开：上传会自动重试，后台任务会在恢复后继续', true)
}

// 上传中误关标签页会白丢已传分片，这里让浏览器先问一句。
const handleBeforeUnload = (event) => {
  if (!uploading.value) return
  event.preventDefault()
  event.returnValue = ''
}

// 打开弹层时挂上标记类，锁背景滚动的规则只在窄屏生效（见样式里的说明）。
const overlayOpen = computed(() => sidebar.value.visible || showAuthModal.value)
watch(overlayOpen, (open) => {
  document.body.classList.toggle('overlay-open', open)
})

watch(
  () => sidebar.value.visible,
  async (visible) => {
    if (visible) {
      focusBeforeSidebar = document.activeElement
      await nextTick()
      analysisWorkspace.value?.focus()
      window.scrollTo(0, 0)
      return
    }
    await nextTick()
    if (focusBeforeSidebar?.isConnected) restoreFocus(focusBeforeSidebar)
    else {
      const mediaRow = document.querySelector(
        `[data-media-id="${sidebar.value.mediaId}"]`,
      )
      const focusTarget = mediaRow || document.getElementById('media-library')
      focusTarget?.focus({ preventScroll: true })
    }
    focusBeforeSidebar = null
  },
)

// 长任务给一个时间锚点，用户才不会怀疑是不是卡死了。
watch(
  () => sidebar.value.loading,
  (loading) => {
    clearInterval(elapsedTimer)
    elapsedTimer = null
    elapsedSeconds.value = 0
    if (!loading) return
    const startedAt = Date.now()
    elapsedTimer = setInterval(() => {
      elapsedSeconds.value = Math.floor((Date.now() - startedAt) / 1000)
    }, 1000)
  },
)

onMounted(() => {
  window.addEventListener('auth-expired', handleAuthExpired)
  window.addEventListener('storage', handleStorage)
  window.addEventListener('keydown', handleKeydown)
  window.addEventListener('online', handleOnline)
  window.addEventListener('offline', handleOffline)
  window.addEventListener('beforeunload', handleBeforeUnload)
  if (DEMO_MODE) {
    const demoView = new URLSearchParams(window.location.search).get('demo')
    currentUser.value = { id: 1, nickname: 'Agent Demo' }
    list.value = ['library-empty', 'library-error'].includes(demoView)
      ? []
      : [
          ['library-processing', 'analysis-processing'].includes(demoView)
            ? { ...DEMO_ITEM, status: 'PROCESSING' }
            : DEMO_ITEM,
        ]
    if (demoView === 'library-upload') {
      uploading.value = true
      uploadProgress.value = {
        label: '正在安全上传',
        filename: '示例视频.mp4',
        percent: 42,
        detail: '42 MB / 100 MB · 分片 4/10',
        warning: '',
      }
    }
    if (demoView === 'library-error')
      listError.value = '无法连接媒体服务，请稍后重试'
    if (
      ![
        'library',
        'library-empty',
        'library-error',
        'library-processing',
        'library-upload',
        'analysis-processing',
      ].includes(demoView)
    ) {
      enterAnalysis(DEMO_ITEM)
      showDemoResult()
      if (demoView === 'analysis-evidence') {
        sidebar.value.evidenceQuery = '迭代遍历'
        searchEvidence()
      } else if (demoView === 'analysis-error') {
        sidebar.value.mode = 'compose'
        sidebar.value.content = ''
        sidebar.value.error = '分析请求暂时不可用，请稍后重试'
      }
    } else if (demoView === 'analysis-processing') {
      enterAnalysis(list.value[0])
    }
    return
  }
  const savedUser = localStorage.getItem('user')
  if (savedUser && hasAuthToken()) {
    try {
      currentUser.value = JSON.parse(savedUser)
    } catch (e) {}
  }
  fetchList({ notify: Boolean(currentUser.value) })
})
onUnmounted(() => {
  window.removeEventListener('auth-expired', handleAuthExpired)
  window.removeEventListener('storage', handleStorage)
  window.removeEventListener('keydown', handleKeydown)
  window.removeEventListener('online', handleOnline)
  window.removeEventListener('offline', handleOffline)
  window.removeEventListener('beforeunload', handleBeforeUnload)
  clearTimeout(messageTimer)
  clearInterval(elapsedTimer)
  uploadAbort.value?.abort()
  document.body.classList.remove('overlay-open')
  taskStreams.stopAll()
})
</script>
<style scoped src="./dovideo.css"></style>
<style scoped src="./production.css"></style>
