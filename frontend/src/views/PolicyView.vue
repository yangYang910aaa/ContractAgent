<!--
  政策库起稿视图：上传或粘贴一份新政策 → 规范化草稿 + 重叠分级 + 冲突列表 + 配套清单。
  只起稿、不入库：本页不写政策库，产物落在草稿目录，入库仍由"批准入库"那一步单独做。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { createPolicyDraft, getPolicyDraft, getPolicyLibrary } from '../api'
import { missingMetaText } from '../labels'
import type { PolicyDraftDetail, PolicyDraftSummary, PolicyLibrary, PublishResult } from '../types'
import ConflictPane from '../components/policy/ConflictPane.vue'
import OverlapPane from '../components/policy/OverlapPane.vue'
import PublishCard from '../components/policy/PublishCard.vue'
import TextPane from '../components/policy/TextPane.vue'

// 两种输入方式：文件走解析器（含 pdf/docx），粘贴用于从别处抄来的条文
const mode = ref<'file' | 'paste'>('file')
const picked = ref<File | null>(null)
const pastedName = ref('')
const pastedText = ref('')
const busy = ref(false)
const error = ref('')
const summary = ref<PolicyDraftSummary | null>(null)
const detail = ref<PolicyDraftDetail | null>(null)
const library = ref<PolicyLibrary | null>(null) // 政策库现状（版本 + 份数），入库后刷新

const canSubmit = computed(() =>
  mode.value === 'file' ? Boolean(picked.value) : Boolean(pastedText.value.trim()),
)

/** 取政策库现状：纯读盘，用来给页面标"依据的是哪一版语料"。 */
async function loadLibrary() {
  try {
    library.value = await getPolicyLibrary()
  } catch {
    library.value = null // 拿不到就不显示这一行，不影响起稿与入库
  }
}

onMounted(loadLibrary)

/** 入库成功：回读草稿（拿到入库记录）并刷新政策库现状。 */
async function onApplied(_result: PublishResult) {
  if (!summary.value) return
  detail.value = await getPolicyDraft(summary.value.draft_id)
  await loadLibrary()
}

/** 选择文件：只留最新一份（起稿是逐份审的，堆一列反而要看错行）。 */
function onPick(e: Event) {
  const el = e.target as HTMLInputElement
  picked.value = el.files?.[0] ?? null
  el.value = '' // 清空 input 值：同一文件再选一次也能触发 change
}

