import { computed, ref, onScopeDispose, getCurrentScope } from 'vue'
import { apiRequest, captureAuthSession, onAuthSessionChange } from './api.js'
import {
  DEMO_EVALUATION,
  DEMO_ITEM,
  DEMO_PLAN,
  DEMO_RESULT,
  DEMO_TRACE,
} from './demoData.js'
import { renderMarkdown } from './markdown.js'
import { uiPhaseForStage } from './uiStage.js'

const DEFAULT_GOAL =
  '理解视频核心内容，提炼关键结论，并给出带时间戳的证据和可执行建议'

// 分析模式选项。GENERAL/LEARNING/REVIEW/CREATION 与后端 AnalysisMode 枚举一一对应,value 直接作为 mode 参数;
// AUTO 是纯前端选项:提交前先调 /analysis/route 让 AI 判定出具体模式,再据此发起分析——
// AUTO 本身绝不会作为 mode 发到任何带 key 的后端接口,从根上避免读写端 key 不对称。
const ANALYSIS_MODES = [
  { value: 'AUTO', title: '自动', description: 'AI 按目标智能选择模式' },
  { value: 'GENERAL', title: '通用', description: '结论 · 时间戳证据 · 建议' },
  {
    value: 'LEARNING',
    title: '学习',
    description: '知识点大纲 · 重点难点 · 自测题',
  },
  {
    value: 'REVIEW',
    title: '审查',
    description: '逻辑漏洞 · 夸大表述 · 遗漏点',
  },
  {
    value: 'CREATION',
    title: '创作',
    description: '爆点片段 · 标题 · 口播脚本',
  },
]
const GOAL_PRESETS = [
  {
    title: '学习笔记',
    description: '章节、知识点与复习建议',
    prompt:
      '生成结构化学习笔记，按章节提炼知识点，引用关键时间戳，并给出复习建议',
  },
  {
    title: '会议纪要',
    description: '结论、分歧与待办事项',
    prompt:
      '生成会议纪要，整理核心议题、明确结论、分歧点和待办事项，并引用对应时间戳',
  },
  {
    title: '操作手册',
    description: '步骤、条件与异常处理',
    prompt:
      '生成可执行操作手册，提取前置条件、操作步骤、注意事项和异常处理，并引用对应时间戳',
  },
]
const STAGE_LABELS = {
  VIDEO_CONTEXT: '解析语音与画面',
  RETRIEVAL: '检索相关证据',
  PLANNER: '拆解分析任务',
  EXECUTOR: '生成结构化结果',
  CRITIC: '核验结论与证据',
}

function createSidebarState() {
  return {
    visible: false,
    type: 'ai',
    mode: 'compose',
    title: '',
    content: '',
    error: '',
    loading: false,
    statusMessage: '',
    streamOffline: false,
    streamRetry: 0,
    mediaId: null,
    goal: DEFAULT_GOAL,
    analysisMode: 'GENERAL',
    playbackUrl: '',
    playbackLoading: false,
    playbackError: '',
    followUp: '',
    followUpLoading: false,
    conversationId: null,
    conversationScope: '',
    conversationHistoryLoading: false,
    conversationError: '',
    analysisContent: '',
    evidenceQuery: '',
    evidenceLoading: false,
    evidenceResults: [],
    evidenceError: '',
    plan: null,
    trace: null,
    evaluation: null,
    feedback: null,
    feedbackLoading: false,
    editingPlan: false,
    planDraft: [],
    rerunLoading: false,
  }
}

