<template>
  <main
    ref="root"
    class="analysis-workspace"
    tabindex="-1"
    aria-label="视频分析工作台"
  >
    <header class="analysis-workspace__header">
      <div class="analysis-workspace__identity">
        <button
          type="button"
          class="analysis-back"
          @click="actions.closeSidebar"
        >
          ← 媒体库
        </button>
        <span class="analysis-workspace__divider">/</span>
        <span
          class="analysis-workspace__filename"
          :title="media?.filename || sidebar.title"
        >
          {{ media?.filename || sidebar.title }}
        </span>
        <span class="analysis-workspace__id">ID {{ sidebar.mediaId }}</span>
      </div>
      <span
        class="analysis-workspace__state"
        :class="media?.status?.toLowerCase()"
      >
        {{ mediaStatusText }}
      </span>
    </header>

    <div
      v-if="media && media.status !== 'COMPLETED'"
      class="analysis-unavailable"
      role="status"
    >
      <AcademyMark />
      <h1>{{ media.status === "FAILED" ? "视频处理失败" : "分析尚未就绪" }}</h1>
      <p>
        {{
          media.status === "FAILED"
            ? "请检查媒体状态，或重新导入视频。"
            : "视频仍在处理。完成后即可查看原片并开始分析。"
        }}
      </p>
      <div>
        <button type="button" @click="actions.closeSidebar">返回媒体库</button>
        <button type="button" @click="actions.refreshMediaList">
          刷新状态
        </button>
      </div>
    </div>

    <div v-else class="analysis-workspace__grid">
      <aside class="analysis-evidence" aria-labelledby="evidence-title">
        <div class="analysis-section-head">
          <h2 id="evidence-title">证据检索</h2>
          <span>ASR / OCR</span>
        </div>
        <p class="analysis-evidence__hint">
          搜索视频中的语音与画面文字。结果来自当前视频的真实索引。
        </p>
        <div v-if="citations.length" class="analysis-citations">
          <h3>本次回答引用</h3>
          <button
            v-for="(citation, index) in citations"
            :key="citation.id"
            type="button"
            class="analysis-evidence__item"
            :class="{
              'is-selected': selectedKey === `citation:${citation.id}`,
            }"
            :aria-pressed="selectedKey === `citation:${citation.id}`"
            @click="selectCitation(citation)"
          >
            <span class="analysis-evidence__top"
              ><strong
                >证据 {{ String(index + 1).padStart(2, "0") }} ·
                {{ formatMediaTime(citation.timestampMs / 1000) }}</strong
              ><small>{{ citation.source }}</small></span
            >
            <span class="analysis-evidence__snippet">{{
              citation.content
            }}</span>
          </button>
        </div>
        <form
          class="analysis-evidence__search"
          @submit.prevent="actions.searchEvidence"
        >
          <input
            v-model="sidebar.evidenceQuery"
            maxlength="500"
            type="search"
            aria-label="检索视频证据"
            placeholder="搜索内容或时间线索"
          />
          <button
            type="submit"
            :disabled="sidebar.evidenceLoading || !sidebar.evidenceQuery.trim()"
          >
            {{ sidebar.evidenceLoading ? "检索中…" : "检索" }}
          </button>
        </form>
        <p
          v-if="sidebar.evidenceError"
          class="analysis-inline-error"
          role="status"
        >
          {{ sidebar.evidenceError }}
        </p>
        <div
          v-if="sidebar.evidenceLoading"
          class="analysis-local-loading"
          role="status"
        >
          正在检索视频证据…
        </div>
        <div
          v-else-if="sidebar.evidenceResults.length"
          class="analysis-evidence__results"
        >
          <button
            v-for="(hit, index) in sidebar.evidenceResults"
            :key="evidenceKey(hit, index)"
            type="button"
            class="analysis-evidence__item"
            :class="{ 'is-selected': selectedKey === evidenceKey(hit, index) }"
            :aria-pressed="selectedKey === evidenceKey(hit, index)"
            @click="selectEvidence(hit, evidenceKey(hit, index))"
          >
            <span class="analysis-evidence__top">
              <strong>
                {{
                  validEvidenceTime(hit)
                    ? formatMediaTime(hit.startMs / 1000)
                    : "时间未知"
                }}
              </strong>
              <small>{{ hit.source || "视频证据" }}</small>
            </span>
            <span class="analysis-evidence__snippet">
              {{
                hit.snippet ||
                hit.transcript ||
                hit.ocrTexts?.join(" · ") ||
                "该时间段暂无可展示文本"
              }}
            </span>
            <span v-if="hit.transcript" class="analysis-evidence__kind">
              ASR
            </span>
            <span
              v-if="hit.ocrTexts?.length"
              class="analysis-evidence__kind is-ocr"
            >
              OCR
            </span>
          </button>
        </div>
        <div
          v-else-if="!sidebar.evidenceQuery"
          class="analysis-evidence__empty"
        >
          输入关键词后，可以在这里查看可跳转的证据记录。
        </div>
        <div class="analysis-evidence__foot">
          证据命中仅代表检索结果；AI 回答与命中的对应关系以实际引用为准。
        </div>
      </aside>

      <div class="analysis-center">
        <section class="analysis-viewer" aria-labelledby="viewer-title">
          <div class="analysis-section-head">
            <h2 id="viewer-title">媒体查看器</h2>
            <span>源视频 / ID {{ sidebar.mediaId }}</span>
          </div>
          <div class="analysis-viewer__stage">
            <video
              v-if="sidebar.playbackUrl"
              ref="videoPlayer"
              :src="sidebar.playbackUrl"
              controls
              playsinline
              preload="metadata"
              @loadedmetadata="updateDuration"
              @timeupdate="updateTime"
              @durationchange="updateDuration"
              @error="actions.handlePlaybackError"
            ></video>
            <div
              v-else
              class="analysis-viewer__placeholder"
              :role="sidebar.playbackError ? 'alert' : 'status'"
            >
              <AcademyMark />
              <strong>
                {{
                  sidebar.playbackLoading
                    ? "正在载入原视频…"
                    : sidebar.playbackError
                      ? "视频加载失败"
                      : demoMode
                        ? "演示数据没有原视频"
                        : "暂无可播放原片"
                }}
              </strong>
              <p v-if="sidebar.playbackError">{{ sidebar.playbackError }}</p>
              <button
                v-if="sidebar.playbackError"
                type="button"
                @click="actions.retryPlayback"
              >
                重新加载
              </button>
            </div>
          </div>
          <div class="analysis-viewer__meta">
            <span>
              {{ formatMediaTime(currentTime) }}
              <span aria-hidden="true">/</span>
              {{ duration ? formatMediaTime(duration) : "--:--" }}
            </span>
            <span>使用播放器原生控制进行播放与定位</span>
          </div>
        </section>

        <AnalysisTimeline
          :hits="sidebar.evidenceResults"
          :windows="temporalWindows"
          :window-total="temporalTotal"
          :observations="temporalObservations"
          :observation-total="observationTotal"
          :citations="citations"
          :duration="duration"
          :current-time="currentTime"
          :selected-key="selectedKey"
          @select="selectTimelineItem"
        />

        <TemporalRecords
          :windows="temporalWindows"
          :observations="temporalObservations"
          :observation-total="observationTotal"
          :observation-loading="observationLoading"
          :observation-error="observationError"
          :total="temporalTotal"
          :available="temporalAvailable"
          :loading="temporalLoading"
          :error="temporalError"
          :selected-key="selectedKey"
          @select="selectTemporalRecord"
          @retry="loadTemporal(true)"
          @more="loadTemporal(false)"
          @retry-observations="loadObservations(true)"
          @more-observations="loadObservations(false)"
        />

        <section class="analysis-transcript" aria-labelledby="transcript-title">
          <div class="analysis-section-head">
            <h2 id="transcript-title">转录文本</h2>
            <button type="button" @click="refreshTranscript">刷新</button>
          </div>
          <p
            v-if="sidebar.type === 'text' && sidebar.loading"
            class="analysis-local-loading"
            role="status"
          >
            {{ sidebar.statusMessage || "正在识别语音…" }}
          </p>
          <p
            v-else-if="sidebar.type === 'text' && sidebar.error"
            class="analysis-inline-error"
            role="alert"
          >
            {{ sidebar.error }}
          </p>
          <p
            v-else-if="transcriptLoading"
            class="analysis-local-loading"
            role="status"
          >
            正在读取转录状态…
          </p>
          <div v-else-if="transcriptText" class="analysis-transcript__content">
            <p>{{ transcriptText }}</p>
            <span>当前接口仅提供全文，没有片段时间戳。</span>
          </div>
          <p
            v-else-if="transcriptError"
            class="analysis-inline-error"
            role="alert"
          >
            {{ transcriptError }}
          </p>
          <p v-else class="analysis-transcript__empty">{{ transcriptHint }}</p>
          <button
            v-if="sidebar.type === 'ai' && !transcriptText"
            type="button"
            class="analysis-text-action"
            @click="actions.transcribe(sidebar.mediaId)"
          >
            提取文字
          </button>
          <button
            v-if="sidebar.type === 'text'"
            type="button"
            class="analysis-text-action"
            @click="actions.openAgent(media)"
          >
            返回视频分析
          </button>
        </section>
      </div>

      <aside
        ref="answerPanel"
        class="analysis-assistant"
        aria-labelledby="assistant-title"
      >
        <div class="analysis-section-head analysis-assistant__head">
          <h2 id="assistant-title">
            {{ sidebar.type === "ai" ? "AI 助手" : "文字提取" }}
          </h2>
          <span>{{ sidebar.type === "ai" ? "VIDEO RESEARCH" : "ASR" }}</span>
        </div>
        <template v-if="sidebar.type === 'ai'">
          <div v-if="sidebar.mode === 'compose'" class="analysis-composer">
            <p class="analysis-assistant__label">选择分析模式</p>
            <div class="analysis-modes">
              <button
                v-for="mode in analysisModes"
                :key="mode.value"
                type="button"
                :class="{ 'is-active': sidebar.analysisMode === mode.value }"
                :aria-pressed="sidebar.analysisMode === mode.value"
                :title="mode.description"
                @click="sidebar.analysisMode = mode.value"
              >
                {{ mode.title }}
              </button>
            </div>
            <p v-if="sidebar.error" class="analysis-inline-error" role="alert">
              {{ sidebar.error }}
            </p>
            <label class="analysis-assistant__label" for="analysis-goal">
              想从视频中了解什么？
            </label>
            <textarea
              id="analysis-goal"
              v-model="sidebar.goal"
              maxlength="500"
              placeholder="询问视频内容、画面或时间点…"
              @keydown.ctrl.enter.prevent="actions.submitAgent"
              @keydown.meta.enter.prevent="actions.submitAgent"
            ></textarea>
            <span v-if="sidebar.goal.length > 400" class="analysis-counter">
              {{ sidebar.goal.length }} / 500 字
            </span>
            <div class="analysis-presets">
              <button
                v-for="preset in goalPresets"
                :key="preset.title"
                type="button"
                :class="{ 'is-active': sidebar.goal === preset.prompt }"
                :title="preset.description"
                @click="sidebar.goal = preset.prompt"
              >
                {{ preset.title }}
              </button>
            </div>
            <button
              type="button"
              class="analysis-primary-action"
              :disabled="!sidebar.goal.trim()"
              @click="actions.submitAgent"
            >
              {{ sidebar.error ? "重新分析" : "开始分析" }}
            </button>
          </div>
          <div
            v-else-if="sidebar.loading"
            class="analysis-running"
            role="status"
          >
            <div class="analysis-running__segments" aria-hidden="true">
              <span></span>
              <span></span>
              <span></span>
              <span></span>
              <span></span>
            </div>
            <strong>{{ loadingHeadline }}</strong>
            <p v-if="sidebar.streamOffline" class="analysis-inline-error">
              连接中断，正在自动重连（第
              {{ sidebar.streamRetry }} 次）；任务仍在服务端继续。
            </p>
            <p>返回媒体库后任务仍会在后台继续。</p>
            <div
              v-if="sidebar.plan?.tasks?.length"
              class="analysis-plan-summary"
            >
              <span>任务计划</span>
              <ol>
                <li v-for="task in sidebar.plan.tasks" :key="task">
                  {{ task }}
                </li>
              </ol>
            </div>
            <div v-if="traceStages.length" class="analysis-plan-summary">
              <span>已完成阶段</span>
              <p v-for="stage in traceStages" :key="stage[0]">
                {{ stage[0] }} · {{ stage[1] }}
              </p>
            </div>
          </div>
          <div v-else class="analysis-answer">
            <div class="analysis-answer__tools">
              <button type="button" @click="actions.startNewAnalysis">
                更换产物
              </button>
              <button
                type="button"
                :disabled="!sidebar.content"
                @click="actions.copyResult"
              >
                复制结果
              </button>
              <button
                type="button"
                :disabled="!sidebar.content"
                @click="actions.downloadResult"
              >
                导出 Markdown
              </button>
            </div>
            <p v-if="sidebar.error" class="analysis-inline-error" role="alert">
              {{ sidebar.error }}
            </p>
            <section v-if="claims.length" class="analysis-claims" aria-label="核心结论与来源证据">
              <h3>核心结论</h3>
              <div v-for="(item, index) in claims" :key="index" class="analysis-claims__item">
                <strong>{{ item.claim }}</strong>
                <div v-if="item.citations.length" class="analysis-answer__citations">
                  <span>相关证据：</span>
                  <button v-for="citation in item.citations" :key="citation.id"
                    type="button" :aria-pressed="selectedKey === `citation:${citation.id}`"
                    @click="selectCitation(citation)">
                    {{ citation.source }} · {{ formatMediaTime(citation.timestampMs / 1000) }}
                  </button>
                </div>
                <p v-else>暂无通过校验的可绑定证据。</p>
              </div>
            </section>
            <div
              v-if="sidebar.content"
              class="analysis-answer__body markdown-content"
              v-html="renderedMarkdown"
              @click="handleAnswerClick"
            ></div>
            <p v-else class="analysis-answer__empty">
              还没有分析结果。选择目标后开始分析。
            </p>
            <p v-if="sidebar.content" class="analysis-answer__citation-hint">
              回答中的时间戳可跳转视频；结构化引用仅包含已核验的来源，检索结果仍是独立查询。
            </p>
            <div v-if="citations.length" class="analysis-answer__citations">
              <strong>已核验的回答证据</strong>
              <button
                v-for="(citation, index) in citations"
                :key="citation.id"
                type="button"
                @click="selectCitation(citation)"
              >
                证据 {{ String(index + 1).padStart(2, "0") }} ·
                {{ formatMediaTime(citation.timestampMs / 1000) }}
              </button>
            </div>
            <details
              v-if="
                sidebar.plan?.tasks?.length ||
                traceStages.length ||
                sidebar.evaluation
              "
              class="analysis-details"
            >
              <summary>分析详情与任务计划</summary>
              <div v-if="sidebar.plan?.tasks?.length">
                <strong>任务计划</strong>
                <div v-if="sidebar.editingPlan" class="analysis-plan-editor">
                  <div v-for="(_, index) in sidebar.planDraft" :key="index">
                    <input
                      v-model="sidebar.planDraft[index]"
                      maxlength="500"
                      :aria-label="`任务 ${index + 1}`"
                    />
                    <button
                      type="button"
                      :aria-label="`删除任务 ${index + 1}`"
                      @click="actions.removePlanTask(index)"
                    >
                      ×
                    </button>
                  </div>
                  <button
                    v-if="sidebar.planDraft.length < 5"
                    type="button"
                    @click="actions.addPlanTask"
                  >
                    添加任务
                  </button>
                  <button type="button" @click="actions.cancelPlanEdit">
                    取消
                  </button>
                  <button
                    type="button"
                    :disabled="sidebar.rerunLoading"
                    @click="actions.rerunWithPlan"
                  >
                    {{ sidebar.rerunLoading ? "提交中" : "按新计划重跑" }}
                  </button>
                </div>
                <template v-else>
                  <ol>
                    <li v-for="task in sidebar.plan.tasks" :key="task">
                      {{ task }}
                    </li>
                  </ol>
                  <button type="button" @click="actions.startPlanEdit">
                    调整计划
                  </button>
                </template>
              </div>
              <div v-if="traceStages.length">
                <strong>执行轨迹</strong>
                <p v-for="stage in traceStages" :key="stage[0]">
                  {{ stage[0] }} · {{ stage[1] }}
                </p>
              </div>
              <div
                v-if="
                  sidebar.evaluation && Object.keys(sidebar.evaluation).length
                "
              >
                <strong>结果校验</strong>
                <p>
                  结构
                  {{ sidebar.evaluation.structuredValid ? "通过" : "待完善" }} ·
                  证据支持
                  {{
                    actions.formatPercent(
                      sidebar.evaluation.evidenceSupportRate,
                    )
                  }}
                  · Critic
                  {{
                    sidebar.evaluation.criticPassed ? "通过" : "达到轮次上限"
                  }}
                </p>
              </div>
            </details>
            <div class="analysis-follow-up">
              <label for="analysis-follow-up">继续追问</label>
              <button type="button" :disabled="sidebar.followUpLoading || sidebar.conversationHistoryLoading"
                @click="actions.startNewConversation">新建对话</button>
              <p v-if="sidebar.conversationHistoryLoading" role="status">正在恢复对话…</p>
              <p v-if="sidebar.conversationError" role="status">{{ sidebar.conversationError }}</p>
              <textarea
                id="analysis-follow-up"
                v-model="sidebar.followUp"
                maxlength="500"
                placeholder="基于视频继续追问…"
                @keydown.ctrl.enter.prevent="actions.submitFollowUp"
                @keydown.meta.enter.prevent="actions.submitFollowUp"
              ></textarea>
              <button
                type="button"
                :disabled="sidebar.followUpLoading || sidebar.conversationHistoryLoading || !sidebar.followUp.trim()"
                @click="actions.submitFollowUp"
              >
                {{ sidebar.followUpLoading ? "分析中…" : "发送追问" }}
              </button>
            </div>
            <div class="analysis-feedback">
              <span>这个结果有帮助吗？</span>
              <button
                type="button"
                :disabled="sidebar.feedbackLoading"
                :aria-pressed="sidebar.feedback === 1"
                @click="actions.sendFeedback(1)"
              >
                赞
              </button>
              <button
                type="button"
                :disabled="sidebar.feedbackLoading"
                :aria-pressed="sidebar.feedback === -1"
                @click="actions.sendFeedback(-1)"
              >
                踩
              </button>
            </div>
          </div>
        </template>
        <div v-else class="analysis-text-result">
          <p v-if="sidebar.loading">{{ loadingHeadline }}</p>
          <p v-if="sidebar.error" class="analysis-inline-error" role="alert">
            {{ sidebar.error }}
          </p>
          <div v-if="sidebar.content">
            <div class="analysis-answer__tools">
              <button type="button" @click="actions.copyResult">
                复制全文
              </button>
              <button type="button" @click="actions.downloadResult">
                导出文本
              </button>
            </div>
            <pre>{{ sidebar.content }}</pre>
          </div>
          <p v-else-if="!sidebar.loading && !sidebar.error">
            暂无可展示的转录文本。
          </p>
        </div>
      </aside>
    </div>
    <footer class="analysis-workspace__footer">
      <span>VideoMind / MEDIA RESEARCH</span>
      <span>播放位置 {{ formatMediaTime(currentTime) }}</span>
    </footer>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { apiRequest, captureAuthSession } from "./api.js";
