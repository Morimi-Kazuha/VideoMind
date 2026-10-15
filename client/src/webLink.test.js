import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { ref, computed } from 'vue'
const source = await readFile(new URL('./components/UploadPanels.vue', import.meta.url), 'utf8')
const script = source.split('<script setup>')[1].split('</script>')[0].replace(/^import .*$/gm, '')
function setup(request, authenticated=true) {
  const events=[]
  let current=true,cleanup
  const create=new Function('ref','computed','onUnmounted','apiRequest','captureAuthSession','hasAuthToken','defineProps','defineEmits', `${script};return {url,busy,status,failed,valid,submit}`)
  const app=create(ref,computed,fn=>cleanup=fn,request,()=>({isCurrent:()=>current}),()=>authenticated,()=>{},()=>event=>events.push(event))
  return {...app,events,expire:()=>current=false,unmount:()=>cleanup()}
}
test('invalid and platform links cannot submit',async()=>{
  const app=setup(()=>assert.fail('must not request'))
  for(const value of ['', 'file:///a.mp4','http://example.com/a.mp4','https://www.bilibili.com/video/BV123','https://user:pass@example.com/a.mp4']) {
    app.url.value=value;assert.equal(app.valid.value,false);await app.submit()
  }
})
test('real request owns busy state, prevents duplicates, refreshes only on success',async()=>{
  let resolve,calls=0
  const app=setup((url,options)=>{calls++;assert.match(url,/upload-url/);assert.equal(options.method,'POST');return new Promise(done=>resolve=done)})
  app.url.value='https://example.com/a.mp4'
  const importing=app.submit();assert.equal(app.busy.value,true)
  await app.submit();assert.equal(calls,1);assert.deepEqual(app.events,[])
  resolve({ok:true,json:async()=>({id:1})});await importing
  assert.equal(app.busy.value,false);assert.deepEqual(app.events,['imported']);assert.equal(app.status.value,'视频已导入')
})
test('failed import shows Chinese server error and never refreshes',async()=>{
  const app=setup(async()=>({ok:false,text:async()=>'视频下载中断'}))
  app.url.value='https://example.com/a.mp4';await app.submit()
  assert.equal(app.status.value,'视频下载中断');assert.equal(app.failed.value,true);assert.deepEqual(app.events,[])
})
test('unauthenticated import and stale session never publish success',async()=>{
  const guest=setup(()=>assert.fail('guest request'),false)
  guest.url.value='https://example.com/a.mp4';await guest.submit();assert.match(guest.status.value,/登录/)
  let resolve
  const app=setup(()=>new Promise(done=>resolve=done));app.url.value='https://example.com/a.mp4'
  const pending=app.submit();app.expire();resolve({ok:true,json:async()=>({id:1})});await pending;assert.deepEqual(app.events,[])
})
test('unmount aborts download and suppresses refresh',async()=>{
  let signal,resolve
  const app=setup((url,options)=>{signal=options.signal;return new Promise(done=>resolve=done)})
  app.url.value='https://example.com/a.mp4';const pending=app.submit();app.unmount()
  assert.equal(signal.aborted,true);resolve({ok:true,json:async()=>({id:1})});await pending;assert.deepEqual(app.events,[])
})
