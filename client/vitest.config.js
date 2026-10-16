import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
export default defineConfig({
  plugins: [vue()],
  define: { __VIDEOMIND_DEMO_ENABLED__: false },
  test: { environment: 'jsdom', include: ['tests/**/*.spec.js'], restoreMocks: true },
})
