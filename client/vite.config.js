import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '..', '')
  const backend = env.VITE_DEV_PROXY_TARGET || 'http://127.0.0.1:8000'
  return {
    envDir: '..',
    plugins: [vue()],
    server: {
      proxy: Object.fromEntries(
        ['/user', '/media', '/analysis', '/admin', '/health'].map(path => [
          path,
          { target: backend, changeOrigin: true }
        ])
      )
    }
  }
})

