import { createApp } from 'vue'
import './style.css'

const isDesignLab = window.location.pathname.replace(/\/+$/, '') === '/design-lab'

if (isDesignLab) {
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', '#070A12')
  document.title = 'DOVideo · Pixel Future Academy · UI-0'
}

const { default: Root } = isDesignLab
  ? await import('./design/DesignLab.vue')
  : await import('./App.vue')

createApp(Root).mount('#app')

