<template>
  <div class="academy-timeline">
    <div class="academy-timeline__labels">
      <span>00:00</span>
      <span>06:00</span>
      <span>12:00</span>
      <span>18:00</span>
      <span>24:00</span>
    </div>
    <div
      class="academy-timeline__track"
      role="group"
      aria-label="Prototype media timeline"
    >
      <button
        v-for="(segment, index) in segments"
        :key="index"
        type="button"
        class="academy-timeline__segment"
        :class="[`is-${segment.kind}`, { 'is-current': modelValue === index }]"
        :aria-label="`${segment.label}, ${Math.round((index * 24) / segments.length)} minute`"
        :aria-pressed="modelValue === index"
        @click="$emit('update:modelValue', index)"
      >
        <span aria-hidden="true"></span>
      </button>
    </div>
    <div class="academy-timeline__legend">
      <span>
        <i class="is-asr"></i>
        ASR
      </span>
      <span>
        <i class="is-ocr"></i>
        OCR
      </span>
      <span>
        <i class="is-evidence"></i>
        EVIDENCE
      </span>
      <span>
        <i class="is-quiet"></i>
        QUIET
      </span>
    </div>
  </div>
</template>

<script setup>
defineProps({
  segments: { type: Array, required: true },
  modelValue: { type: Number, required: true },
})
defineEmits(['update:modelValue'])
</script>
