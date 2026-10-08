<template>
  <div class="app-stage academy-root ui1-app">
    <header class="ui1-header">
      <div class="ui1-header__inner">
        <a class="ui1-brand" href="/" aria-label="VideoMind 媒体库">
          <span class="ui1-brand__mark"><AcademyMark /></span>
          <span>
            VideoMind
            <small>MEDIA RESEARCH TERMINAL</small>
          </span>
        </a>
        <span class="ui1-header__context">
          媒体实验室
          <span>//</span>
          媒体库
        </span>
        <div class="ui1-header__actions">
          <span class="ui1-system" :class="{ 'is-offline': isOffline }">
            <i></i>
            {{ systemStatusText }}
          </span>
          <button
            v-if="!currentUser"
            class="ui1-plain-button"
            @click="openAuthModal"
          >
            登录 / 注册
          </button>
          <div v-else class="ui1-account">
            <span :title="currentUser.nickname">
              {{ currentUser.nickname }}
            </span>
            <button class="ui1-plain-button" @click="logout">退出登录</button>
          </div>
        </div>
      </div>
    </header>

    <div v-if="!sidebar.visible" class="ui1-layout">
      <aside class="ui1-rail" aria-label="主导航">
        <p class="ui1-rail__eyebrow">工作空间 / 01</p>
        <a
          class="ui1-rail__item is-current"
          href="#media-library"
          aria-current="page"
        >
          <span class="ui1-rail__glyph">▣</span>
          媒体库
          <span class="ui1-rail__count">{{ list.length }}</span>
        </a>
        <a class="ui1-rail__item" href="#import-media">
          <span class="ui1-rail__glyph">↥</span>
          导入媒体
        </a>
        <div class="ui1-rail__bottom">
          <span class="pixel-cluster" aria-hidden="true"></span>
          VideoMind / ACADEMY
          <br />
          <small>视频研究工作台</small>
        </div>
      </aside>

      <main class="ui1-main main-container">
        <div class="ui1-page-head">
          <div>
            <p class="ui1-eyebrow">ARCHIVE / 01</p>
            <h1>媒体库</h1>
            <p>管理视频、查看处理状态，并从这里开始分析。</p>
          </div>
          <a
            href="#import-media"
            class="academy-button academy-button--primary"
          >
            ＋ 导入媒体
          </a>
        </div>

        <section
          id="import-media"
          class="ui1-import"
          aria-labelledby="import-title"
        >
          <div class="ui1-section-heading">
            <span class="ui1-section-index">01</span>
            <h2 id="import-title">导入媒体</h2>
            <span class="ui1-section-note">本地视频文件</span>
          </div>
          <input
            type="file"
            id="file-input"
            @change="handleFileChange"
            accept=".mp4,.mov,.mkv,.avi,.webm,.m4v"
            hidden
          />
          <div
            class="ui1-import__body"
            :class="{ 'is-dragover': isDragOver, 'is-busy': uploading }"
            @dragenter.prevent="handleDragEnter"
            @dragover.prevent="isDragOver = true"
            @dragleave.prevent="handleDragLeave"
            @drop.prevent="handleDrop"
          >
            <template v-if="!uploading">
              <label for="file-input" class="ui1-drop">
                <span class="ui1-drop__icon"><AcademyMark /></span>
                <strong>
                  {{ isDragOver ? '松开以导入视频' : '将视频拖到这里导入' }}
                </strong>
                <span>或点击选择本地视频文件</span>
                <span class="ui1-drop__button">
                  选择本地文件
                  <span aria-hidden="true">↗</span>
                </span>
              </label>
            </template>
            <div v-else class="ui1-uploading" role="status" aria-live="polite">
              <span class="ui1-eyebrow">IMPORT / ACTIVE</span>
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

        <section
          id="media-library"
          tabindex="-1"
          class="ui1-library"
          aria-labelledby="library-title"
        >
          <div class="ui1-section-heading">
            <span class="ui1-section-index">02</span>
            <h2 id="library-title">媒体文件</h2>
            <span class="ui1-section-note">{{ list.length }} 个视频</span>
          </div>
          <div class="ui1-library__tools">
            <label class="ui1-search">
              <span aria-hidden="true">⌕</span>
              <input
                v-model="searchQuery"
                type="search"
                placeholder="搜索视频名称"
                aria-label="搜索视频名称"
              />
            </label>
            <button
              type="button"
              class="ui1-plain-button"
              :disabled="listLoading"
              @click="fetchList({ notify: true })"
            >
              刷新列表
            </button>
          </div>
          <div v-if="libraryView === 'loading'" class="ui1-state" role="status">
            <AcademyMark />
            <strong>正在加载媒体库</strong>
            <p>正在读取视频记录…</p>
            <div class="ui1-progress is-indeterminate"><span></span></div>
          </div>
          <div
            v-else-if="libraryView === 'error'"
            class="ui1-state is-error"
            role="alert"
          >
            <AcademyMark />
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
            <AcademyMark />
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
            <div class="ui1-list" role="list" aria-label="媒体文件">
              <article
                v-for="item in visibleList"
                :key="item.id"
                class="ui1-row"
                :data-media-id="item.id"
                tabindex="-1"
                role="listitem"
              >
                <span class="ui1-row__icon" aria-hidden="true">▶</span>
                <div class="ui1-row__identity">
                  <strong :title="item.filename">{{ item.filename }}</strong>
                  <span>
                    ID {{ item.id }}
                    <span aria-hidden="true">/</span>
                    导入于 {{ formatTime(item.uploadTime) }}
                  </span>
                </div>
                <span
                  class="ui1-row__status"
                  :class="cardStatusClass(item)"
                  :title="cardStatusTitle(item)"
                >
                  <i></i>
                  {{ cardStatusLabel(item) }}
                </span>
                <div class="ui1-row__actions">
                  <button
                    type="button"
                    :title="
                      item.status === 'COMPLETED'
                        ? '打开视频分析'
                        : '查看视频处理状态'
                    "
                    @click="enterAnalysis(item)"
                  >
                    {{ item.status === 'COMPLETED' ? '视频分析' : '查看状态' }}
                  </button>
                  <button
                    type="button"
                    :disabled="item.status !== 'COMPLETED'"
                    :title="actionTitle(item, '提取文字')"
                    @click="enterTranscription(item)"
                  >
                    提取文字
                  </button>
                  <button
                    type="button"
                    :disabled="item.status !== 'COMPLETED'"
                    :title="actionTitle(item, '下载音频')"
                    @click="downloadAudio(item)"
                  >
                    下载音频
                  </button>
                  <button
                    type="button"
                    class="ui1-row__delete"
                    :disabled="deletingId === item.id"
                    :title="deletingId === item.id ? '正在删除…' : '删除视频'"
                    :aria-label="`删除 ${item.filename}`"
                    @click="deleteItem(item)"
                  >
                    删除
                  </button>
                </div>
              </article>
            </div>
            <div v-if="!visibleList.length" class="ui1-search-empty">
              <span>没有找到“{{ searchQuery }}”</span>
              <button type="button" @click="searchQuery = ''">清除搜索</button>
            </div>
            <p v-if="listError" class="ui1-inline-error" role="alert">
              刷新失败：{{ listError }}
              <button type="button" @click="fetchList({ notify: true })">
                重试
              </button>
            </p>
          </template>
        </section>
      </main>
    </div>

    <div v-if="!sidebar.visible" class="ui1-footer">
      <span>VideoMind / MEDIA RESEARCH</span>
      <span>{{ systemStatusText }}</span>
    </div>

    <AnalysisWorkspace
      v-if="sidebar.visible"
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
import AcademyMark from './design/AcademyMark.vue'
import './design/academy.css'
import './production.css'
import { mediaLibraryView, mediaStatusLabel } from './mediaLibraryState.js'