import AcademyMark from "./design/AcademyMark.vue";
import AnalysisTimeline from "./AnalysisTimeline.vue";
import TemporalRecords from "./TemporalRecords.vue";
import { useClaimEvidence } from "./claimEvidence.js";
import {
  evidenceKey,
  formatMediaTime,
  validEvidenceTime,
} from "./analysisTimeline.js";
import "./analysis-workspace.css";

const props = defineProps({
  sidebar: { type: Object, required: true },
  media: { type: Object, default: null },
  actions: { type: Object, required: true },
  analysisModes: { type: Array, required: true },
  goalPresets: { type: Array, required: true },
  traceStages: { type: Array, required: true },
  renderedMarkdown: { type: String, default: "" },
  loadingHeadline: { type: String, default: "" },
  demoMode: { type: Boolean, default: false },
});

const root = ref(null);
const answerPanel = ref(null);
const videoPlayer = ref(null);
const currentTime = ref(0);
const duration = ref(0);
const selectedKey = ref("");
const transcriptText = ref("");
const transcriptError = ref("");
const transcriptHint = ref("暂无独立转录结果。分析仍可使用语音和画面证据。");
const transcriptLoading = ref(false);
let transcriptRequest = 0;
const temporalWindows = ref([]);
const temporalTotal = ref(0);
const temporalAvailable = ref(false);
const temporalLoading = ref(false);
const temporalError = ref("");
const temporalObservations = ref([]);
const observationTotal = ref(0);
const observationLoading = ref(false);
const observationError = ref("");
const { claims, citations, refresh: refreshCitations, dispose: disposeCitations } =
  useClaimEvidence(() => props.sidebar);
