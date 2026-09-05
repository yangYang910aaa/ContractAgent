<!--
  任务详情视图：轮询展示任务全生命周期。
  审查中 → 待审批（HITL：放行/打回/编辑重审）→ 报告/失败。
  轮询策略见 tick()：停在闸口等人工时暂停轮询，操作后恢复轮询等结果。
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { approve, editFields, getTask, reject } from '../api'
import type { TaskDetail, TaskStatus } from '../types'

const props = defineProps<{ threadId: string }>()
const emit = defineEmits<{ back: [] }>()

// detail=当前任务快照（轮询更新）；error=加载失败；acting=审批请求进行中
const detail = ref<TaskDetail | null>(null)
const error = ref('')
const acting = ref(false)
const actionError = ref('') // 审批/编辑的表单级错误（如打回缺原因、JSON 非法）
const note = ref('') // 审批意见（打回必填）
const showEdit = ref(false) // 是否展开「编辑字段重审」面板
const patchText = ref('{\n  "warranty_months": 24\n}') // 字段补丁 JSON（编辑重审用）
let timer: number | undefined

// 状态/评级/等级 → 中文与印章样式（含义见 style.css 的 .stamp-* 族）
const statusText: Record<TaskStatus, string> = {
  pending: '排队中',
  processing: '审查中',
  gate: '待人工审批',
  done: '已完成',
  error: '失败',
}

const gradeText: Record<string, string> = {
  pass: '通过',
  conditional_pass: '有条件通过',
  fail: '不通过',
}

const gradeClass: Record<string, string> = {
  pass: 'stamp-ok',
  conditional_pass: 'stamp-warn',
  fail: 'stamp-seal',
}

const severityText: Record<string, string> = { high: '高风险', medium: '中风险', low: '低风险' }
const severityClass: Record<string, string> = { high: 'stamp-seal', medium: 'stamp-warn', low: 'stamp-mute' }

const fieldLabels: Record<string, string> = {
  contract_kind: '品类',
  buyer: '甲方（采购方）',
  supplier: '乙方（供应商）',
  signature_date: '签署日期',
  effective_date: '生效日期',
  expiry_date: '到期日',
  total_amount: '合同总额（元）',
  currency: '币种',
  penalty_rate: '违约金日率（%）',
  liability_cap: '责任上限（%）',
  warranty_months: '质保期（月）',
  termination_notice_days: '解约通知（天）',
  confidentiality_months: '保密期（月）',
  ip_ownership: 'IP 权属',
  governing_law: '适用法律',
}

// 审查中 = pending/processing：显示进行中动画，不渲染闸口/报告
const extracting = computed(() => detail.value && ['pending', 'processing'].includes(detail.value.status))
const ext = computed(() => detail.value?.report?.extracted ?? null)

/** 取抽取字段值（null-safe；模板里频繁判空，抽成函数避免内联表达式类型坑）。 */
function extVal(key: string): unknown {
  return ext.value?.[key]
}

/** 抽取字段展示文本：付款期次走 paymentText 拼接，其余直接转字符串。 */
function extText(key: string): string {
  if (key === 'payment_schedule') return paymentText(extVal(key))
  const v = extVal(key)
  return v == null ? '' : String(v)
}

async function load() {
  try {
    const d = await getTask(props.threadId)
    detail.value = d
    error.value = ''
  } catch (err) {
    // 加载失败保留旧快照并提示，等待下个轮询周期自动重试
    error.value = err instanceof Error ? err.message : '加载失败'
  }
}

/** 轮询一跳：非闸口状态继续刷；闸口且无进行中审批 → 暂停等人工操作。 */
function tick() {
  if (!detail.value) return
  const st = detail.value.status
  // 停在闸口且没有正在提交的审批 → 停下轮询，等人操作
  if (st === 'gate' && !acting.value) return
  load()
}

/** 审批动作统一执行器：置 acting 锁 → 调后端 → 成功后清空意见输入。 */
async function runAction(fn: () => Promise<TaskDetail>) {
  acting.value = true
  actionError.value = ''
  try {
    detail.value = await fn()
    note.value = ''
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : '审批失败'
  } finally {
    acting.value = false
  }
}

function doApprove() {
  /** 放行：意见可为空，动作本身即留痕。 */
  runAction(() => approve(props.threadId, note.value))
}