// --- 变量定义 ---
const DEMO_MODE = new URLSearchParams(window.location.search).has('demo')
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

const systemStatusText = computed(() => {
  if (isOffline.value) return '网络已断开'
  if (uploading.value) {
    return uploadProgress.value.percent !== null
      ? `上传 ${uploadProgress.value.percent}%`
      : '处理中'
  }
  if (activeTasks.value.length) return `后台任务 ${activeTasks.value.length}`
  return '系统就绪'
})

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
  restoreFocus(focusBeforeAuth)
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

<style>
/* 字体已改为 index.html 里的非阻塞 <link> 加载，此处不再用 @import 阻塞首屏 CSSOM */

:root {
  --bg-deep: #0b0c10;
  --bg-card: #121418;
  --accent-lime: #c5f946;
  --accent-purple: #8a2be2;
  --text-main: #e0e0e0;
  --text-sub: #71757a;
  --text-inverse: #0b0c10;
  --border-tech: #2a2d35;
  --shadow-float: 0 10px 30px -10px rgba(0, 0, 0, 0.7);
  --shadow-glow-lime: 0 0 20px rgba(197, 249, 70, 0.2);
}

* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html,
body,
#app {
  margin: 0 !important;
  padding: 0 !important;
  width: 100vw !important;
  max-width: 100vw !important;
  min-height: 100vh !important;
  overflow-x: hidden;
  background-color: var(--bg-deep);
}

.app-stage {
  position: relative;
  z-index: 1;
  width: 100%;
  min-height: 100vh;
  color: var(--text-main);
  font-family: 'Space Grotesk', 'Noto Sans SC', monospace;
}

.ambient-noise {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseFilter'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.65' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseFilter)' opacity='0.05'/%3E%3C/svg%3E");
  pointer-events: none;
  z-index: -1;
}
.ambient-glow {
  position: fixed;
  top: -20%;
  left: 20%;
  width: 60vw;
  height: 60vh;
  background: radial-gradient(
    circle,
    rgba(197, 249, 70, 0.08) 0%,
    rgba(11, 12, 16, 0) 70%
  );
  pointer-events: none;
  z-index: -2;
}