let temporalRequest = 0;
let observationRequest = 0;

const mediaStatusText = computed(
  () =>
    ({ COMPLETED: "就绪", PROCESSING: "处理中", FAILED: "失败" })[
      props.media?.status
    ] || "排队中",
);

function updateDuration() {
  duration.value = Number.isFinite(videoPlayer.value?.duration)
    ? videoPlayer.value.duration
    : 0;
}
function updateTime() {
  currentTime.value = videoPlayer.value?.currentTime || 0;
}

function seekVideo(seconds) {
  if (!Number.isFinite(seconds)) return;
  const player = videoPlayer.value;
  if (!player) {
    props.actions.showMessage(
      props.sidebar.playbackError
        ? "原视频加载失败，请先重新加载"
        : "原视频尚未就绪，暂时无法跳转",
      true,
    );
    return;
  }
  if (player.readyState === 0) {
    const sidebar = props.sidebar;
    const generation = sidebar.generation;
    const session = captureAuthSession();
    player.addEventListener("loadedmetadata", () => {
      if (session.isCurrent() && props.sidebar === sidebar &&
          sidebar.generation === generation && sidebar.visible &&
          videoPlayer.value === player) seekVideo(seconds);
    }, {
      once: true,
    });
    return;
  }
  const maxTime = Number.isFinite(player.duration)
    ? Math.max(0, player.duration - 0.1)
    : seconds;
  player.currentTime = Math.min(Math.max(0, seconds), maxTime);
  currentTime.value = player.currentTime;
  player.play().catch(() => {});
}

