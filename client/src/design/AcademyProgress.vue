<template>
  <div class="academy-progress">
    <div class="academy-progress__caption">
      <span>{{ label }}</span>
      <strong>{{ boundedValue }}%</strong>
    </div>
    <div
      class="academy-progress__track"
      role="progressbar"
      :aria-label="label"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-valuenow="boundedValue"
    >
      <span
        class="academy-progress__fill"
        :style="{ width: `${boundedValue}%` }"
      ></span>
    </div>
    <p v-if="detail" class="academy-progress__detail">{{ detail }}</p>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  value: { type: Number, required: true },
  detail: { type: String, default: '' },
})
const boundedValue = computed(() =>
  Math.min(100, Math.max(0, Math.round(props.value))),
)
</script>