/* 导航 */
.navbar {
  position: sticky;
  top: 0;
  z-index: 100;
  width: 100%;
  padding: 1.2rem 0;
  background: rgba(11, 12, 16, 0.85);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border-tech);
}
.nav-content {
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 2rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.brand {
  display: flex;
  align-items: baseline;
  gap: 2px;
}
.brand-do {
  font-family: 'Dela Gothic One', sans-serif;
  font-size: 1.8rem;
  color: var(--text-main);
  letter-spacing: -1px;
}
.brand-video {
  font-family: 'Space Grotesk', sans-serif;
  font-size: 1.8rem;
  font-weight: 300;
}
.beta-badge {
  font-size: 0.7rem;
  font-weight: 700;
  background: var(--accent-lime);
  color: var(--text-inverse);
  padding: 2px 6px;
  border-radius: 2px;
  margin-left: 8px;
  transform: translateY(-4px);
  box-shadow: 0 0 5px var(--accent-lime);
}

.nav-controls {
  display: flex;
  align-items: center;
  gap: 15px;
}
.auth-btn {
  background: transparent;
  border: 1px solid var(--border-tech);
  color: var(--accent-lime);
  padding: 6px 16px;
  border-radius: 4px;
  font-family: 'Noto Sans SC', sans-serif;
  font-weight: 700;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: all 0.3s;
  font-size: 0.85rem;
}
.auth-btn:hover {
  background: rgba(197, 249, 70, 0.1);
  border-color: var(--accent-lime);
  box-shadow: 0 0 10px rgba(197, 249, 70, 0.2);
}
.user-profile {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: monospace;
  font-size: 0.9rem;
  color: var(--text-main);
}
.user-name {
  color: var(--accent-lime);
}
.logout-btn {
  background: none;
  border: none;
  color: var(--text-sub);
  cursor: pointer;
  padding: 4px;
  display: flex;
  align-items: center;
  transition: color 0.3s;
}
.logout-btn:hover {
  color: #ff4757;
}

.status-pill {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-card);
  padding: 6px 12px;
  border-radius: 4px;
  border: 1px solid var(--border-tech);
  font-size: 0.8rem;
  color: var(--text-sub);
}
.status-dot {
  width: 6px;
  height: 6px;
  background: var(--accent-lime);
  border-radius: 50%;
}
.status-pill.is-active .status-dot {
  animation: pulse-lime 1.5s infinite alternate;
}

/* Hero */
.main-container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 4rem 2rem;
}
.hero-section {
  text-align: center;
  margin-bottom: 6rem;
  animation: slideUpFade 0.8s forwards;
}
.slogan-main {
  font-family: 'Syncopate', sans-serif;
  font-size: clamp(2.5rem, 6vw, 4.5rem);
  font-weight: 700;
  margin-bottom: 0.5rem;
  text-shadow: 0 0 20px rgba(197, 249, 70, 0.2);
}
.slogan-sub {
  font-size: 1.1rem;
  color: var(--text-sub);
  letter-spacing: 2px;
  margin-bottom: 3rem;
}

/* === [START] 核心重构：Upload Wrapper (Physical Skew) === */
.upload-wrapper {
  max-width: 800px;
  margin: 0 auto;
  perspective: 1000px;
  opacity: 0;
  animation: slideUpFade 0.8s 0.2s forwards;
}

.upload-magnet {
  position: relative;
  height: 300px;
  background: var(--bg-card);
  border-radius: 16px;
  box-shadow: var(--shadow-float);
  border: 2px solid var(--border-tech);
  overflow: hidden; /* 必须隐藏溢出 */
  transition: all 0.3s;
}
.upload-magnet:hover {
  border-color: var(--accent-lime);
  box-shadow: var(--shadow-glow-lime);
  transform: translateY(-5px);
}

/* 容器布局 */
.split-container {
  display: flex;
  height: 100%;
  width: 100%;
  position: relative;
  overflow: hidden;
}

/* 左右面板 (物理倾斜) */
.skew-pane {
  flex: 1;
  height: 100%;
  position: relative;
  cursor: pointer;
  background: rgba(11, 12, 16, 0.5); /* 默认深色底 */
  transition: all 0.4s ease;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1;
  /* 核心：直接对容器进行 skew，而不是 clip-path */
  transform: skewX(-10deg);
}

/* 增加左右面板的宽度，确保覆盖边缘 */
.pane-local {
  margin-left: -20px;
  padding-right: 20px;
  border-right: 2px solid var(--accent-lime);
}
.pane-url {
  margin-right: -20px;
  padding-left: 20px;
}

/* 鼠标悬停逻辑：只改变背景色，不加外发光，防止穿模 */
.skew-pane:hover {
  background: rgba(197, 249, 70, 0.05); /* 极淡的绿色背景，限制在斜框内 */
  z-index: 10;
}

/* 中间缝隙 */
.split-gap {
  width: 4px;
  background: transparent;
  transform: skewX(-10deg);
}

/* 内容回正 */
.pane-content {
  /* 必须反向 skew 回来，否则文字是斜的 */
  transform: skewX(10deg);
  display: flex;
  flex-direction: column;
  align-items: center;
  z-index: 2;
  transition: transform 0.3s;
}
.skew-pane:hover .pane-content {
  transform: skewX(10deg) scale(1.05);
}

/* 互斥变暗 */
.split-container:has(.skew-pane:hover) .skew-pane:not(:hover) {
  opacity: 0.3;
  filter: grayscale(1);
}

