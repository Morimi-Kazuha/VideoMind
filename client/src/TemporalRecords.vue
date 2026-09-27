<template>
  <section class="temporal-records" aria-labelledby="temporal-title">
    <div class="analysis-section-head">
      <h2 id="temporal-title">全片时间记录</h2>
      <span>{{ usingObservations ? "逐条来源记录" : "60 秒上下文窗口" }}</span>
    </div>
    <div class="temporal-records__tabs" role="group" aria-label="时间记录类型">
      <button
        v-for="choice in choices"
        :key="choice.id"
        type="button"
        :aria-pressed="tab === choice.id"
        :class="{ 'is-active': tab === choice.id }"
        @click="
          tab = choice.id;
          visibleCount = 24;
        "
      >
        {{ choice.label }}
      </button>
    </div>
    <p
      v-if="loading && !windows.length"
      class="analysis-local-loading"
      role="status"
    >
      正在读取全片时间窗口…
    </p>
    <p v-if="error" class="analysis-inline-error" role="alert">
      {{ error }} <button type="button" @click="$emit('retry')">重试</button>
    </p>
    <p v-if="observationError" class="analysis-inline-error" role="alert">
      逐条记录读取失败，当前显示窗口视图。
      <button type="button" @click="$emit('retry-observations')">重试</button>
    </p>
    <p
      v-if="
        !loading &&
        !observationLoading &&
        !error &&
        !records.length &&
        !windows.length
      "
      class="analysis-transcript__empty"
    >
      {{
        available
          ? "暂无可用的时间化转录或画面文字。"
          : "媒体分析尚未生成时间窗口。"
      }}
    </p>
    <p
      v-if="windows.length || usingObservations"
      class="temporal-records__explain"
    >
      {{
        usingObservations
          ? "逐条记录保留来源时间；ASR 可定位原始时间段，OCR 定位提取帧的时间点。"
          : "文本按后端 60 秒窗口汇总，时间按钮定位到窗口起点；不表示每句话的精确起点。"
      }}
    </p>
    <p
      v-if="(windows.length || usingObservations) && !records.length"
      class="analysis-transcript__empty"
    >
      当前已加载的数据没有{{ tab === "asr" ? "语音转录" : "画面文字" }}记录。
    </p>
    <div
      v-if="windows.length || usingObservations"
      class="temporal-records__list"
    >
      <button
        v-for="record in visibleRecords"
        :key="record.id || record.segmentId || record.startMs"
        type="button"
        class="temporal-records__item"
        :class="{
          'is-selected': selectedKey === recordKey(record),
        }"
        :aria-pressed="selectedKey === recordKey(record)"
        @click="$emit('select', record)"
      >
        <strong>{{ formatMediaTime(record.startMs / 1000) }}</strong>
        <span>{{ recordText(record) }}</span>
        <small v-if="usingObservations && record.endMs != null">
          至 {{ formatMediaTime(record.endMs / 1000) }}
        </small>
        <small v-if="usingObservations && record.frameId">
          帧 {{ record.frameId.slice(0, 12) }}
        </small>
      </button>
    </div>
    <button
      v-if="visibleCount < records.length"
      class="temporal-records__more"
      type="button"
      @click="visibleCount += 24"
    >
      显示更多记录
    </button>
    <button
      v-if="!usingObservations && windows.length < total && !loading"
      class="temporal-records__more"
      type="button"
      @click="$emit('more')"
    >
      加载更多时间窗口（{{ windows.length }} / {{ total }}）
    </button>
    <button
      v-if="
        usingObservations &&
        observations.length < observationTotal &&
        !observationLoading
      "
      class="temporal-records__more"
      type="button"
      @click="$emit('more-observations')"
    >
      加载更多逐条记录（{{ observations.length }} / {{ observationTotal }}）
    </button>
    <p
      v-if="loading && windows.length"
      class="analysis-local-loading"
      role="status"
    >
      正在载入更多时间窗口…
    </p>
    <p v-if="observationLoading" class="analysis-local-loading" role="status">
      正在读取逐条来源记录…
    </p>
  </section>
</template>

<script setup>
import { computed, ref } from "vue";
import { formatMediaTime } from "./analysisTimeline.js";

const props = defineProps({
  windows: { type: Array, default: () => [] },
  observations: { type: Array, default: () => [] },
  observationTotal: { type: Number, default: 0 },
  observationLoading: { type: Boolean, default: false },
  observationError: { type: String, default: "" },
  total: { type: Number, default: 0 },
  available: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  error: { type: String, default: "" },
  selectedKey: { type: String, default: "" },
});
defineEmits([
  "select",
  "retry",
  "more",
  "retry-observations",
  "more-observations",
]);
const choices = [
  { id: "asr", label: "语音转录" },
  { id: "ocr", label: "画面文字" },
];
const tab = ref("asr");
const visibleCount = ref(24);
const usingObservations = computed(() => props.observations.length > 0);
const records = computed(() =>
  usingObservations.value
    ? props.observations.filter((record) =>
        tab.value === "asr"
          ? record.kind === "ASR" && record.text?.trim()
          : record.kind === "OCR",
      )
    : props.windows.filter((window) =>
        tab.value === "asr"
          ? window.transcript?.trim()
          : window.ocrTexts?.some((text) => text?.trim()),
      ),
);
const visibleRecords = computed(() =>
  records.value.slice(0, visibleCount.value),
);
function recordKey(record) {
  return usingObservations.value
    ? `observation:${record.id}`
    : `window:${record.segmentId || record.startMs}`;
}
function recordText(record) {
  if (usingObservations.value) return record.text || "画面帧（未识别文字）";
  return tab.value === "asr" ? record.transcript : record.ocrTexts.join(" · ");
}
</script>
