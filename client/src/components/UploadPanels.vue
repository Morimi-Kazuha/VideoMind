<template>
<div class="split-container">

              <button type="button" class="skew-pane pane-local" @click="$emit('choose')">
                <div class="pane-content unskew">
                  <div class="magnet-icon">
                    <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
                  </div>
                  <span class="magnet-title">LOCAL FILE</span>
                  <span class="magnet-desc">{{ isDragOver ? '松手上传' : '点击 / 拖拽本地文件' }}</span>
                </div>
              </button>

              <div class="split-gap"></div>

              <div class="skew-pane pane-url">
                <div class="pane-content unskew">
                  <div class="magnet-icon">
                    <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1 4-10z"></path></svg>
                  </div>
                  <span class="magnet-title">WEB LINK</span>

                  <form class="url-input-box" @click.stop @submit.prevent="submit">
                    <input
                        v-model="url" :disabled="busy" aria-label="公开 HTTPS MP4 视频直链"
                        type="url"
                        placeholder="粘贴视频链接..."

                    />
                    <button class="url-go-btn" type="submit" :disabled="busy || !valid" aria-label="导入视频" :aria-busy="busy">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
                    </button>
                  </form>
                  <span v-if="status" class="magnet-desc web-link-status" :role="failed ? 'alert' : 'status'" aria-live="polite">{{ status }}</span>
                </div>
              </div>


</div>
</template>
<script setup>
import { computed, ref, onUnmounted } from 'vue'
import { apiRequest, captureAuthSession, hasAuthToken } from '../api.js'
defineProps({ isDragOver: Boolean })
const emit = defineEmits(['choose', 'imported'])
const url = ref(''), busy = ref(false), status = ref(''), failed = ref(false)
let disposed = false
const controller = new AbortController()
onUnmounted(() => { disposed = true; controller.abort() })
const valid = computed(() => {
  try {
    const u = new URL(url.value.trim())
    return u.protocol === 'https:' && !u.username && !u.password && !u.hash &&
      (!u.port || u.port === '443') && /\.mp4$/i.test(decodeURIComponent(u.pathname))
  } catch { return false }
})
const submit = async () => {
  if (!valid.value || busy.value) return
  if (!hasAuthToken()) { failed.value = true; status.value = '请先登录后导入'; return }
  const session = captureAuthSession()
  busy.value = true; failed.value = false; status.value = '正在下载并保存视频（最多 100 MiB / 45 秒）…'
  try {
    const response = await apiRequest(`/media/upload-url?${new URLSearchParams({ url: url.value.trim() })}`, { method: 'POST', signal: controller.signal })
    if (!response.ok) throw new Error(await response.text())
    await response.json()
    if (disposed || !session.isCurrent()) return
    status.value = '视频已导入'; url.value = ''; emit('imported')
  } catch (error) {
    if (!disposed && session.isCurrent()) { failed.value = true; status.value = error.message || '导入失败，请稍后重试' }
  } finally { busy.value = false }
}
</script>
<style scoped src="../dovideo.css"></style>

<style scoped>
.web-link-status { max-width: 90%; overflow-wrap: anywhere; text-align: center; margin-top: 6px; }
.url-go-btn:disabled { opacity: .35; cursor: not-allowed; }
</style>