.magnet-icon {
  color: var(--accent-lime);
  margin-bottom: 1rem;
  filter: drop-shadow(0 0 5px var(--accent-lime));
}
.magnet-title {
  font-size: 1.4rem;
  font-weight: 700;
  letter-spacing: 1px;
  margin-bottom: 5px;
  font-family: 'Dela Gothic One', sans-serif;
}
.magnet-desc {
  font-size: 0.8rem;
  color: var(--text-sub);
  font-family: monospace;
}

/* URL 输入框 (需回正) */
.url-input-box {
  display: flex;
  margin-top: 15px;
  border-bottom: 2px solid var(--border-tech);
  transition: all 0.3s;
  position: relative;
  z-index: 30;
}
.skew-pane:hover .url-input-box {
  border-color: var(--accent-lime);
}
.url-input-box input {
  background: transparent;
  border: none;
  outline: none;
  color: var(--text-main);
  font-family: monospace;
  padding: 8px 5px;
  width: 180px;
  font-size: 0.9rem;
}
.url-go-btn {
  background: transparent;
  border: none;
  color: var(--accent-lime);
  cursor: pointer;
  padding: 0 8px;
  opacity: 0.7;
  transition: all 0.3s;
}
.url-go-btn:hover {
  opacity: 1;
  transform: translateX(3px);
}

/* 处理中状态 */
.magnet-content.busy {
  height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: var(--bg-card);
  position: relative;
  z-index: 50;
}
.busy-text {
  margin-top: 15px;
  color: var(--accent-lime);
  font-family: monospace;
  animation: pulse-lime 2s infinite;
}
.busy-file {
  max-width: 80%;
  margin-top: 8px;
  color: var(--text-sub);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.upload-progress {
  width: min(320px, 72%);
  height: 4px;
  margin-top: 20px;
  background: var(--border-tech);
  overflow: hidden;
}
.upload-progress span {
  display: block;
  height: 100%;
  background: var(--accent-lime);
  transition: width 0.25s ease;
}
.busy-stat {
  max-width: 82%;
  margin-top: 10px;
  color: var(--text-sub);
  font-family: monospace;
  font-size: 0.78rem;
  text-align: center;
}
.busy-warning {
  max-width: 82%;
  margin-top: 8px;
  color: #ff9aa4;
  font-family: monospace;
  font-size: 0.78rem;
  text-align: center;
}
.busy-actions {
  margin-top: 16px;
}
.busy-actions button {
  border: 1px solid var(--border-tech);
  border-radius: 4px;
  background: transparent;
  color: var(--text-sub);
  padding: 7px 14px;
  font-size: 0.8rem;
  cursor: pointer;
  transition: all 0.3s;
}
.busy-actions button:hover {
  border-color: #ff4757;
  color: #ff7c88;
}
/* === [END] 重构结束 === */

.upload-resume {
  margin-top: 16px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--bg-card);
  border: 1px solid var(--border-tech);
  border-left: 2px solid var(--accent-lime);
  color: var(--text-sub);
  font-size: 0.85rem;
  text-align: left;
}
.upload-resume button {
  border: 1px solid var(--border-tech);
  border-radius: 4px;
  background: transparent;
  color: var(--accent-lime);
  padding: 6px 12px;
  cursor: pointer;
}
.upload-resume button:hover {
  border-color: var(--accent-lime);
  background: rgba(197, 249, 70, 0.08);
}

.notification-bar {
  margin-top: 2rem;
  display: inline-block;
  background: var(--accent-lime);
  color: var(--text-inverse);
  padding: 10px 24px;
  font-weight: 700;
  border-radius: 4px;
  clip-path: polygon(5% 0%, 100% 0%, 95% 100%, 0% 100%);
}
.notification-bar.error {
  background: #ff4757;
  color: #fff;
  cursor: pointer;
}

.quantum-loader {
  width: 50px;
  height: 50px;
  border: 4px solid var(--border-tech);
  border-top-color: var(--accent-lime);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-bottom: 1rem;
  box-shadow: 0 0 10px var(--accent-lime);
}
.quantum-loader.small {
  width: 30px;
  height: 30px;
  margin: 0 auto;
}

