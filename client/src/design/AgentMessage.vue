<template>
  <article class="academy-message" :class="`academy-message--${sender}`">
    <header>
      <span>{{ sender === 'agent' ? 'DOVIDEO AGENT' : 'RESEARCHER' }}</span>
      <time>{{ time }}</time>
    </header>
    <div class="academy-message__content"><slot /></div>
  </article>
</template>

<script setup>
defineProps({
  sender: { type: String, required: true },
  time: { type: String, required: true },
})
</script>