function selectEvidence(hit, key) {
  selectedKey.value = key;
  if (validEvidenceTime(hit)) seekVideo(Number(hit.startMs) / 1000);
}

function selectTemporalWindow(window) {
  selectedKey.value = `window:${window.segmentId || window.startMs}`;
  seekVideo(Number(window.startMs) / 1000);
}

function selectTemporalRecord(record) {
  if (record.id) {
    selectedKey.value = `observation:${record.id}`;
    seekVideo(Number(record.startMs) / 1000);
  } else selectTemporalWindow(record);
}

function selectCitation(citation) {
  selectedKey.value = `citation:${citation.id}`;
  seekVideo(Number(citation.timestampMs) / 1000);
}

function selectTimelineItem(marker) {
  if (marker.hit.cited) selectCitation(marker.hit);
  else if (marker.key.startsWith("observation:"))
    selectTemporalRecord(marker.hit);
  else if (marker.key.startsWith("window:")) selectTemporalWindow(marker.hit);
  else selectEvidence(marker.hit, marker.key);
}

let workspaceMounted = true;
function captureOperation() {
  const session = captureAuthSession();
  const generation = props.sidebar.generation;
  const mediaId = props.sidebar.mediaId;
  return () => workspaceMounted && session.isCurrent() && generation === props.sidebar.generation && mediaId === props.sidebar.mediaId;
}
onUnmounted(() => { workspaceMounted = false; disposeCitations(); });