/* Workspace */
.workspace-section {
  opacity: 0;
  animation: slideUpFade 0.8s 0.4s forwards;
}
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 2rem;
  border-bottom: 2px solid var(--border-tech);
  padding-bottom: 12px;
}
.library-title {
  display: flex;
  align-items: center;
  gap: 12px;
}
.section-header h3 {
  font-size: 1.5rem;
  font-weight: 700;
}
.count-chip {
  background: var(--border-tech);
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-family: monospace;
}
.library-search {
  width: min(320px, 42vw);
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 11px;
  border: 1px solid var(--border-tech);
  background: #090a0d;
  color: var(--text-sub);
}
.library-search:focus-within {
  border-color: var(--accent-lime);
  color: var(--accent-lime);
}
.library-search input {
  width: 100%;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text-main);
  font: inherit;
}
.library-empty {
  padding: 52px 20px;
  text-align: center;
  color: var(--text-sub);
  border: 1px dashed var(--border-tech);
}
.library-empty button {
  margin-top: 12px;
  border: 0;
  background: transparent;
  color: var(--accent-lime);
  cursor: pointer;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 20px;
}
.project-card {
  background: var(--bg-card);
  border-radius: 12px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.3);
  border: 1px solid var(--border-tech);
  overflow: hidden;
  transition: transform 0.2s;
  position: relative;
}
.project-card:hover {
  transform: translateY(-2px);
  border-color: var(--accent-lime);
}
.card-meta {
  display: flex;
  gap: 1.5rem;
  padding: 1.5rem;
  align-items: center;
  border-bottom: 1px solid var(--border-tech);
  background: rgba(18, 21, 18, 0.5);
}
.meta-icon {
  width: 56px;
  height: 56px;
  background: rgba(197, 249, 70, 0.05);
  border: 1px solid var(--accent-lime);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent-lime);
}
.filename-mask {
  font-size: 1.1rem;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 180px;
}
.meta-tags {
  display: flex;
  gap: 12px;
  font-size: 0.85rem;
  font-family: monospace;
  margin-top: 5px;
}
.time-tag {
  color: var(--text-sub);
}
.status-indicator {
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
}
.status-indicator.completed {
  color: var(--accent-lime);
  border: 1px solid var(--accent-lime);
  background: rgba(197, 249, 70, 0.1);
}
.status-indicator.processing {
  color: var(--accent-purple);
  border: 1px solid var(--accent-purple);
  animation: blink 1s infinite;
}
.status-indicator.failed {
  color: #ff7c88;
  border: 1px solid #ff4757;
  background: rgba(255, 71, 87, 0.08);
}
.status-indicator.unknown {
  color: var(--text-sub);
  border: 1px solid var(--border-tech);
}

.action-dock {
  display: grid;
  grid-template-columns: 1fr 1fr 1.5fr;
  gap: 12px;
  padding: 12px;
  background: rgba(5, 8, 5, 0.5);
}
.dock-item {
  position: relative;
  border: 1px solid var(--border-tech);
  background: var(--bg-card);
  border-radius: 8px;
  padding: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  cursor: pointer;
  transition: all 0.3s;
  color: var(--text-sub);
  font-family: monospace;
  overflow: hidden;
}
.dock-item:hover:not(:disabled) {
  color: var(--accent-lime);
  border-color: var(--accent-lime);
  background: rgba(197, 249, 70, 0.05);
}
.dock-item:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}
.dock-item.ai-core {
  border-color: var(--accent-purple);
  color: var(--accent-purple);
}
.dock-item.ai-core .label-group {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  z-index: 1;
}
.dock-item.ai-core .item-sub {
  font-size: 0.75rem;
  color: var(--accent-purple);
  opacity: 0.8;
}
.dock-item.ai-core:hover:not(:disabled) {
  border-color: var(--accent-lime);
  color: var(--text-inverse);
  background: var(--accent-lime);
}
.dock-item.ai-core:hover:not(:disabled) .item-sub {
  color: var(--text-inverse);
}

