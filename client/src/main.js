import { createApp } from 'vue'
import './style.css'

const isDesignLab = window.location.pathname.replace(/\/+$/, '') === '/design-lab'

if (isDesignLab) {
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', '#070A12')
  document.title = 'VideoMind · Pixel Future Academy · 设计实验室'
}

const { default: Root } = isDesignLab
  ? await import('./design/DesignLab.vue')
  : await import('./App.vue')

createApp(Root).mount('#app')