async function loadTemporal(reset = true) {
  if (temporalLoading.value || props.demoMode || !props.sidebar.mediaId) return;
  const current = captureOperation();
  const request = ++temporalRequest;
  const mediaId = props.sidebar.mediaId;
  if (reset) {
    temporalWindows.value = [];
    temporalTotal.value = 0;
    temporalAvailable.value = false;
  }
  temporalLoading.value = true;
  temporalError.value = "";
  try {
    const params = new URLSearchParams({
      id: String(mediaId),
      limit: "200",
      offset: String(temporalWindows.value.length),
    });
    const response = await apiRequest(`/analysis/temporal-windows?${params}`);
    if (!response.ok)
      throw new Error((await response.text()) || "时间记录读取失败");
    const page = await response.json();
    if (!current() || request !== temporalRequest || mediaId !== props.sidebar.mediaId)
      return;
    temporalAvailable.value = Boolean(page.available);
    temporalTotal.value = Number(page.total) || 0;
    temporalWindows.value = [
      ...temporalWindows.value,
      ...(Array.isArray(page.items) ? page.items : []),
    ];
  } catch (error) {
    if (current() && request === temporalRequest)
      temporalError.value = error?.message || "时间记录读取失败";
  } finally {
    if (current() && request === temporalRequest) temporalLoading.value = false;
  }
}

