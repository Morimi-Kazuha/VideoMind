<template>
  <section class="temporal-records" aria-labelledby="temporal-title">
    <div class="analysis-section-head">
      <h2 id="temporal-title">全片时间记录</h2>
      <span>60 秒上下文窗口</span>
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
    <p
      v-if="!loading && !error && !windows.length"
      class="analysis-transcript__empty"
    >
      {{
        available
          ? "暂无可用的时间化转录或画面文字。"
          : "媒体分析尚未生成时间窗口。"
      }}
    </p>
    <p v-if="windows.length" class="temporal-records__explain">
      文本按后端 60 秒窗口汇总，时间按钮定位到窗口起点；不表示每句话的精确起点。
    </p>
    <p
      v-if="windows.length && !records.length"
      class="analysis-transcript__empty"
    >
      当前已加载的窗口没有{{ tab === "asr" ? "语音转录" : "画面文字" }}记录。
    </p>
    <div v-if="windows.length" class="temporal-records__list">
      <button
        v-for="window in visibleRecords"
        :key="window.segmentId || window.startMs"
        type="button"
        class="temporal-records__item"
        :class="{
          'is-selected':
            selectedKey === `window:${window.segmentId || window.startMs}`,
        }"
        :aria-pressed="
          selectedKey === `window:${window.segmentId || window.startMs}`
        "
        @click="$emit('select', window)"
      >
        <strong>{{ formatMediaTime(window.startMs / 1000) }}</strong>
        <span>{{
          tab === "asr" ? window.transcript : window.ocrTexts.join(" · ")
        }}</span>
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
      v-if="windows.length < total && !loading"
      class="temporal-records__more"
      type="button"
      @click="$emit('more')"
    >
      加载更多时间窗口（{{ windows.length }} / {{ total }}）
    </button>
    <p
      v-if="loading && windows.length"
      class="analysis-local-loading"
      role="status"
    >
      正在载入更多时间窗口…
    </p>
  </section>
</template>

<script setup>
import { computed, ref } from "vue";
import { formatMediaTime } from "./analysisTimeline.js";

const props = defineProps({
  windows: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  available: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  error: { type: String, default: "" },
  selectedKey: { type: String, default: "" },
});
defineEmits(["select", "retry", "more"]);
const choices = [
  { id: "asr", label: "语音转录" },
  { id: "ocr", label: "画面文字" },
];
const tab = ref("asr");
const visibleCount = ref(24);
const records = computed(() =>
  props.windows.filter((window) =>
    tab.value === "asr"
      ? window.transcript?.trim()
      : window.ocrTexts?.some((text) => text?.trim()),
  ),
);
const visibleRecords = computed(() =>
  records.value.slice(0, visibleCount.value),
);
</script>