function doReject() {
  // 这种情况是：打回没填原因 → 前端拦截，避免无痕打回
  if (!note.value.trim()) {
    actionError.value = '打回请填写原因'
    return
  }
  runAction(() => reject(props.threadId, note.value))
}

function doEdit() {
  // 编辑重审：先本地校验 JSON，非法直接提示不请求后端
  let patches: Record<string, unknown>
  try {
    patches = JSON.parse(patchText.value)
  } catch {
    actionError.value = '补丁不是合法 JSON'
    return
  }
  runAction(() => editFields(props.threadId, patches, note.value || '修改字段后重审'))
}

function resetFor() {
  /** 切换任务号时重置本地状态再拉新任务（watch props.threadId 触发）。 */
  detail.value = null
  error.value = ''
  acting.value = false
  note.value = ''
  showEdit.value = false
  load()
}

watch(() => props.threadId, resetFor)

onMounted(() => {
  load()
  timer = window.setInterval(tick, 1600)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})

function paymentText(raw: unknown): string {
  /** 付款期次数组 → "名称 金额（比例%）；…" 单行展示。 */
  if (!Array.isArray(raw)) return ''
  return raw
    .map((t) => {
      const x = t as Record<string, unknown>
      return `${String(x.name ?? '')} ${x.amount ?? ''}（${x.percent ?? ''}%）`
    })
    .join('；')
}

// 阶段进度条：与后端状态一一映射（pending→排队、processing→抽取审查、
// gate→人工审批、done/error→报告）。error 时末段标红提示。
const stageNames = ['排队', '抽取审查', '人工审批', '报告']
const stageIndex = computed(() => {
  const st = detail.value?.status
  if (st === 'pending') return 0
  if (st === 'processing') return 1
  if (st === 'gate') return 2
  return 3
})
const stageFailed = computed(() => detail.value?.status === 'error')

