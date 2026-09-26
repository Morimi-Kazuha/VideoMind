<template>
  <article class="academy-evidence">
    <div class="academy-evidence__top">
      <span>EVIDENCE // {{ index }}</span>
      <span class="academy-badge">{{ source }}</span>
    </div>
    <dl>
      <div>
        <dt>TIME</dt>
        <dd>{{ time }}</dd>
      </div>
      <div>
        <dt>FRAME</dt>
        <dd>{{ frame }}</dd>
      </div>
    </dl>
    <p>{{ excerpt }}</p>
    <button type="button" class="academy-text-button" @click="$emit('open')">
      OPEN FRAME
      <span aria-hidden="true">↗</span>
    </button>
  </article>
</template>

<script setup>
defineProps({
  index: { type: String, required: true },
  source: { type: String, required: true },
  time: { type: String, required: true },
  frame: { type: String, required: true },
  excerpt: { type: String, required: true },
})
defineEmits(['open'])
</script>
