<template>
  <span class="academy-status" :class="`academy-status--${state}`">
    <span class="academy-status__light" aria-hidden="true"></span>
    <span class="academy-status__label">{{ label }}</span>
    <span class="academy-status__value">{{ value }}</span>
  </span>
</template>

<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: String, required: true },
  state: { type: String, default: 'idle' },
})
</script>