/* Sidebar */
.sidebar-backdrop {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(4px);
  z-index: 998;
}
.sidebar-panel {
  position: fixed;
  top: 0;
  right: -920px;
  width: 880px;
  max-width: calc(100vw - 24px);
  height: 100%;
  background: var(--bg-card);
  border-left: 2px solid var(--accent-lime);
  z-index: 999;
  transition: right 0.4s cubic-bezier(0.19, 1, 0.22, 1);
  display: flex;
  flex-direction: column;
  box-shadow: -10px 0 40px rgba(0, 0, 0, 0.8);
}
.sidebar-panel.is-open {
  right: 0;
}
.sidebar-header {
  padding: 20px 30px;
  border-bottom: 1px solid var(--border-tech);
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: rgba(11, 12, 16, 0.9);
}
.sidebar-title {
  font-size: 1.4rem;
  font-weight: 700;
  color: var(--text-main);
  display: flex;
  align-items: center;
  gap: 10px;
}
.icon {
  color: var(--accent-lime);
  display: flex;
  align-items: center;
}
.close-btn {
  width: 40px;
  height: 40px;
  background: none;
  border: none;
  color: var(--text-sub);
  cursor: pointer;
  transition: color 0.3s;
  font-size: 1.35rem;
}
.close-btn:hover {
  color: var(--accent-lime);
}
.sidebar-body {
  flex: 1;
  overflow-y: auto;
  padding: 30px;
}
.loading-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-sub);
  gap: 20px;
}
.markdown-content,
.text-content {
  line-height: 1.8;
  color: var(--text-main);
  font-size: 0.95rem;
}
.text-content pre {
  white-space: pre-wrap;
  font-family: monospace;
  background: #000;
  padding: 15px;
  border-radius: 8px;
  border: 1px solid var(--border-tech);
  color: #ccc;
}
.text-meta {
  margin-bottom: 10px;
  color: var(--text-sub);
  font-family: monospace;
  font-size: 0.8rem;
}
.markdown-content h1,
.markdown-content h2,
.markdown-content h3 {
  color: var(--accent-lime);
  margin-top: 1.5em;
  margin-bottom: 0.5em;
  font-family: 'Space Grotesk', sans-serif;
}
.markdown-content h1 {
  border-bottom: 1px solid var(--border-tech);
  padding-bottom: 10px;
}
.markdown-content ul {
  padding-left: 20px;
}
.markdown-content li {
  margin-bottom: 8px;
  color: #d4d4d8;
}
.markdown-content strong {
  color: var(--accent-lime);
  font-weight: 700;
}
.markdown-content p {
  margin-bottom: 1em;
}
.markdown-content a[href^='#video-t='] {
  display: inline-block;
  padding: 1px 6px;
  border: 1px solid rgba(197, 249, 70, 0.45);
  color: var(--accent-lime);
  text-decoration: none;
  font-family: monospace;
}
.markdown-content a[href^='#video-t=']:hover {
  background: var(--accent-lime);
  color: var(--text-inverse);
}
.result-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 18px;
}
.result-actions button {
  border: 1px solid var(--border-tech);
  background: transparent;
  color: var(--text-sub);
  padding: 8px 10px;
  cursor: pointer;
}
.result-actions button:hover:not(:disabled) {
  border-color: var(--accent-lime);
  color: var(--accent-lime);
}
.result-actions button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Agent workspace */
.video-evidence {
  margin: -30px -30px 28px;
  background: #050607;
  border-bottom: 1px solid var(--border-tech);
}
.video-evidence video {
  display: block;
  width: 100%;
  max-height: 420px;
  aspect-ratio: 16 / 9;
  background: #000;
  object-fit: contain;
}
.video-evidence p,
.video-evidence-loading {
  padding: 10px 30px;
  color: var(--text-sub);
  font-size: 0.8rem;
}
.video-evidence-loading {
  min-height: 110px;
  display: grid;
  place-items: center;
}
.video-evidence-error {
  min-height: 110px;
  padding: 18px 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: #ff9aa4;
}
.video-evidence-error button {
  border: 1px solid #ff4757;
  background: transparent;
  color: #ff9aa4;
  padding: 6px 10px;
  border-radius: 4px;
  cursor: pointer;
}
.agent-composer {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.agent-caption {
  color: var(--text-sub);
  line-height: 1.7;
}
.inline-error {
  padding: 11px 12px;
  border-left: 2px solid #ff4757;
  background: rgba(255, 71, 87, 0.08);
  color: #ff9aa4;
  line-height: 1.5;
}
.agent-composer textarea,
.follow-up-box textarea {
  width: 100%;
  min-height: 130px;
  resize: vertical;
  background: #090a0d;
  color: var(--text-main);
  border: 1px solid var(--border-tech);
  border-radius: 6px;
  padding: 14px;
  line-height: 1.6;
  outline: none;
}
.agent-composer textarea:focus,
.follow-up-box textarea:focus {
  border-color: var(--accent-lime);
}
.field-counter {
  color: var(--text-sub);
  font-family: monospace;
  font-size: 0.76rem;
  text-align: right;
}
.goal-presets {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.goal-presets button,
.feedback-row button {
  border: 1px solid var(--border-tech);
  border-radius: 4px;
  background: transparent;
  color: var(--text-sub);
  padding: 7px 10px;
  cursor: pointer;
}
.goal-presets button {
  min-height: 82px;
  padding: 12px;
  text-align: left;
}
.goal-presets strong,
.goal-presets span {
  display: block;
}
.goal-presets strong {
  margin-bottom: 6px;
  color: var(--text-main);
}
.goal-presets span {
  font-size: 0.76rem;
  line-height: 1.45;
}
.goal-presets button:hover,
.goal-presets button.active,
.feedback-row button:hover,
.feedback-row button.active {
  color: var(--accent-lime);
  border-color: var(--accent-lime);
  background: rgba(197, 249, 70, 0.08);
}
.goal-presets button:hover strong,
.goal-presets button.active strong {
  color: var(--accent-lime);
}
.agent-run-btn {
  border: 0;
  border-radius: 4px;
  padding: 13px 18px;
  background: var(--accent-lime);
  color: var(--text-inverse);
  font-weight: 700;
  cursor: pointer;
}
.agent-run-btn:disabled,
.follow-up-box button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.evidence-search {
  margin: 18px 0 24px;
}
.evidence-search-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}
.evidence-search-form input {
  min-width: 0;
  border: 1px solid var(--border-tech);
  border-radius: 4px;
  background: #090a0d;
  color: var(--text-main);
  padding: 10px 12px;
  outline: none;
}
.evidence-search-form input:focus {
  border-color: var(--accent-lime);
}
.evidence-search-form button,
.evidence-search-results button {
  border: 1px solid var(--border-tech);
  border-radius: 4px;
  background: transparent;
  color: var(--text-sub);
  padding: 9px 11px;
  cursor: pointer;
}
.evidence-search-form button:hover,
.evidence-search-results button:hover {
  border-color: var(--accent-lime);
  color: var(--accent-lime);
}
.evidence-search-form button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.evidence-search-error {
  margin-top: 8px;
  color: #ff9aa4;
  font-size: 0.82rem;
}
.evidence-search-results {
  display: grid;
  gap: 6px;
  margin-top: 8px;
}
.evidence-search-results button {
  display: grid;
  grid-template-columns: 58px 70px minmax(0, 1fr);
  gap: 10px;
  text-align: left;
}
.evidence-search-results strong {
  color: var(--accent-lime);
}
.evidence-search-results small {
  color: var(--accent-purple);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.evidence-search-results span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-running {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.agent-running .loading-state {
  min-height: 210px;
  height: auto;
}
.loading-state p {
  max-width: 34rem;
  text-align: center;
}
.stream-offline {
  color: #ff9aa4;
  font-family: monospace;
  font-size: 0.82rem;
}
.loading-hint {
  color: var(--text-sub);
  font-size: 0.8rem;
  opacity: 0.85;
}
.agent-inspector {
  margin-top: 28px;
  border-top: 1px solid var(--border-tech);
  padding-top: 16px;
}
.agent-inspector summary {
  color: var(--text-sub);
  cursor: pointer;
  font-weight: 600;
  padding: 8px 0;
}
.agent-inspector summary:hover {
  color: var(--accent-lime);
}
.agent-inspector-content {
  padding-top: 14px;
}
.agent-meta-block {
  margin-bottom: 18px;
  padding: 14px;
  background: #0c0e12;
  border-left: 2px solid var(--accent-lime);
}
.meta-label {
  display: block;
  color: var(--accent-lime);
  font-size: 0.78rem;
  font-weight: 700;
  margin-bottom: 10px;
}
.agent-meta-block ol {
  padding-left: 20px;
  color: #c9cbd0;
}
.agent-meta-block li {
  margin: 7px 0;
}
.plan-editor {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}
.plan-editor-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 34px;
  gap: 8px;
}
.plan-editor input {
  min-width: 0;
  border: 1px solid var(--border-tech);
  background: #090b0e;
  color: var(--text-main);
  padding: 9px 10px;
}
.plan-editor button,
.plan-edit-trigger {
  border: 1px solid var(--border-tech);
  background: transparent;
  color: var(--text-sub);
  padding: 7px 10px;
  cursor: pointer;
}
.plan-editor button:hover,
.plan-edit-trigger:hover {
  color: var(--accent-lime);
  border-color: var(--accent-lime);
}
.plan-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.plan-edit-trigger {
  margin-top: 8px;
}
.stage-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.stage-list span,
.quality-row span {
  border: 1px solid var(--border-tech);
  border-radius: 4px;
  padding: 6px 8px;
  color: var(--text-sub);
  font-size: 0.78rem;
}
.quality-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.follow-up-box {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 10px;
  margin-top: 24px;
}
.follow-up-box textarea {
  min-height: 76px;
}
.follow-up-box button {
  align-self: stretch;
  min-width: 76px;
  border: 1px solid var(--accent-lime);
  border-radius: 4px;
  background: rgba(197, 249, 70, 0.08);
  color: var(--accent-lime);
  cursor: pointer;
}
.feedback-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 18px;
  color: var(--text-sub);
  font-size: 0.85rem;
}

/* 登录框 */
.auth-backdrop {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.8);
  backdrop-filter: blur(5px);
  z-index: 2000;
  display: flex;
  justify-content: center;
  align-items: center;
}
.auth-panel {
  width: 400px;
  max-width: 90vw;
  background: var(--bg-card);
  border: 1px solid var(--border-tech);
  border-top: 2px solid var(--accent-lime);
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.8);
  display: flex;
  flex-direction: column;
  animation: slideUpFade 0.3s forwards;
}
.auth-header {
  padding: 20px;
  border-bottom: 1px solid var(--border-tech);
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: rgba(11, 12, 16, 0.9);
}
.auth-title {
  font-family: 'Noto Sans SC', sans-serif;
  font-size: 1.2rem;
  color: var(--text-main);
  font-weight: 700;
  letter-spacing: 1px;
}
.auth-body {
  padding: 30px;
}
.input-group {
  margin-bottom: 20px;
}
.input-group label {
  display: block;
  font-family: 'Noto Sans SC', monospace;
  color: var(--text-sub);
  font-size: 0.75rem;
  margin-bottom: 8px;
  letter-spacing: 1px;
}
.input-group input {
  width: 100%;
  background: #000;
  border: 1px solid var(--border-tech);
  padding: 12px;
  color: var(--text-main);
  font-family: monospace;
  font-size: 1rem;
  outline: none;
  transition: all 0.3s;
}
.input-group input:focus {
  border-color: var(--accent-lime);
  box-shadow: 0 0 10px rgba(197, 249, 70, 0.2);
}
.cyber-btn {
  width: 100%;
  background: var(--text-main);
  color: var(--bg-deep);
  border: none;
  padding: 12px;
  font-weight: 700;
  font-family: 'Noto Sans SC', sans-serif;
  cursor: pointer;
  transition: all 0.3s;
  clip-path: polygon(5% 0%, 100% 0%, 95% 100%, 0% 100%);
  margin-bottom: 20px;
}
.cyber-btn:hover:not(:disabled) {
  background: var(--accent-lime);
  color: var(--text-inverse);
  box-shadow: 0 0 20px rgba(197, 249, 70, 0.4);
}
.cyber-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.auth-toggle {
  text-align: center;
  font-size: 0.85rem;
  font-family: 'Noto Sans SC', sans-serif;
  color: var(--text-sub);
}
.toggle-link {
  background: none;
  border: none;
  color: var(--accent-lime);
  cursor: pointer;
  font-weight: 700;
  margin-left: 5px;
  text-decoration: underline;
}
.toggle-link:hover {
  color: #fff;
}
.auth-msg {
  margin-top: 15px;
  text-align: center;
  font-family: 'Noto Sans SC', monospace;
  font-size: 0.8rem;
  color: var(--accent-lime);
}
.auth-msg.error {
  color: #ff4757;
}

