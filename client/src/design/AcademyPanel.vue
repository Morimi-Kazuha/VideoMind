<template>
  <section class="academy-panel" :class="{ 'academy-panel--raised': raised }">
    <header v-if="label" class="academy-panel__header">
      <div class="academy-panel__heading">
        <span v-if="index" class="academy-panel__index">{{ index }}</span>
        <span>{{ label }}</span>
      </div>
      <slot name="action" />
    </header>
    <div class="academy-panel__body"><slot /></div>
  </section>
</template>

<script setup>
defineProps({
  label: { type: String, default: '' },
  index: { type: String, default: '' },
  raised: { type: Boolean, default: false },
})
</script>