/** 导出报告 JSON：前端侧生成下载（报告已全量在 detail.report 里）。 */
function downloadReport() {
  const report = detail.value?.report
  if (!report) return
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${detail.value?.source || 'report'}.report.json`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <section class="rise task">
    <div class="bar">
      <button class="btn btn-ghost" @click="emit('back')">← 返回</button>
      <span v-if="detail" class="mono-num tid">{{ detail.thread_id }}</span>
    </div>

    <!-- 阶段进度条：当前步高亮；已完成步打勾点；error 末段标红 -->
    <div v-if="detail" class="steps">
      <div v-for="(s, i) in stageNames" :key="s" class="step"
           :class="{ on: i === stageIndex, done: i < stageIndex, fail: stageFailed && i === stageNames.length - 1 }">
        <span class="dot"></span>
        <span>{{ s }}</span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <!-- 无数据/加载中 -->
    <div v-else-if="!detail" class="card pad-center muted">加载中…</div>

    <!-- 审查中 -->
    <div v-else-if="extracting" class="card pad-center">
      <p class="serif big pulse">{{ statusText[detail.status] }}</p>
      <p class="muted">正在抽取字段 → 规则审查 → 政策比对，约需 30~60 秒</p>
    </div>

    <!-- 待审批闸口 -->
    <div v-else-if="detail.status === 'gate' && detail.gate_payload" class="gate">
      <div class="card pad">
        <div class="gate-head">
          <h3>高风险，需人工审批</h3>
          <span class="stamp stamp-seal">{{ gradeText[detail.grade ?? 'fail'] ?? '不通过' }}</span>
        </div>
        <div v-for="(r, i) in detail.gate_payload.high_risks" :key="i" class="risk card">
          <div class="risk-top">
            <span class="stamp stamp-seal">{{ severityText.high }}</span>
            <span class="risk-type serif">{{ r.risk_type }}</span>
            <span v-if="r.policy_ref" class="mono-num ref">{{ r.policy_ref }}</span>
          </div>
          <p v-if="r.clause_ref" class="clause muted">条款：{{ r.clause_ref }}</p>
          <p v-if="r.evidence" class="quote">「{{ r.evidence }}」</p>
          <p v-if="r.suggestion" class="suggest">{{ r.suggestion }}</p>
        </div>

        <div class="approval">
          <textarea v-model="note" rows="2" placeholder="审批意见（打回必填原因，留痕可追溯）"></textarea>
          <p v-if="actionError" class="err">{{ actionError }}</p>
          <div class="btns">
            <button class="btn btn-primary" :disabled="acting" @click="doApprove">放行</button>
            <button class="btn btn-ghost" :disabled="acting" @click="doReject">打回</button>
            <button class="btn btn-plain" @click="showEdit = !showEdit">{{ showEdit ? '收起' : '编辑字段重审' }}</button>
          </div>
          <div v-if="showEdit" class="edit-panel">
            <label class="muted">字段补丁（JSON，键=ContractModel 字段名）</label>
            <textarea v-model="patchText" rows="4" class="mono-num"></textarea>
            <button class="btn btn-ghost" :disabled="acting" @click="doEdit">提交并重审</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 完成：报告 -->
    <div v-else-if="detail.status === 'done' && detail.report" class="report">
      <!-- 报告头：文件名 + 评级章 -->
      <div class="card pad head">
        <div>
          <p class="file serif">{{ detail.source }}</p>
          <p class="muted">评级</p>
        </div>
        <span class="stamp" :class="gradeClass[detail.report.grade ?? ''] ?? 'stamp-mute'">
          {{ gradeText[detail.report.grade ?? ''] ?? detail.report.grade ?? '—' }}
        </span>
      </div>

      <!-- 报告速览：评级/风险数/政策数/模式 + 导出 -->
      <div class="sum card">
        <div class="sum-item">
          <span class="muted">风险</span>
          <b class="mono-num">{{ detail.report.risks?.length ?? 0 }}</b>
        </div>
        <div class="sum-item">
          <span class="muted">政策引用</span>
          <b class="mono-num">{{ detail.report.policy_hits?.length ?? 0 }}</b>
        </div>
        <div class="sum-item">
          <span class="muted">审查模式</span>
          <b class="mono-num">{{ detail.report.review_mode ?? 'single' }}</b>
        </div>
        <button class="btn btn-ghost" @click="downloadReport">导出 JSON</button>
      </div>

      <!-- 审批留痕：done 报告里回显最近一次审批动作与意见 -->
      <div v-if="detail.report.approval" class="card pad appr">
        <span class="muted">审批记录：</span>
        <b>{{ detail.report.approval.action === 'approved' ? '放行' : detail.report.approval.action === 'rejected' ? '打回' : '编辑重审' }}</b>
        <span v-if="detail.report.approval.reviewer_note" class="note">「{{ detail.report.approval.reviewer_note }}」</span>
      </div>

      <!-- 风险清单：空=自动放行提示，非空逐条展示 -->
      <template v-if="detail.report.risks?.length">
        <h4>风险清单</h4>
        <div v-for="(r, i) in detail.report.risks" :key="i" class="risk card">
          <div class="risk-top">
            <span class="stamp" :class="severityClass[r.severity]">{{ severityText[r.severity] }}</span>
            <span class="risk-type serif">{{ r.risk_type }}</span>
            <span v-if="r.policy_ref" class="mono-num ref">{{ r.policy_ref }}</span>
          </div>
          <p v-if="r.clause_ref" class="clause muted">条款：{{ r.clause_ref }}</p>
          <p v-if="r.evidence" class="quote">「{{ r.evidence }}」</p>
          <p v-if="r.suggestion" class="suggest">{{ r.suggestion }}</p>
        </div>
      </template>
      <template v-else>
        <p class="none ok-text serif">未发现风险 · 自动放行</p>
      </template>

      <!-- 政策引用：policy_ref + 相似度 + 制度原文片段 -->
      <template v-if="detail.report.policy_hits?.length">
        <h4>政策引用</h4>
        <div v-for="(h, i) in detail.report.policy_hits" :key="i" class="card pad hit">
          <span class="mono-num ref">{{ h.policy_ref }}</span>
          <span v-if="h.score != null" class="muted mono-num">score {{ Number(h.score).toFixed(3) }}</span>
          <p class="snip">{{ h.snippet }}</p>
        </div>
      </template>

      <!-- 抽取字段：仅展示有值的字段（null 不占行），避免长列表空行噪音 -->
      <template v-if="detail.report.extracted">
        <h4>抽取字段</h4>
        <div class="card grid">
          <template v-for="(label, key) in fieldLabels" :key="key">
            <div v-if="extVal(key) != null" class="kv">
              <dt>{{ label }}</dt>
              <dd class="mono-num">{{ extText(key) }}</dd>
            </div>
          </template>
          <div v-if="paymentText(extVal('payment_schedule'))" class="kv">
            <dt>付款期次</dt>
            <dd class="mono-num">{{ paymentText(extVal('payment_schedule')) }}</dd>
          </div>
        </div>
      </template>
    </div>

    <!-- 失败 -->
    <div v-else-if="detail.status === 'error'" class="card pad err-box">
      <h3>审查失败</h3>
      <p>{{ detail.error || detail.report?.error || '未知错误' }}</p>
    </div>
  </section>
</template>

<style scoped>
.task {
  max-width: 860px;
}

.bar {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 16px;
}

.tid {
  color: var(--muted);
  font-size: 12.5px;
}

.err {
  color: var(--seal);
}

.pad {
  padding: 18px 22px;
}

.pad-center {
  padding: 48px 22px;
  text-align: center;
}

.big {
  font-size: 22px;
  letter-spacing: 0.2em;
  color: var(--seal);
}

.gate-head,
.risk-top {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.gate-head h3,
.report h4 {
  font-family: var(--serif);
  margin: 0;
}

.gate-head {
  justify-content: space-between;
  margin-bottom: 14px;
}

.risk {
  padding: 12px 16px;
  margin: 10px 0;
}

.risk-type {
  font-weight: 700;
}

.ref {
  font-size: 12.5px;
  color: var(--seal);
  border: 1px solid var(--seal-soft);
  background: var(--seal-soft);
  border-radius: 4px;
  padding: 1px 7px;
}

.quote {
  border-left: 3px solid var(--line-strong);
  padding-left: 10px;
  margin: 6px 0;
  color: var(--ink-2);
}

.clause,
.suggest {
  margin: 4px 0;
}

.approval {
  margin-top: 16px;
}

.btns {
  display: flex;
  gap: 10px;
  margin-top: 10px;
  align-items: center;
}

.edit-panel {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 460px;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.head .file {
  font-size: 18px;
  margin: 0 0 2px;
}

.head p {
  margin: 0;
}

.appr {
  margin-top: 12px;
  display: flex;
  gap: 8px;
  align-items: baseline;
}

.appr .note {
  color: var(--ink-2);
}

.report h4 {
  margin: 22px 0 6px;
  letter-spacing: 0.1em;
}

.none {
  text-align: center;
  padding: 26px 0;
}

.ok-text {
  color: var(--ok);
  letter-spacing: 0.1em;
}

.hit {
  margin: 8px 0;
}

.hit .ref {
  margin-right: 10px;
}

.snip {
  margin: 6px 0 0;
  color: var(--ink-2);
  font-size: 13.5px;
  white-space: pre-line;
}

.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
}

.kv {
  padding: 9px 16px;
  border-bottom: 1px dashed var(--line);
}

.kv:nth-last-child(-n + 2) {
  border-bottom: 0;
}

.kv dt {
  color: var(--muted);
  font-size: 12.5px;
}

.kv dd {
  margin: 2px 0 0;
  word-break: break-all;
}

.err-box h3 {
  margin-top: 0;
  color: var(--seal);
}

/* 阶段进度条：点 + 连线，当前步印章红、已完成步实心、失败末段标红 */
.steps {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 4px 0 18px;
}

.step {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 13px;
  letter-spacing: 0.04em;
}

.step + .step::before {
  content: '';
  width: 26px;
  height: 1px;
  background: var(--line-strong);
  margin-right: 4px;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid var(--line-strong);
  background: var(--card);
}

.step.on {
  color: var(--seal);
  font-weight: 700;
}

.step.on .dot {
  border-color: var(--seal);
  background: var(--seal);
  box-shadow: 0 0 0 3px var(--seal-soft);
  animation: pulse 1.6s ease-in-out infinite;
}

.step.done .dot {
  border-color: var(--ok);
  background: var(--ok);
}

.step.fail {
  color: var(--seal);
}

.step.fail .dot {
  border-color: var(--seal);
  background: var(--seal-soft);
}

/* 报告速览条 */
.sum {
  display: flex;
  align-items: center;
  gap: 26px;
  margin-top: 12px;
  padding: 12px 22px;
}

.sum-item {
  display: flex;
  flex-direction: column;
}

.sum-item span {
  font-size: 12px;
}

.sum-item b {
  font-size: 20px;
  line-height: 1.2;
}

.sum .btn {
  margin-left: auto;
}
</style>
