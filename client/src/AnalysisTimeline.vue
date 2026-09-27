<template>
  <section class="analysis-timeline" aria-labelledby="timeline-title">
    <div class="analysis-section-head">
      <h3 id="timeline-title">时间轴</h3>
      <span>{{
        completeObservations
          ? "逐条来源记录 + 检索证据"
          : windows.length
            ? "已加载的 60 秒窗口 + 检索证据"
            : "证据检索范围"
      }}</span>
    </div>
    <div v-if="!duration" class="analysis-timeline__empty">
      载入视频时长后显示时间位置。
    </div>
    <div v-else-if="!markers.length" class="analysis-timeline__empty">
      暂无可用的时间化数据；检索证据后可显示命中时间段。
    </div>
    <div v-else class="analysis-timeline__lanes">
      <div v-for="lane in lanes" :key="lane.id" class="analysis-timeline__lane">
        <span class="analysis-timeline__label">{{ lane.label }}</span>
        <div class="analysis-timeline__track">
          <button
            v-for="marker in markers.filter((value) => value.lane === lane.id)"
            :key="`${lane.id}-${marker.key}`"
            type="button"
            class="analysis-timeline__marker"
            :class="[
              `is-${lane.id}`,
              {
                'is-selected': marker.key === selectedKey,
                'is-cited': marker.hit.cited,
              },
            ]"
            :style="{ left: `${marker.left}%`, width: `${marker.width}%` }"
            :title="`${lane.label} ${formatMediaTime(marker.seconds)} · ${marker.count > 1 ? `${marker.count} 条记录` : marker.hit.snippet || marker.hit.text || marker.hit.transcript || marker.hit.ocrTexts?.join(' · ') || '视频证据'}`"
            :aria-label="`${lane.label} ${formatMediaTime(marker.seconds)}，跳转播放`"
            @click="$emit('select', marker)"
          ></button>
          <span
            class="analysis-timeline__cursor"
            :style="{
              left: `${Math.min(100, Math.max(0, (currentTime / duration) * 100))}%`,
            }"
            aria-hidden="true"
          ></span>
        </div>
      </div>
      <div class="analysis-timeline__scale">
        <span>00:00</span>
        <span>{{ formatMediaTime(duration) }}</span>
      </div>
      <p
        v-if="windows.length && !completeObservations"
        class="analysis-timeline__note"
      >
        ASR / OCR 色块表示汇总窗口，不代表逐句或逐帧的持续时间。<span
          v-if="windows.length < windowTotal"
          >尚有未加载的时间窗口。</span
        >
      </p>
    </div>
  </section>
</template>

<script setup>
import { computed } from "vue";
import { formatMediaTime, timelineMarkers } from "./analysisTimeline.js";

const props = defineProps({
  hits: { type: Array, default: () => [] },
  windows: { type: Array, default: () => [] },
  windowTotal: { type: Number, default: 0 },
  observations: { type: Array, default: () => [] },
  observationTotal: { type: Number, default: 0 },
  citations: { type: Array, default: () => [] },
  duration: { type: Number, default: 0 },
  currentTime: { type: Number, default: 0 },
  selectedKey: { type: String, default: "" },
});
defineEmits(["select"]);
const lanes = [
  { id: "asr", label: "ASR" },
  { id: "ocr", label: "OCR" },
  { id: "evidence", label: "证据" },
];
const completeObservations = computed(
  () =>
    props.observations.length > 0 &&
    props.observations.length >= props.observationTotal,
);
const markers = computed(() =>
  timelineMarkers(
    props.hits,
    props.duration,
    props.windows,
    props.citations,
    props.observations,
    props.observationTotal,
  ),
);
</script>
