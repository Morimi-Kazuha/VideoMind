import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, nextTick } from 'vue'
import { useAnalysisWorkspace } from '../src/useAnalysisWorkspace.js'
import { setAuthToken } from '../src/api.js'
import App from '../src/App.vue'
const wrappers = []
const json = data => new Response(JSON.stringify({code:0, message:'', data}), {headers:{'content-type':'application/json'}})
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
beforeEach(() => {
  localStorage.clear(); localStorage.setItem('user', JSON.stringify({id:1,nickname:'test'})); setAuthToken('test-token')
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  vi.stubGlobal('fetch', vi.fn(async url => json(String(url).includes('/media/list') ? [] : {})))
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); document.body.innerHTML = '' })
function workspace() {
  let state
  const Harness = defineComponent({setup() {
    state = useAnalysisWorkspace({demoMode:false, taskStreams:{stopMedia:vi.fn(),stopAll:vi.fn(),start:vi.fn()},
      showMessage:vi.fn(),refreshMediaList:vi.fn(),findMediaItem:vi.fn()})
    return state
  }, template:'<div>{{ sidebar.playbackUrl }}</div>'})
  const wrapper = mount(Harness); wrappers.push(wrapper)
  Object.assign(state.sidebar.value, {visible:true,mediaId:1,type:'ai'})
  return {wrapper,state}
}
test('mounted workspace rejects late playback from previous media', async () => {
  const {state,wrapper} = workspace(); const old = deferred()
  fetch.mockImplementation(url => String(url).includes('id=1') ? old.promise : Promise.resolve(json('/second')))
  state.retryPlayback(); state.sidebar.value.mediaId = 2; state.sidebar.value.playbackLoading = false; state.retryPlayback()
  await flushPromises(); old.resolve(json('/first')); await flushPromises()
  expect(wrapper.text()).toBe('/second')
})
test('scope disposal invalidates pending playback writes', async () => {
  const {state,wrapper} = workspace(); const pending = deferred(); fetch.mockReturnValue(pending.promise)
  state.retryPlayback(); wrapper.unmount(); wrappers.pop(); pending.resolve(json('/too-late')); await flushPromises()
  expect(state.sidebar.value.playbackUrl).toBe(''); expect(state.sidebar.value.visible).toBe(false)
})
test('real App watchers toggle overlay and unmount clears it', async () => {
  const wrapper = mount(App, {attachTo:document.body}); wrappers.push(wrapper); await flushPromises()
  wrapper.vm.showAuthModal = true; await nextTick(); expect(document.body.classList.contains('overlay-open')).toBe(true)
  window.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'})); await nextTick()
  expect(document.body.classList.contains('overlay-open')).toBe(false)
  wrapper.vm.showAuthModal = true; await nextTick(); wrapper.unmount(); wrappers.pop()
  expect(document.body.classList.contains('overlay-open')).toBe(false)
})
test('real App audio download consumes binary and releases object URL', async () => {
  const wrapper = mount(App); wrappers.push(wrapper); await flushPromises()
  window.URL.createObjectURL = vi.fn(() => 'blob:audio'); window.URL.revokeObjectURL = vi.fn()
  const click = vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(() => {})
  fetch.mockResolvedValue(new Response(new Uint8Array([73,68,51]), {headers:{'content-type':'audio/mpeg'}}))
  await wrapper.vm.downloadAudio({id:1,filename:'clip.mp4'})
  expect(click).toHaveBeenCalledOnce(); expect(window.URL.revokeObjectURL).toHaveBeenCalledWith('blob:audio')
})
test('unmounted App does not trigger a late audio download', async () => {
  const wrapper = mount(App); wrappers.push(wrapper); await flushPromises(); window.URL.createObjectURL = vi.fn()
  const pending = deferred(); fetch.mockReturnValue(pending.promise)
  const action = wrapper.vm.downloadAudio({id:1,filename:'clip.mp4'}); wrapper.unmount(); wrappers.pop()
  pending.resolve(new Response('audio')); await action; expect(window.URL.createObjectURL).not.toHaveBeenCalled()
})
