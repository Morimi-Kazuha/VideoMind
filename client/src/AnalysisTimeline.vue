<template>
  <section class="analysis-timeline" aria-labelledby="timeline-title">
    <div class="analysis-section-head">
      <h3 id="timeline-title">时间轴</h3>
      <span>证据检索范围</span>
    </div>
    <div v-if="!duration" class="analysis-timeline__empty">
      载入视频时长后显示时间位置。
    </div>
    <div v-else-if="!markers.length" class="analysis-timeline__empty">
      检索证据后，这里会标出真实时间段。
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
              { 'is-selected': marker.key === selectedKey },
            ]"
            :style="{ left: `${marker.left}%`, width: `${marker.width}%` }"
            :title="`${lane.label} ${formatMediaTime(marker.seconds)} · ${marker.hit.snippet || '视频证据'}`"
            :aria-label="`${lane.label} ${formatMediaTime(marker.seconds)}，跳转播放`"
            @click="$emit('select', marker.hit, marker.key)"
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
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { formatMediaTime, timelineMarkers } from './analysisTimeline.js'

const props = defineProps({
  hits: { type: Array, default: () => [] },
  duration: { type: Number, default: 0 },
  currentTime: { type: Number, default: 0 },
  selectedKey: { type: String, default: '' },
})
defineEmits(['select'])
const lanes = [
  { id: 'asr', label: 'ASR' },
  { id: 'ocr', label: 'OCR' },
  { id: 'evidence', label: '证据' },
]
const markers = computed(() => timelineMarkers(props.hits, props.duration))
</script>
