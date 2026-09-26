<template>
  <div class="academy-tabs" role="tablist" :aria-label="label">
    <button
      v-for="item in items"
      :key="item.value"
      type="button"
      role="tab"
      class="academy-tabs__tab"
      :class="{ 'is-active': modelValue === item.value }"
      :id="panelId ? `${panelId}-${item.value}` : undefined"
      :aria-controls="panelId || undefined"
      :aria-selected="modelValue === item.value"
      :tabindex="modelValue === item.value ? 0 : -1"
      @click="$emit('update:modelValue', item.value)"
      @keydown="onKeydown($event, item.value)"
    >
      {{ item.label }}
    </button>
  </div>
</template>

<script setup>
const props = defineProps({
  items: { type: Array, required: true },
  modelValue: { type: String, required: true },
  label: { type: String, required: true },
  panelId: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

function onKeydown(event, value) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const current = props.items.findIndex((item) => item.value === value)
  const next =
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? props.items.length - 1
        : (current +
            (event.key === 'ArrowRight' ? 1 : -1) +
            props.items.length) %
          props.items.length
  emit('update:modelValue', props.items[next].value)
  event.currentTarget.parentElement.children[next]?.focus()
}
</script>