/** 起稿：先拿摘要渲染概览，再取详情补全四块结果（详情要回读磁盘产物）。 */
async function runDraft() {
  if (busy.value || !canSubmit.value) return
  busy.value = true
  error.value = ''
  summary.value = null
  detail.value = null
  try {
    const created =
      mode.value === 'file'
        ? await createPolicyDraft({ file: picked.value! })
        : await createPolicyDraft({ text: pastedText.value, name: pastedName.value })
    summary.value = created
    detail.value = await getPolicyDraft(created.draft_id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '起稿失败'
  } finally {
    busy.value = false
  }
}

/** 清空输入与结果，接着起下一份草稿。 */
function reset() {
  picked.value = null
  pastedName.value = ''
  pastedText.value = ''
  summary.value = null
  detail.value = null
  error.value = ''
}
</script>

<template>
  <section class="rise policy">
    <div class="head">
      <h2>政策库起稿</h2>
      <p class="muted">
        上传或粘贴一份还没入库的政策正文，系统按现有体例重排出草稿、逐条比对现有政策库的重叠程度，
        并列出可核对的冲突与配套清单。<b>本页只起稿，不入库。</b>
      </p>
      <p v-if="library" class="lib mono-num">
        政策库现状：{{ library.version }} · {{ library.files }} 份政策 / {{ library.units }} 个检索单元
      </p>
    </div>

    <!-- 输入 -->
    <div class="card pad input">
      <div class="tabs">
        <button type="button" :class="{ on: mode === 'file' }" :disabled="busy" @click="mode = 'file'">
          上传文件
        </button>
        <button type="button" :class="{ on: mode === 'paste' }" :disabled="busy" @click="mode = 'paste'">
          粘贴文本
        </button>
      </div>
      <p class="tip muted">
        放进来的是<b>政策正文</b>（新政策稿、Word/PDF 政策文本、一段条文都行），不是本页生成过的草稿文件；
        传已入库的版本也没关系——它会用「编号重复 + 自重叠」直接告诉你这份已经在库里。
      </p>

      <label v-if="mode === 'file'" class="drop">
        <input type="file" accept=".md,.txt,.pdf,.docx" @change="onPick" />
        <span class="drop-main">{{ picked ? picked.name : '点击选择政策文件' }}</span>
        <span class="drop-sub mono-num">md / txt / pdf / docx · 图片型扫描件请先转成文本</span>
      </label>

      <div v-else class="paste">
        <input v-model="pastedName" class="name" type="text" placeholder="来源名（可留空，如 P-16_解除与善后.md）" />
        <textarea
          v-model="pastedText"
          class="text mono-num"
          rows="10"
          placeholder="# 政策标题&#10;文件编号：P-XX　　版本：V1.0　　生效日期：…&#10;归口部门：…&#10;适用范围：…&#10;## 第一条 …"
        ></textarea>
      </div>

      <div class="actions">
        <button class="btn btn-primary" type="button" :disabled="!canSubmit || busy" @click="runDraft">
          {{ busy ? '起稿中（逐条检索现有政策）…' : '开始起稿' }}
        </button>
        <button v-if="summary || error" class="btn btn-ghost" type="button" :disabled="busy" @click="reset">
          清空
        </button>
        <span class="hint muted">起稿只花检索的向量化调用，不改政策库</span>
      </div>

      <p v-if="error" class="err">{{ error }}</p>
    </div>

    <!-- 概览：摘要先到，四块结果随后补全 -->
    <div v-if="summary" class="card pad brief">
      <div class="b-line">
        <span class="b-ref mono-num">{{ summary.ref || '未编号' }}</span>
        <span class="b-title">{{ summary.title || summary.source }}</span>
      </div>
      <div class="b-facts">
        <span class="fact">条文 <b class="mono-num">{{ summary.articles }}</b></span>
        <span class="fact">高度重叠 <b class="mono-num">{{ summary.overlap.high }}</b></span>
        <span class="fact">中等重叠 <b class="mono-num">{{ summary.overlap.medium }}</b></span>
        <span class="fact">低重叠 <b class="mono-num">{{ summary.overlap.low }}</b></span>
        <span class="fact">冲突 <b class="mono-num">{{ summary.conflicts.length }}</b></span>
      </div>
      <p v-if="summary.missing.length" class="miss">元信息待补：{{ missingMetaText(summary.missing) }}</p>
      <p class="muted dir mono-num">草稿目录：{{ summary.draft_id }}</p>
    </div>

    <!-- 结果：左草稿、右重叠与冲突，配套清单整宽在下面 -->
    <div v-if="detail" class="result">
      <TextPane
        title="规范化草稿"
        note="按现有政策体例重排；缺失的元信息写「待填」"
        :text="detail.draft"
        grows
      />
      <div class="side">
        <OverlapPane :overlaps="detail.overlaps" />
        <ConflictPane :conflicts="detail.conflicts" />
      </div>
      <div class="full">
        <TextPane
          title="配套清单"
          note="范围卡体例骨架，待定项与样本计划由人补全"
          :text="detail.checklist"
        />
      </div>
      <div class="full">
        <PublishCard
          :draft-id="detail.draft_id"
          :suggested-file="detail.suggested_file"
          :draft-text="detail.draft"
          :missing="detail.parsed.missing"
          :applied="detail.applied"
          @applied="onApplied"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.policy {
  max-width: 1080px;
}

.head h2 {
  font-size: 21px;
  letter-spacing: 0.02em;
  margin: 0 0 4px;
  font-weight: 700;
}

.head p {
  margin: 0 0 18px;
  font-size: 13px;
  line-height: 1.7;
}

.head .lib {
  margin: -12px 0 16px;
  font-size: 12px;
  color: var(--muted);
}

.head b {
  color: var(--ink);
}

.tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.tabs button {
  border: 1.5px solid var(--line);
  background: #fff;
  border-radius: 8px;
  padding: 5px 16px;
  font-size: 13px;
  font-weight: 600;
  color: var(--ink-2);
  transition: border-color 0.14s ease, background 0.14s ease, color 0.14s ease;
}

.tabs button:hover:not(:disabled) {
  border-color: var(--pri);
}

.tabs button.on {
  border-color: var(--pri);
  background: var(--pri-soft);
  color: var(--pri);
}

.tip {
  margin: -4px 0 12px;
  font-size: 12.5px;
  line-height: 1.7;
}

.tip b {
  color: var(--ink);
}

.drop {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 34px 20px 30px;
  border: 1.5px dashed var(--line-strong);
  border-radius: 12px;
  background: #fff;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.drop:hover {
  border-color: var(--pri);
  background: #f8faff;
}

.drop input {
  display: none;
}

.drop-main {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.drop-sub {
  font-size: 12px;
  color: var(--muted);
}

.paste {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.name,
.text {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 9px 12px;
  font-size: 13px;
  color: var(--ink);
  background: #fff;
}

.name:focus,
.text:focus {
  outline: none;
  border-color: var(--pri);
  box-shadow: 0 0 0 3px var(--pri-soft);
}

.text {
  resize: vertical;
  line-height: 1.7;
}

.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}

.hint {
  font-size: 12px;
}

.err {
  margin: 10px 0 0;
}

.brief {
  margin-top: 16px;
}

.b-line {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.b-ref {
  font-size: 13px;
  font-weight: 700;
  color: var(--pri);
  background: var(--pri-soft);
  border-radius: 6px;
  padding: 2px 8px;
}

.b-title {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.b-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin-top: 10px;
}

.fact {
  font-size: 12.5px;
  color: var(--muted);
}

.fact b {
  font-size: 14px;
  color: var(--ink);
  margin-left: 3px;
}

.miss {
  margin: 10px 0 0;
  font-size: 12.5px;
  color: var(--seal-deep);
}

.dir {
  margin: 8px 0 0;
  font-size: 12px;
}

.result {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: 16px;
  margin-top: 16px;
  align-items: start;
}

.side {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.full {
  grid-column: 1 / -1;
}

/* 窄屏改单列：两栏挤在 700px 以下时条文会断成一片 */
@media (max-width: 900px) {
  .result {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