async function loadObservations(reset = true) {
  if (observationLoading.value || props.demoMode || !props.sidebar.mediaId)
    return;
  const current = captureOperation();
  const request = ++observationRequest;
  const mediaId = props.sidebar.mediaId;
  if (reset) {
    temporalObservations.value = [];
    observationTotal.value = 0;
  }
  observationLoading.value = true;
  observationError.value = "";
  try {
    const params = new URLSearchParams({
      id: String(mediaId),
      limit: "200",
      offset: String(temporalObservations.value.length),
    });
    const response = await apiRequest(
      `/analysis/temporal-observations?${params}`,
    );
    if (!response.ok)
      throw new Error((await response.text()) || "逐条来源记录读取失败");
    const page = await response.json();
    if (!current() || request !== observationRequest || mediaId !== props.sidebar.mediaId)
      return;
    observationTotal.value = Number(page.total) || 0;
    temporalObservations.value = [
      ...temporalObservations.value,
      ...(Array.isArray(page.items) ? page.items : []),
    ];
  } catch (error) {
    if (current() && request === observationRequest)
      observationError.value = error?.message || "逐条来源记录读取失败";
  } finally {
    if (current() && request === observationRequest) observationLoading.value = false;
  }
}

function handleAnswerClick(event) {
  const link = event.target.closest('a[href^="#video-t="]');
  if (!link) return;
  event.preventDefault();
  const seconds = Number(link.getAttribute("href").split("=")[1]);
  seekVideo(seconds);
  const index = props.sidebar.evidenceResults.findIndex(
    (hit) =>
      validEvidenceTime(hit) &&
      Math.abs(Number(hit.startMs) / 1000 - seconds) < 0.5,
  );
  selectedKey.value =
    index >= 0 ? evidenceKey(props.sidebar.evidenceResults[index], index) : "";
}