export function useAnalysisWorkspace({
  demoMode,
  taskStreams,
  showMessage,
  refreshMediaList,
  findMediaItem,
  onAnswerAppended = () => {},
}) {
  const sidebar = ref(createSidebarState())
  let evidenceRequestVersion = 0
  let workspaceGeneration = 0
  let metadataGeneration = 0
  let playbackRequest = 0
  let historyGeneration = 0
  let pendingFollowUp = null
  const invalidateWorkspace = () => {
    workspaceGeneration += 1
    evidenceRequestVersion += 1
    metadataGeneration += 1
    historyGeneration += 1
    sidebar.value.generation = workspaceGeneration
    sidebar.value.followUpLoading = false
    sidebar.value.feedbackLoading = false
    sidebar.value.evidenceLoading = false
    sidebar.value.rerunLoading = false
    sidebar.value.conversationHistoryLoading = false
  }
  const captureWorkspace = () => {
    const generation = workspaceGeneration
    const session = captureAuthSession()
    const state = sidebar.value
    const mediaId = state.mediaId
    const goal = state.goal
    const mode = state.analysisMode
    return () => generation === workspaceGeneration && session.isCurrent()
      && state === sidebar.value && sidebar.value.visible
      && mediaId === sidebar.value.mediaId && goal === sidebar.value.goal
      && mode === sidebar.value.analysisMode
  }
  const traceStages = computed(() => {
    const durations = {}
    for (const [stage, duration] of Object.entries(sidebar.value.trace?.stageDurationMs || {})) {
      const phase = uiPhaseForStage(stage)
      if (phase) durations[phase] = (durations[phase] || 0) + Number(duration || 0)
    }
    return Object.entries(durations).map(([phase, duration]) => [STAGE_LABELS[phase], formatDuration(duration)])
  })
  const renderedMarkdown = computed(() => renderMarkdown(sidebar.value.content))
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
    pendingFollowUp = null
    try { localStorage.setItem(scope, id) } catch { /* in-memory session still works */ }
    return id
  }
  const restoreConversation = async () => {
    const id = ensureConversation()
    if (!id || demoMode) return
    const current = captureWorkspace()
    const request = ++historyGeneration
    sidebar.value.conversationHistoryLoading = true
    sidebar.value.conversationError = ''
    try {
      const params = new URLSearchParams({ id: String(sidebar.value.mediaId),
        goal: sidebar.value.goal, mode: sidebar.value.analysisMode, conversationId: id })
      const response = await apiRequest(`/analysis/follow-up/history?${params}`)
      if (!response.ok) throw new Error(await response.text())
      const history = await response.json()
      if (!current() || request !== historyGeneration || sidebar.value.conversationId !== id) return
      const turns = Array.isArray(history.turns) ? history.turns : []
      const summary = history.summary?.summary_text
      // Rebuild from the original analysis once; repeated restoration must
      // not append identical history or damage timestamp Markdown links.
      sidebar.value.content = sidebar.value.analysisContent
        + (summary ? `\n\n## 较早对话摘要\n${summary}` : '')
        + turns.map(turn => `\n\n## 追问\n${turn.question}\n\n${turn.answer}`).join('')
    } catch (error) {
      if (current() && request === historyGeneration && sidebar.value.conversationId === id)
        sidebar.value.conversationError = error.message || '会话历史暂不可用'
    } finally {
      if (current() && request === historyGeneration && sidebar.value.conversationId === id)
        sidebar.value.conversationHistoryLoading = false
    }
  }
  const startNewConversation = () => {
    invalidateWorkspace()
    ensureConversation(true)
    sidebar.value.content = sidebar.value.analysisContent
    sidebar.value.followUp = ''
    sidebar.value.conversationError = ''
  }
  captureAuthSession()
  const stopAuthListener = onAuthSessionChange(() => resetWorkspace())
  if (getCurrentScope()) onScopeDispose(stopAuthListener)
  const isCurrentWorkspace = (id, type, goal = null, analysisMode = null) =>
    sidebar.value.mediaId === id &&
    sidebar.value.type === type &&
    (goal === null || sidebar.value.goal === goal) &&
    (analysisMode === null || sidebar.value.analysisMode === analysisMode)

  const openSidebar = (type, title) => {
    invalidateWorkspace()
    sidebar.value.visible = true
    sidebar.value.type = type
    sidebar.value.title = title
    sidebar.value.loading = true
    sidebar.value.content = ''
    sidebar.value.error = ''
    sidebar.value.statusMessage = ''
    sidebar.value.streamOffline = false
    sidebar.value.streamRetry = 0
  }

  const closeSidebar = () => {
    invalidateWorkspace()
    if (sidebar.value.type === 'ai' && sidebar.value.mediaId) {
      // goal 与 mode 一起持久化:二者共同决定任务身份,重开时必须成对恢复,
      // 否则会用错模式去查状态,导致"上次的非通用分析结果查不到"。
      saveGoalDraft(sidebar.value.mediaId, sidebar.value.goal)
      saveModeDraft(sidebar.value.mediaId, sidebar.value.analysisMode)
    }
    evidenceRequestVersion += 1
    sidebar.value.visible = false
  }

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

  const refreshAgentMeta = async (
    id,
    goal,
    includeEvaluation,
    analysisMode = sidebar.value.analysisMode || 'GENERAL',
  ) => {
    const current = captureWorkspace()
    const request = ++metadataGeneration
    const params = new URLSearchParams({
      id: String(id),
      goal,
      mode: analysisMode,
    })
    const requests = [
      apiRequest(`/analysis/agent-plan?${params}`),
      apiRequest(`/analysis/agent-trace?${params}`),
    ]
    if (includeEvaluation)
      requests.push(apiRequest(`/analysis/agent-evaluation?${params}`))

    const settled = await Promise.allSettled(requests)
    if (!current() || request !== metadataGeneration) return
    const [plan, trace, evaluation] = await Promise.all(
      settled.map(readSettledJson),
    )
    if (!current() || request !== metadataGeneration) return
    if (plan && !sidebar.value.editingPlan) sidebar.value.plan = plan
    if (trace) sidebar.value.trace = trace
    if (includeEvaluation && evaluation) sidebar.value.evaluation = evaluation
  }

  const startTaskStream = (id, type, goal = '', analysisMode = 'GENERAL') => {
    const current = captureWorkspace()
    const session = captureAuthSession()
    const resolvedMode = analysisMode || 'GENERAL'
    const scope = type === 'ai' ? analysisScope(goal, resolvedMode) : ''
    const isCurrentTask = () => current() &&
      isCurrentWorkspace(
        id,
        type,
        type === 'ai' ? goal : null,
        type === 'ai' ? resolvedMode : null,
      )
    const taskLabel = type === 'ai' ? 'AI 分析' : '文字提取'
    const background = () => session.isCurrent() && (!sidebar.value.visible
      || !isCurrentWorkspace(id, type, type === 'ai' ? goal : null, type === 'ai' ? resolvedMode : null))
    const finish = (result, failed = false, retained = '') => {
      if (!current() && !background()) return
      const watching = sidebar.value.visible && isCurrentTask()
      if (watching) {
        sidebar.value.content = failed ? retained : result
        if (type === 'ai' && !failed) {
          sidebar.value.analysisContent = result
          void restoreConversation()
        }
        sidebar.value.loading = false
        sidebar.value.statusMessage = ''
        sidebar.value.streamOffline = false
        sidebar.value.streamRetry = 0
        sidebar.value.error = failed ? result : ''
        if (failed && type === 'ai') sidebar.value.mode = 'compose'
        if (type === 'ai' && !failed)
          void refreshAgentMeta(id, goal, true, resolvedMode).catch(() => {})
      }
      // 用户可能已经关掉面板去看别的视频，带上文件名才知道是哪个任务结束了。
      const filename = watching ? '' : findMediaItem(id)?.filename || ''
      const suffix = filename ? ` · ${filename}` : ''
      showMessage(
        failed
          ? `${taskLabel}失败${suffix}：${result || '请稍后重试'}`
          : `${taskLabel}完成${suffix}`,
        failed,
      )
      taskStreams.stop(id, type, scope)
    }

    const params = new URLSearchParams({ id: String(id) })
    if (type === 'ai') {
      params.set('goal', goal)
      params.set('mode', resolvedMode)
    }
    const path =
      type === 'ai'
        ? `/analysis/analysis-events?${params}`
        : `/analysis/transcription-events?${params}`

    taskStreams.start(
      id,
      type,
      scope,
      path,
      async (status) => {
        if (!current() && !background()) return
        if (isCurrentTask()) {
          // 收到任何事件都说明连接是通的，先把“重连中”提示撤掉。
          sidebar.value.streamOffline = false
          sidebar.value.streamRetry = 0
          if (
            status.message &&
            (status.state === 'PROCESSING' || status.state === 'QUEUED')
          ) {
            sidebar.value.statusMessage = status.message
          }
        }
        if (type === 'ai' && status.stage && isCurrentTask() && !['COMPLETED', 'FAILED'].includes(status.state)) {
          void refreshAgentMeta(id, goal, false, resolvedMode).catch(() => {})
        }
        if (!current() && !background()) return
        if (status.state === 'COMPLETED') {
          void Promise.resolve(refreshMediaList()).catch(() => {})
          finish(status.result || (type === 'ai' ? '分析完成' : ''))
        } else if (status.state === 'FAILED') {
          finish(status.message || '任务执行失败', true, type === 'text' ? status.result || '' : '')
        }
      },
      (error, attempt, terminal = false) => {
        // isCurrentTask 保证用户已切换视频或关闭面板时，旧任务的错误不会写到新页面上。
        if (terminal) {
          console.warn('task event stream stopped', error)
          if (!isCurrentTask()) return
          sidebar.value.streamOffline = false
          sidebar.value.streamRetry = 0
          sidebar.value.loading = false
          sidebar.value.error = error?.message || '任务事件流已断开，请稍后重试'
          return
        }
        console.warn('task event stream reconnecting', error)
        if (!isCurrentTask()) return
        sidebar.value.streamOffline = true
        sidebar.value.streamRetry = attempt || 1
      },
    )
  }

  const transcribe = async (id) => {
    const item = findMediaItem(id)
    if (demoMode) {
      openSidebar('text', 'ASR 转写结果')
      sidebar.value.mediaId = id
      sidebar.value.content = item?.transcriptText || DEMO_ITEM.transcriptText
      sidebar.value.loading = false
      return
    }
    const panelTitle = item?.filename
      ? `全量文字提取 · ${item.filename}`
      : '全量文字提取'
    if (taskStreams.has(id, 'text')) {
      openSidebar('text', panelTitle)
      sidebar.value.mediaId = id
      sidebar.value.playbackUrl = ''
      loadPlayback(id)
      startTaskStream(id, 'text')
      sidebar.value.statusMessage = '文字提取正在后台继续，进度会自动同步'
      return
    }

    openSidebar('text', panelTitle)
    sidebar.value.mediaId = id
    sidebar.value.playbackUrl = ''
    loadPlayback(id)
    const current = captureWorkspace()
    sidebar.value.statusMessage = '提取任务已提交，正在识别语音'
    try {
      const statusResponse = await apiRequest(
        `/analysis/transcription-status?id=${id}`,
      )
      if (!statusResponse.ok) throw new Error(await statusResponse.text())
      const currentStatus = await statusResponse.json()
      if (!current()) return
      if (currentStatus.state === 'COMPLETED') {
        if (current()) {
          sidebar.value.content = currentStatus.result || ''
          sidebar.value.statusMessage = ''
          sidebar.value.loading = false
        }
        return
      }
      if (currentStatus.state === 'FAILED' && currentStatus.result) {
        sidebar.value.content = currentStatus.result
        sidebar.value.error = currentStatus.message || '本次提取失败，保留上次文字'
        sidebar.value.loading = false
        sidebar.value.statusMessage = ''
        return
      }
      if (
        currentStatus.state === 'QUEUED' ||
        currentStatus.state === 'PROCESSING'
      ) {
        if (current()) {
          sidebar.value.statusMessage = currentStatus.message
        }
        if (current()) startTaskStream(id, 'text')
        return
      }
      const response = await apiRequest(`/analysis/transcribe?id=${id}`, {
        method: 'POST',
      })
      if (!response.ok) throw new Error(await response.text())
      if (current()) startTaskStream(id, 'text')
    } catch (error) {
      if (current()) {
        sidebar.value.content = ''
        sidebar.value.statusMessage = ''
        sidebar.value.error = error.message || '文字提取失败，请稍后重试'
        sidebar.value.loading = false
      }
    }
  }

  const analyze = async (id, goal, mode = 'GENERAL') => {
    const current = captureWorkspace()
    const resolvedMode = mode || 'GENERAL'
    const scope = analysisScope(goal, resolvedMode)
    if (taskStreams.has(id, 'ai', scope)) {
      sidebar.value.mode = 'result'
      sidebar.value.loading = true
      startTaskStream(id, 'ai', goal, resolvedMode)
      sidebar.value.statusMessage = '这个目标已有分析在进行，正在接管进度'
      return
    }

    sidebar.value.loading = true
    sidebar.value.mode = 'result'
    sidebar.value.content = ''
    sidebar.value.statusMessage = '任务已提交，正在排队进入 Agent 流水线'
    sidebar.value.streamOffline = false
    sidebar.value.streamRetry = 0
    try {
      const params = new URLSearchParams({
        id: String(id),
        goal,
        mode: resolvedMode,
      })
      const response = await apiRequest(`/analysis/ai?${params}`, {
        method: 'POST',
      })
      const message = await response.text()
      if (!current()) return
      if (response.status === 409) {
        if (current()) startTaskStream(id, 'ai', goal, resolvedMode)
        refreshAgentMeta(id, goal, false, resolvedMode)
        return
      }
      if (!response.ok) {
        if (current()) {
          showMessage(message, true)
          sidebar.value.loading = false
          sidebar.value.statusMessage = ''
          sidebar.value.mode = 'compose'
          sidebar.value.error = message
        }
        return
      }
      if (current()) startTaskStream(id, 'ai', goal, resolvedMode)
      refreshAgentMeta(id, goal, false, resolvedMode)
    } catch (error) {
      if (current()) {
        sidebar.value.mode = 'compose'
        sidebar.value.error = error.message || String(error)
        sidebar.value.loading = false
        sidebar.value.statusMessage = ''
      }
    }
  }

  const openAgent = async (item) => {
    invalidateWorkspace()
    evidenceRequestVersion += 1
    const goal = loadGoalDraft(item.id)
    // 恢复上次使用的模式,与 goal 一起构成任务身份;缺省为通用模式。
    const analysisMode = loadModeDraft(item.id)
    sidebar.value = {
      ...createSidebarState(),
      generation: workspaceGeneration,
      visible: true,
      title: `Video Agent · ${item.filename}`,
      mediaId: item.id,
      goal,
      analysisMode,
    }
    if (demoMode) return
    const current = captureWorkspace()

    loadPlayback(item.id)
    try {
      const params = new URLSearchParams({
        id: String(item.id),
        goal,
        mode: sidebar.value.analysisMode || 'GENERAL',
      })
      const response = await apiRequest(`/analysis/analysis-status?${params}`)
      if (!response.ok) {
        const detail = await response.text()
        throw new Error(detail || '历史分析状态加载失败')
      }
      const status = await response.json()
      if (
        !current() || sidebar.value.mediaId !== item.id ||
        sidebar.value.goal !== goal ||
        sidebar.value.analysisMode !== analysisMode
      )
        return

      if (status.state === 'COMPLETED') {
        sidebar.value.mode = 'result'
        sidebar.value.content = status.result || ''
        sidebar.value.analysisContent = status.result || ''
        sidebar.value.loading = false
        await restoreConversation()
        await refreshAgentMeta(item.id, goal, true, analysisMode)
      } else if (status.state === 'QUEUED' || status.state === 'PROCESSING') {
        sidebar.value.mode = 'result'
        sidebar.value.loading = true
        sidebar.value.statusMessage =
          status.message || '正在恢复上一次未完成的分析任务'
        startTaskStream(item.id, 'ai', goal, analysisMode)
        await refreshAgentMeta(item.id, goal, false, analysisMode)
      } else if (status.state === 'FAILED') {
        sidebar.value.error = status.message || '上次分析未完成，可以重新提交'
      }
    } catch (error) {
      console.warn('Previous analysis unavailable', error)
      if (
        current() && sidebar.value.mediaId === item.id &&
        sidebar.value.goal === goal &&
        sidebar.value.analysisMode === analysisMode
      ) {
        sidebar.value.error =
          error.message || '历史分析状态加载失败，可以重新提交'
      }
    }
  }

  const showDemoResult = () => {
    sidebar.value.mode = 'result'
    sidebar.value.loading = false
    sidebar.value.content = DEMO_RESULT
    if (!sidebar.value.plan) sidebar.value.plan = DEMO_PLAN
    sidebar.value.trace = DEMO_TRACE
    sidebar.value.evaluation = DEMO_EVALUATION
  }

  // AUTO 模式的意图路由:仅凭目标文本让后端 AI 判定出具体模式。apiRequest 已解包信封,
  // response.json() 直接就是 { mode, reason }。失败由调用方兜底,这里只负责发起请求。
  const routeMode = async (goal) => {
    const response = await apiRequest('/analysis/route', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal }),
    })
    if (!response.ok) throw new Error(await response.text())
    return response.json()
  }

  const submitAgent = async () => {
    const goal = sidebar.value.goal.trim()
    if (!goal || sidebar.value.loading) return
    const mediaId = sidebar.value.mediaId
    // 后端、SSE scope 与异步回调必须使用同一份归一化目标，否则前后空格会让当前任务判定失配。
    invalidateWorkspace()
    sidebar.value.goal = goal
    const current = captureWorkspace()
    sidebar.value.error = ''
    saveGoalDraft(mediaId, goal)
    if (demoMode) {
      sidebar.value.mode = 'result'
      sidebar.value.loading = true
      sidebar.value.plan = DEMO_PLAN
      sidebar.value.trace = DEMO_TRACE
      setTimeout(() => { if (current()) showDemoResult() }, 450)
      return
    }
    let mode = sidebar.value.analysisMode || 'GENERAL'
    if (mode === 'AUTO') {
      // 先把 AUTO 落定成具体模式,再提交。落定结果写回 sidebar.analysisMode,
      // 之后的状态查询 / 重跑 / 元信息刷新都用它,确保与提交端 key 完全一致。
      sidebar.value.mode = 'result'
      sidebar.value.loading = true
      sidebar.value.content = ''
      sidebar.value.statusMessage = '正在识别分析意图…'
      let decision = null
      try {
        decision = await routeMode(goal)
      } catch (error) {
        console.warn('intent routing failed', error)
      }
      // 路由是异步的,用户可能已切到别的视频或改了目标;仅当仍停留在同一任务时才落定并继续。
      if (
        !current() || sidebar.value.mediaId !== mediaId ||
        sidebar.value.goal.trim() !== goal
      )
        return
      if (decision && decision.mode) {
        mode = decision.mode
        showMessage(
          `AI 已识别为「${modeTitle(mode)}」模式：${decision.reason || ''}`.trim(),
        )
      } else {
        mode = 'GENERAL'
        showMessage('意图识别暂不可用，已按通用模式分析', true)
      }
      sidebar.value.analysisMode = mode
    }
    // 记住这次实际使用的具体模式,重开面板时据此恢复,保证能查回本次分析结果。
    saveModeDraft(mediaId, mode)
    return analyze(mediaId, goal, mode)
  }

  const startNewAnalysis = () => {
    invalidateWorkspace()
    sidebar.value.mode = 'compose'
    sidebar.value.loading = false
    sidebar.value.error = ''
  }

  const startPlanEdit = () => {
    sidebar.value.planDraft = [...(sidebar.value.plan?.tasks || [])]
    sidebar.value.editingPlan = true
  }

  const cancelPlanEdit = () => {
    sidebar.value.editingPlan = false
    sidebar.value.planDraft = []
  }

  const addPlanTask = () => {
    if (sidebar.value.planDraft.length < 5) sidebar.value.planDraft.push('')
  }

  const removePlanTask = (index) => {
    if (sidebar.value.planDraft.length > 1)
      sidebar.value.planDraft.splice(index, 1)
  }

  const rerunWithPlan = async () => {
    if (sidebar.value.rerunLoading) return
    const tasks = sidebar.value.planDraft
      .map((task) => task.trim())
      .filter(Boolean)
    if (!tasks.length || tasks.length > 5) {
      showMessage('计划需保留 1 至 5 个有效任务', true)
      return
    }
    invalidateWorkspace()
    const current = captureWorkspace()
    if (demoMode) {
      sidebar.value.plan = { ...DEMO_PLAN, tasks }
      cancelPlanEdit()
      sidebar.value.loading = true
      setTimeout(() => { if (current()) showDemoResult() }, 450)
      return
    }

    const mediaId = sidebar.value.mediaId
    const goal = sidebar.value.goal
    const analysisMode = sidebar.value.analysisMode || 'GENERAL'
    sidebar.value.rerunLoading = true
    try {
      const reviseParams = new URLSearchParams({ mode: analysisMode })
      const response = await apiRequest(
        `/analysis/agent-revise?${reviseParams}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            mediaId,
            goal,
            correctedTasks: tasks,
            comment: '用户调整 Planner 任务后重新执行',
          }),
        },
      )
      const message = await response.text()
      if (!current()) return
      if (!response.ok) throw new Error(message || '重新提交失败')
      if (current()) {
        sidebar.value.plan = { ...sidebar.value.plan, tasks }
        cancelPlanEdit()
        sidebar.value.content = ''
        sidebar.value.loading = true
        sidebar.value.statusMessage = '已按新计划重新提交，正在重新执行'
        sidebar.value.streamOffline = false
        sidebar.value.streamRetry = 0
      }
      if (current()) startTaskStream(mediaId, 'ai', goal, analysisMode)
    } catch (error) {
      if (current()) {
        showMessage(error.message || '重新提交失败', true)
      }
    } finally {
      if (current())
        sidebar.value.rerunLoading = false
    }
  }

  const submitFollowUp = async () => {
    const question = sidebar.value.followUp.trim()
    if (!question || sidebar.value.followUpLoading || sidebar.value.conversationHistoryLoading) return
    if (demoMode) {
      sidebar.value.content += `\n\n## 追问\n${question}\n\n根据 08:42 的讲解，迭代写法使用显式栈保存待访问节点，时间复杂度仍为 O(n)，额外空间复杂度为 O(h)。`
      sidebar.value.followUp = ''
      onAnswerAppended()
      return
    }

    const current = captureWorkspace()
    const mediaId = sidebar.value.mediaId
    const goal = sidebar.value.goal
    const analysisMode = sidebar.value.analysisMode || 'GENERAL'
    sidebar.value.followUpLoading = true
    const conversationId = ensureConversation()
    if (conversationId && (!pendingFollowUp || pendingFollowUp.question !== question || pendingFollowUp.conversationId !== conversationId))
      pendingFollowUp = { question, conversationId, requestId: globalThis.crypto.randomUUID() }
    try {
      const params = new URLSearchParams({
        id: String(mediaId),
        question,
        goal,
        mode: analysisMode,
      })
      if (conversationId) {
        params.set('conversationId', conversationId)
        params.set('requestId', pendingFollowUp.requestId)
      }
      const response = await apiRequest(`/analysis/follow-up?${params}`, {
        method: 'POST',
      })
      const answer = await response.text()
      if (!response.ok) throw new Error(answer || '追问失败')
      if (current() && sidebar.value.conversationId === conversationId) {
        historyGeneration += 1
        sidebar.value.conversationHistoryLoading = false
        sidebar.value.content += `\n\n## 追问\n${question}\n\n${answer}`
        sidebar.value.followUp = ''
        pendingFollowUp = null
        // 答案追加在长文末尾，主动带用户滚过去，否则会以为“点了没反应”。
        onAnswerAppended()
      }
    } catch (error) {
      if (current()) {
        showMessage(`❌ ${error.message}`, true)
      }
    } finally {
      if (current())
        sidebar.value.followUpLoading = false
    }
  }

  const searchEvidence = async () => {
    const query = sidebar.value.evidenceQuery.trim()
    if (!query || sidebar.value.evidenceLoading) return
    const requestVersion = ++evidenceRequestVersion
    const current = captureWorkspace()
    const mediaId = sidebar.value.mediaId
    sidebar.value.evidenceLoading = true
    sidebar.value.evidenceError = ''
    sidebar.value.evidenceResults = []
    try {
      if (demoMode) {
        const demoResults = [
          {
            startMs: 522000,
            endMs: 582000,
            source: 'ASR+OCR',
            snippet:
              '迭代遍历使用显式栈保存待访问节点，画面展示了前序遍历顺序。',
            transcript: '迭代遍历使用显式栈保存待访问节点。',
            ocrTexts: ['前序遍历：根节点、左子树、右子树'],
          },
        ]
        if (current() && requestVersion === evidenceRequestVersion) {
          sidebar.value.evidenceResults = demoResults
        }
        return
      }
      const params = new URLSearchParams({
        id: String(mediaId),
        query,
      })
      const response = await apiRequest(`/analysis/evidence-search?${params}`)
      if (!response.ok) {
        const detail = await response.text()
        throw new Error(detail || '视频证据检索失败')
      }
      const results = await response.json()
      if (
        !current() || requestVersion !== evidenceRequestVersion ||
        sidebar.value.mediaId !== mediaId
      )
        return
      sidebar.value.evidenceResults = Array.isArray(results) ? results : []
      if (!sidebar.value.evidenceResults.length) {
        sidebar.value.evidenceError = '没有找到匹配的视频证据'
      }
    } catch (error) {
      if (
        !current() || requestVersion !== evidenceRequestVersion ||
        sidebar.value.mediaId !== mediaId
      )
        return
      sidebar.value.evidenceResults = []
      sidebar.value.evidenceError = error.message || '视频证据检索失败'
    } finally {
      if (
        current() && requestVersion === evidenceRequestVersion &&
        sidebar.value.mediaId === mediaId
      ) {
        sidebar.value.evidenceLoading = false
      }
    }
  }

  const sendFeedback = async (rating) => {
    if (sidebar.value.feedbackLoading || sidebar.value.feedback === rating)
      return
    if (demoMode) {
      sidebar.value.feedback = rating
      showMessage('演示反馈已记录')
      return
    }
    const current = captureWorkspace()
    const mediaId = sidebar.value.mediaId
    const goal = sidebar.value.goal
    const analysisMode = sidebar.value.analysisMode || 'GENERAL'
    sidebar.value.feedbackLoading = true
    try {
      const response = await apiRequest('/analysis/agent-feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mediaId, goal, mode: analysisMode, rating }),
      })
      if (!response.ok) throw new Error(await response.text())
      if (current()) {
        sidebar.value.feedback = rating
        showMessage('反馈已记录')
      }
    } catch (error) {
      if (current()) {
        showMessage(`❌ ${error.message}`, true)
      }
    } finally {
      if (current())
        sidebar.value.feedbackLoading = false
    }
  }

  const retryPlayback = () => {
    if (sidebar.value.mediaId && !sidebar.value.playbackLoading) {
      loadPlayback(sidebar.value.mediaId)
    }
  }

  const handlePlaybackError = () => {
    if (!sidebar.value.playbackUrl) return
    sidebar.value.playbackUrl = ''
    sidebar.value.playbackError =
      '视频无法播放：可能是播放地址不可用，或视频编码不受当前浏览器支持。请重新加载；链接导入的视频可重新导入后再试。'
  }

  const resetWorkspace = () => {
    invalidateWorkspace()
    evidenceRequestVersion += 1
    sidebar.value = { ...createSidebarState(), generation: workspaceGeneration }
    pendingFollowUp = null
  }

  const discardMediaWorkspace = (mediaId) => {
    taskStreams.stopMedia(mediaId)
    try {
      localStorage.removeItem(goalDraftKey(mediaId))
      localStorage.removeItem(modeDraftKey(mediaId))
      const prefix = `videomind:conversation:${userId()}:${mediaId}:`
      for (let index = localStorage.length - 1; index >= 0; index--) {
        const key = localStorage.key(index)
        if (key?.startsWith(prefix)) localStorage.removeItem(key)
      }
    } catch {
      // Storage being unavailable should not block media deletion.
    }
    if (sidebar.value.mediaId === mediaId) resetWorkspace()
  }

  return {
    sidebar,
    goalPresets: GOAL_PRESETS,
    analysisModes: ANALYSIS_MODES,
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
    restoreConversation,
    searchEvidence,
    sendFeedback,
    retryPlayback,
    handlePlaybackError,
    resetWorkspace,
    discardMediaWorkspace,
    formatPercent: (value) => `${Math.round((Number(value) || 0) * 100)}%`,
  }
}

