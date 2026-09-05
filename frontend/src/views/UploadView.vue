<!--
  上传视图：可一次选多份合同 → 逐份上传入队（后台 worker 顺序审查）。
  本页只负责"送进队列"，不轮询；每份成功后给「查看」入口，全部完成可去队列。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { uploadContract } from '../api'

// 查看/跳队列事件交给 App 切视图
const emit = defineEmits<{ open: [threadId: string]; goQueue: [] }>()

// picked=待上传文件列表；busy=正在上传；results=每份结果（成功带任务号/失败带原因）
const picked = ref<File[]>([])
const busy = ref(false)
const uploading = ref('') // 当前正在传的文件名（进度文案）
const results = ref<{ name: string; ok: boolean; tid?: string; error?: string }[]>([])
const hint = '支持 PDF / Word / 文本(md,txt)，可多选批量上传；审核在后台顺序进行。'

const allDone = computed(() => !busy.value && results.value.length > 0)

/** 选择文件：追加到列表（可多次选），清掉上次结果。 */
function onPick(e: Event) {
  const el = e.target as HTMLInputElement
  const files = Array.from(el.files ?? [])
  if (!files.length) return
  picked.value = picked.value.concat(files)
  results.value = []
  el.value = '' // 清空 input 值：同一文件再次选择也能触发 change
}

/** 移除某个待上传文件（点 ×）。 */
function dropFile(index: number) {
  picked.value.splice(index, 1)
}

/** 逐份上传：失败不中断其余文件，全部结束后统一展示结果。 */
async function uploadAll() {
  if (!picked.value.length || busy.value) return
  busy.value = true
  results.value = []
  for (const file of picked.value) {
    uploading.value = file.name
    try {
      const res = await uploadContract(file)
      results.value.push({ name: file.name, ok: true, tid: res.thread_id })
    } catch (err) {
      results.value.push({ name: file.name, ok: false, error: err instanceof Error ? err.message : '上传失败' })
    }
  }
  uploading.value = ''
  busy.value = false
  picked.value = []
}
</script>

<template>
  <section class="rise upload">
    <!-- 页头 -->
    <div class="head">
      <h2>上传合同</h2>
      <p class="muted">{{ hint }}</p>
    </div>

    <!-- 上传入口：整卡可点，已选文件后显示列表 -->
    <label class="drop card">
      <input type="file" accept=".pdf,.docx,.md,.txt" multiple @change="onPick" />
      <span class="drop-main">{{ picked.length ? `已选 ${picked.length} 份` : '点击选择合同文件（可多选）' }}</span>
      <span class="drop-sub mono-num">pdf / docx / md / txt · 文本型即可</span>
    </label>

    <!-- 待上传清单：可移除单项 -->
    <ul v-if="picked.length" class="files card">
      <li v-for="(f, i) in picked" :key="`${f.name}-${i}`">
        <span class="fname">{{ f.name }}</span>
        <span class="mono-num fsize">{{ (f.size / 1024).toFixed(1) }} KB</span>
        <button class="x" title="移除" :disabled="busy" @click="dropFile(i)">×</button>
      </li>
    </ul>

    <!-- 动作：无文件/上传中禁用；上传中显示当前进度 -->
    <div class="actions">
      <button class="btn btn-primary" :disabled="!picked.length || busy" @click="uploadAll">
        {{ busy ? `上传中：${uploading}` : picked.length ? `开始审查 ${picked.length} 份` : '开始审查' }}
      </button>
      <button v-if="picked.length && !busy" class="btn btn-ghost" @click="picked = []">清空</button>
    </div>

    <!-- 上传结果汇总：逐份成功/失败 + 查看入口 -->
    <div v-if="results.length" class="results card">
      <p class="r-title serif">上传结果</p>
      <ul>
        <li v-for="(r, i) in results" :key="i" class="row">
          <span class="fname">{{ r.name }}</span>
          <span v-if="r.ok" class="stamp stamp-ok">已入队</span>
          <span v-else class="stamp stamp-seal">失败</span>
          <button v-if="r.ok" class="btn btn-plain" @click="emit('open', r.tid!)">查看</button>
          <span v-else class="err-txt muted">{{ r.error }}</span>
        </li>
      </ul>
      <div v-if="allDone" class="r-actions">
        <button class="btn btn-ghost" @click="emit('goQueue')">去任务队列看进度</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.upload {
  max-width: 760px;
}

.head h2 {
  font-family: var(--serif);
  font-size: 26px;
  letter-spacing: 0.12em;
  margin: 0 0 4px;
  padding-left: 15px;
  position: relative;
}

.head h2::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0.2em;
  bottom: 0.2em;
  width: 4px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--seal), rgba(165, 49, 44, 0.3));
}

.head p {
  margin: 0 0 20px;
  font-size: 13.5px;
}

.drop {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 40px 20px 34px;
  border-style: dashed;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.drop::before {
  /* 上传入口 = 虚线朱环 + 「＋」：比纯文字更有"往这里放"的指向 */
  content: "＋";
  display: grid;
  place-items: center;
  width: 54px;
  height: 54px;
  margin-bottom: 6px;
  border-radius: 50%;
  border: 1.5px dashed var(--seal);
  color: var(--seal);
  font-size: 26px;
  line-height: 1;
  font-weight: 300;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  box-shadow: inset 0 0 0 4px rgba(165, 49, 44, 0.06);
}

.drop:hover {
  border-color: var(--seal);
  background: linear-gradient(180deg, #fdf9ee, #f5ecda);
  box-shadow:
    0 2px 0 rgba(165, 49, 44, 0.25),
    0 6px 18px rgba(90, 76, 45, 0.08);
}

.drop:hover::before {
  transform: rotate(12deg) scale(1.05);
  box-shadow:
    inset 0 0 0 4px rgba(165, 49, 44, 0.06),
    0 0 0 5px rgba(165, 49, 44, 0.08);
}

.drop input {
  display: none;
}

.drop-main {
  font-family: var(--serif);
  font-size: 17px;
  letter-spacing: 0.08em;
}

.drop-sub {
  font-size: 12.5px;
  color: var(--muted);
  font-family: var(--mono);
  letter-spacing: 0.06em;
}

.files {
  list-style: none;
  margin: 14px 0 0;
  padding: 6px 0;
}

.files li,
.results .row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 14px;
}

.files li + li,
.results li + li {
  border-top: 1px dashed var(--line);
}

.fname {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fsize {
  color: var(--muted);
  font-size: 12px;
}

.x {
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 18px;
  line-height: 1;
}

.x:hover:not(:disabled) {
  color: var(--seal);
}

.actions {
  margin: 18px 0;
  display: flex;
  gap: 10px;
}

.results {
  padding: 14px 16px;
}

.results ul {
  list-style: none;
  margin: 0;
  padding: 0;
}

.r-title {
  margin: 0 0 6px;
  font-size: 17px;
  letter-spacing: 0.1em;
}

.r-actions {
  margin-top: 12px;
}

.err-txt {
  font-size: 12.5px;
}
</style>