async function refreshTranscript() {
  const current = captureOperation();
  const request = ++transcriptRequest;
  transcriptError.value = "";
  transcriptLoading.value = false;
  if (props.sidebar.type === "text") {
    transcriptText.value = props.sidebar.content || "";
    return;
  }
  if (props.demoMode) {
    transcriptText.value = props.media?.transcriptText || "";
    return;
  }
  transcriptLoading.value = true;
  try {
    const response = await apiRequest(
      `/analysis/transcription-status?id=${props.sidebar.mediaId}`,
    );
    if (!response.ok)
      throw new Error((await response.text()) || "转录状态读取失败");
    const status = await response.json();
    if (!current() || request !== transcriptRequest) return;
    transcriptText.value =
      ["COMPLETED", "FAILED"].includes(status.state) ? status.result || "" : "";
    transcriptHint.value =
      status.message || "暂无独立转录结果。分析仍可使用语音和画面证据。";
  } catch (error) {
    if (current() && request === transcriptRequest)
      transcriptError.value = error?.message || "转录状态读取失败";
  } finally {
    if (current() && request === transcriptRequest) transcriptLoading.value = false;
  }
}

async function scrollToLatestAnswer() {
  await nextTick();
  const container = answerPanel.value?.querySelector(".analysis-answer__body");
  const headings = container?.querySelectorAll("h2, h3") || [];
  const target = headings.length
    ? headings[headings.length - 1]
    : container?.lastElementChild;
  target?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function focus() {
  root.value?.focus({ preventScroll: true });
}
defineExpose({ focus, scrollToLatestAnswer });
watch(
  () => [props.sidebar.mediaId, props.sidebar.generation],
  () => {
    selectedKey.value = "";
    currentTime.value = 0;
    duration.value = 0;
    transcriptText.value = "";
    refreshTranscript();
    temporalRequest += 1;
    temporalLoading.value = false;
    temporalWindows.value = [];
    temporalTotal.value = 0;
    temporalAvailable.value = false;
    observationRequest += 1;
    observationLoading.value = false;
    temporalObservations.value = [];
    observationTotal.value = 0;
    observationError.value = "";
    loadTemporal();
    loadObservations();
  },
);
watch(
  () => props.sidebar.type === "text" && props.sidebar.content,
  (value) => {
    if (value) transcriptText.value = props.sidebar.content;
  },
);
watch(
  () => props.media?.status,
  (status, previous) => {
    if (status === "COMPLETED" && previous && previous !== "COMPLETED") {
      if (!props.sidebar.playbackUrl && !props.sidebar.playbackLoading)
        props.actions.retryPlayback();
      refreshTranscript();
      loadTemporal();
      loadObservations();
    }
  },
);
watch(
  () => [
    props.sidebar.mediaId,
    props.sidebar.generation,
    props.sidebar.goal,
    props.sidebar.analysisMode,
    props.sidebar.content,
    props.sidebar.loading,
    props.sidebar.visible,
    props.sidebar.type,
  ],
  () => { if (!props.demoMode) refreshCitations(); },
);
watch(
  () => props.sidebar.loading,
  (loading, wasLoading) => {
    if (props.sidebar.type === "ai" && wasLoading && !loading) {
      loadTemporal(true);
      loadObservations(true);
    }
  },
);
onMounted(() => {
  focus();
  refreshTranscript();
  loadTemporal();
  loadObservations();
  if (!props.demoMode) refreshCitations();
});
</script>