async function readSettledJson(result) {
  if (result.status !== 'fulfilled' || !result.value.ok) return null
  try {
    return await result.value.json()
  } catch (error) {
    console.warn('Agent metadata response is invalid', error)
    return null
  }
}

function modeTitle(value) {
  return ANALYSIS_MODES.find((m) => m.value === value)?.title || value
}

function analysisScope(goal, mode) {
  return `${mode || 'GENERAL'}:${goal}`
}

function formatDuration(value) {
  const milliseconds = Number(value) || 0
  if (milliseconds < 1000) return `${Math.round(milliseconds)} 毫秒`
  return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 1 : 0)} 秒`
}

function goalDraftKey(mediaId) {
  return `dovideo:goal:${mediaId}`
}

function loadGoalDraft(mediaId) {
  try {
    return localStorage.getItem(goalDraftKey(mediaId)) || DEFAULT_GOAL
  } catch {
    return DEFAULT_GOAL
  }
}

function saveGoalDraft(mediaId, goal) {
  if (!mediaId || !goal?.trim()) return
  try {
    localStorage.setItem(goalDraftKey(mediaId), goal.trim())
  } catch {
    // Private browsing can disable storage; the current session still works.
  }
}

function modeDraftKey(mediaId) {
  return `dovideo:mode:${mediaId}`
}

function loadModeDraft(mediaId) {
  try {
    return localStorage.getItem(modeDraftKey(mediaId)) || 'GENERAL'
  } catch {
    return 'GENERAL'
  }
}

// 只持久化“具体模式”。AUTO 是临时选择,落定后才是真实模式;绝不把 AUTO 写进草稿,
// 否则重开时会用 AUTO 去查状态(后端按 GENERAL 解析),反而查不回真正的结果。
function saveModeDraft(mediaId, mode) {
  if (!mediaId || !mode || mode === 'AUTO') return
  try {
    localStorage.setItem(modeDraftKey(mediaId), mode)
  } catch {
    // Private browsing can disable storage; the current session still works.
  }
}
