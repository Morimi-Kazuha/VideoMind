<template>
  <button
    class="academy-button"
    :class="`academy-button--${variant}`"
    :type="type"
  >
    <slot />
  </button>
</template>

<script setup>
defineProps({
  variant: { type: String, default: 'secondary' },
  type: { type: String, default: 'button' },
})
</script>