/* 删除按钮 */
.delete-btn {
  position: absolute;
  top: 10px;
  right: 10px;
  background: transparent;
  border: none;
  width: 36px;
  height: 36px;
  color: #71757a;
  cursor: pointer;
  opacity: 0;
  transition:
    color 0.2s ease,
    opacity 0.2s ease;
  z-index: 10;
}
.project-card:hover .delete-btn,
.delete-btn:focus-visible {
  opacity: 1;
}
.delete-btn:hover:not(:disabled) {
  color: #ff4757;
}
.delete-btn:disabled {
  cursor: progress;
  opacity: 0.5;
}
.url-input-box input:disabled,
.url-go-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 720px) {
  .navbar {
    padding: 0.8rem 0;
  }
  .nav-content {
    padding: 0 1rem;
  }
  .brand-do,
  .brand-video {
    font-size: 1.25rem;
  }
  .status-pill {
    display: none;
  }
  .auth-btn {
    padding: 6px 10px;
  }
  .main-container {
    padding: 2.5rem 1rem;
  }
  .hero-section {
    margin-bottom: 3rem;
  }
  .slogan-main {
    font-size: 2rem;
  }
  .slogan-sub {
    margin-bottom: 2rem;
  }
  .upload-magnet {
    height: auto;
    min-height: 420px;
    border-radius: 8px;
  }
  .split-container {
    flex-direction: column;
  }
  .skew-pane,
  .pane-local,
  .pane-url {
    min-height: 210px;
    margin: 0;
    padding: 0;
    transform: none;
  }
  .pane-local {
    border-right: 0;
    border-bottom: 1px solid var(--accent-lime);
  }
  .pane-content,
  .skew-pane:hover .pane-content {
    transform: none;
  }
  .split-gap {
    display: none;
  }
  .card-grid {
    grid-template-columns: 1fr;
  }
  .section-header {
    align-items: stretch;
    flex-direction: column;
  }
  .library-search {
    width: 100%;
  }
  .action-dock {
    grid-template-columns: 1fr;
  }
  .filename-mask {
    max-width: 55vw;
  }
  .sidebar-panel {
    width: 100%;
    max-width: 100vw;
    right: -100vw;
  }
  .sidebar-header {
    padding: 16px 18px;
  }
  .sidebar-title {
    font-size: 1rem;
    max-width: calc(100vw - 70px);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .sidebar-body {
    padding: 20px 16px;
  }
  .video-evidence {
    margin: -20px -16px 22px;
  }
  .video-evidence p,
  .video-evidence-loading {
    padding-left: 16px;
    padding-right: 16px;
  }
  .goal-presets {
    grid-template-columns: 1fr;
  }
  .goal-presets button {
    min-height: 68px;
  }
  .result-actions {
    justify-content: stretch;
  }
  .result-actions button {
    flex: 1;
    padding: 9px 4px;
    font-size: 0.76rem;
  }
  .evidence-search-form {
    grid-template-columns: 1fr;
  }
  .evidence-search-results button {
    grid-template-columns: 56px minmax(0, 1fr);
  }
  .evidence-search-results small {
    display: none;
  }
  .follow-up-box {
    grid-template-columns: 1fr;
  }
  .follow-up-box button {
    min-height: 44px;
  }
  .delete-btn {
    opacity: 1;
  }
  .upload-resume {
    flex-direction: column;
    align-items: stretch;
    text-align: center;
  }
  .upload-resume button {
    min-height: 40px;
  }
  .busy-stat,
  .busy-warning {
    max-width: 92%;
  }
  /*
    移动端侧栏铺满整屏，背景还能滚动会非常割裂，这里锁住。
    只在窄屏生效：移动端滚动条是浮层式的，隐藏溢出不会让内容横向跳动；
    桌面端滚动条占布局宽度，锁滚动会带来 15px 左右的位移，所以不动。
  */
  body.overlay-open {
    overflow: hidden;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
  }
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes slideUpFade {
  from {
    opacity: 0;
    transform: translateY(40px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@keyframes pulse-lime {
  0% {
    opacity: 0.5;
    box-shadow: 0 0 5px var(--accent-lime);
  }
  100% {
    opacity: 1;
    box-shadow: 0 0 15px var(--accent-lime);
  }
}
@keyframes blink {
  50% {
    opacity: 0.5;
  }
}
</style>
