<!--
  任务详情视图：轮询展示任务全生命周期。
  审查中 → 待审批（HITL：放行/打回/编辑重审）→ 报告/失败。
  轮询策略见 tick()：停在闸口等人工时暂停轮询，操作后恢复轮询等结果。
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { approve, editFields, getTask, reject } from '../api'
import SourceDrawer from '../components/SourceDrawer.vue'
import { kindLabel, policyReflow, prettyField, riskLabel } from '../labels'
import type { ReviewSection, SourceAnchor, TaskDetail, TaskStatus } from '../types'

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
// U2 原文抽屉：showSource=开关；anchor=定位指令（风险项 clause_ref 点进来）
const showSource = ref(false)
const sourceAnchor = ref<SourceAnchor | null>(null)
let timer: number | undefined

// 演示/验收直达：?source=1 打开原文抽屉（配合 App 的 ?view=task&thread= 使用）
if (new URLSearchParams(location.search).get('source') === '1') {
  showSource.value = true
}

/** 给原文抽屉的风险高亮：闸口看待审 high，报告看最终 risks（无 severity 按 high）。 */
const drawerRisks = computed(() => {
  const d = detail.value
  if (!d) return []
  const src =
    d.status === 'gate'
      ? (d.gate_payload?.high_risks ?? [])
      : d.status === 'done'
        ? (d.report?.risks ?? [])
        : []
  return src.map((r) => ({
    risk_type: r.risk_type,
    label: r.label ?? null,
    severity: (r as { severity?: string }).severity ?? 'high',
    clause_ref: r.clause_ref ?? '',
    evidence: r.evidence ?? '',
    // 原文摘录要一并带上：说明句（"预付款比例 70%"）在正文里搜不到，
    // 少了它抽屉里就只能整块标色、标不到那一句
    evidence_quote: (r as { evidence_quote?: string | null }).evidence_quote ?? '',
  }))
})

/** 右栏"原文核对"清单：把风险项的 clause_ref 去重聚合，取最高 severity 并计数，
 *  同时留一句摘录——条款号来自模型自述，可能对应不到条款块（实测有"三、其他 3"
 *  这类正文里不存在的号），点定位时靠摘录兜底才找得到位置。 */
const hitClauses = computed(() => {
  const seen = new Map<string, { clause: string; sev: string; count: number; quote: string }>()
  for (const r of drawerRisks.value) {
    const clause = (r.clause_ref ?? '').trim()
    if (!clause) continue
    const sev = r.severity === 'medium' ? 'medium' : 'high'
    const quote = (r.evidence_quote || r.evidence || '').trim()
    const cur = seen.get(clause)
    if (cur) {
      cur.count += 1
      if (cur.sev === 'medium' && sev === 'high') cur.sev = sev
      if (!cur.quote) cur.quote = quote
    } else {
      seen.set(clause, { clause, sev, count: 1, quote })
    }
  }
  return [...seen.values()]
})

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

// 审查模式（review_mode 机器码 → 中文；导出 JSON 仍是机器码，属预期）
const reviewModeText: Record<string, string> = {
  single: '单审',
  double: '主审 + 盲审复核',
  parallel: '多智能体并行',
}

// 评级大圆章（.ring 系列，见全局 style.css）：双层朱文/石绿印
const ringClass: Record<string, string> = {
  pass: 'ring-ok',
  conditional_pass: 'ring-warn',
  fail: 'ring-seal',
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
/** 审查中提示的预计时长：双审要多跑一轮独立复核；扫描件/图片多一步本地 OCR
 *  （实测 3~17 秒/页），都比单审慢——原来固定写"约需 30~60 秒"，双审扫描件会像卡住。 */
const etaText = computed(() => {
  const doubleReview = detail.value?.review_mode === 'double'
  return doubleReview
    ? '双审（主审 + 独立复核）约需 1~2 分钟；扫描件/图片件还要加本地 OCR，更久'
    : '单审约需 30~60 秒；扫描件/图片件要加本地 OCR，约 1~2 分钟起'
})
const ext = computed(() => detail.value?.report?.extracted ?? null)
// 报告风险：疑似空白模板单拎为顶部"结论条"，不进风险清单卡片与计数
const reportRisks = computed(() => detail.value?.report?.risks ?? [])
const templateNotice = computed(
  () => reportRisks.value.find((r) => r.risk_type === 'blank_template_suspected') ?? null,
)
const listRisks = computed(() => reportRisks.value.filter((r) => r.risk_type !== 'blank_template_suspected'))
// 双审复核段：review_mode=double 的报告才有（merge_review 产出；单审为 null）
const review = computed(() => detail.value?.report?.review ?? null)
const reviewError = computed(() => review.value?.error ?? '')
// 闸口阶段报告还没生成，复核结论随 gate 载荷带出（否则审批人放行前看不到盲审结果）
const gateReview = computed(() => (detail.value?.gate_payload as { review?: ReviewSection | null } | null)?.review ?? null)

// 复核发现的处理结果 → 徽标文案/样式（与后端 outcome 对齐）
const outcomeText: Record<string, string> = {
  agreed: '与主审一致',
  added: '复核新增',
  upgraded: '取高升级',
  // "仅提示"太含糊（看不出是"提示1"还是别的）→ 直接写清处理结果：
  // 复核发现了，但没过确定性校验/类型不在可并入范围，所以只记录、不并入风险清单
  noted: '仅记录不并入',
}
const outcomeClass: Record<string, string> = {
  agreed: 'rv-agree',
  added: 'rv-added',
  upgraded: 'rv-upgrade',
  noted: 'rv-note',
}

/** 复核统计挑出非零项，右栏/卡片顶部的 chips 用。 */
const reviewChips = computed(() => {
  return chipsOf(review.value?.stats)
})

/** 统计 → chips（报告段与闸口段共用）。 */
function chipsOf(stats: Record<string, number> | undefined | null) {
  const s = stats
  if (!s) return []
  return [
    { key: 'added', label: '复核新增', count: s.added },
    { key: 'upgraded', label: '取高升级', count: s.upgraded },
    { key: 'agreed', label: '与主审一致', count: s.agreed },
    { key: 'noted', label: '仅记录不并入', count: s.noted },
  ].filter((c) => c.count > 0)
}
const gateReviewChips = computed(() => chipsOf(gateReview.value?.stats))

/** 政策行解析：已知标签行（文件编号/版本/生效日期/归口部门/适用范围/第X条）
 * 拆出标签与内容，标签用强调色、内容保持正文色——关键信息一眼可分。 */
function policyRowParts(line: string): { lbl: string | null; val: string } {
  const m = line.match(/^(文件编号|版本|生效日期|归口部门|适用范围|第[一二三四五六七八九十百\d]+条)[：:](.*)$/)
  return m ? { lbl: `${m[1]}：`, val: m[2] } : { lbl: null, val: line }
}

/** 政策行样式归类：首行=细则标题；元信息/归口适用范围/条文头各有色调。 */
function policyRowClass(index: number, line: string): string {
  if (index === 0) return 'pl-title'
  if (/^(文件编号|版本|生效日期)[：:]/.test(line)) return 'pl-meta'
  if (/^(归口部门|适用范围)[：:]/.test(line)) return 'pl-scope'
  if (/^第[一二三四五六七八九十百\d]+条/.test(line)) return 'pl-article'
  return 'pl-body'
}

/** 取抽取字段值（null-safe；模板里频繁判空，抽成函数避免内联表达式类型坑）。 */
function extVal(key: string): unknown {
  return ext.value?.[key]
}

/** 抽取字段展示文本：付款期次走 paymentText 拼接，其余直接转字符串。 */
function extText(key: string): string {
  if (key === 'payment_schedule') return paymentText(extVal(key))
  // 品类值是机器码（如 agri_goods），展示层永远给中文
  if (key === 'contract_kind') return kindLabel(extVal(key) as string | null | undefined)
  const v = extVal(key)
  return v == null ? '' : String(v)
}

/** 报告/评级文案：疑似空白模板的 conditional_pass 展示为"待确认"。 */
function gradeDisplay(grade: string | null | undefined): string {
  if (templateNotice.value && grade === 'conditional_pass') return '待确认'
  return gradeText[grade ?? ''] ?? grade ?? '—'
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

/** U2：打开原文抽屉。有 clause 时定位到对应条款块；无条款号但有 evidence 的
 *  中风险项（如"保密条款缺失"）按摘录定位到所在条款块——报告里所有风险都能
 *  "原文定位"，而不是只有带条款号的高风险。 */
function openSource(clause?: string, evidence?: string) {
  // seq 自增：同一目标反复点击也会触发抽屉内 watch 重新滚动
  if (clause || evidence) {
    sourceAnchor.value = {
      clause: clause ?? '',
      evidence: evidence ?? '',
      seq: (sourceAnchor.value?.seq ?? 0) + 1,
    }
  } else {
    sourceAnchor.value = null
  }
  showSource.value = true
}
</script>

<template>
  <section class="rise task">
    <div class="bar">
      <button class="btn btn-ghost" @click="emit('back')">← 返回</button>
      <!-- U2：gate/done/error 时点开抽屉核对原文；审查中也可开（自动等原文） -->
      <button
        v-if="detail && detail.status !== 'pending'"
        class="btn btn-ghost btn-source"
        @click="openSource()"
      >
        查看原合同  
      </button>
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
      <p class="muted">正在抽取字段 → 规则审查 → 政策比对，{{ etaText }}</p>
    </div>

    <!-- 待审批闸口（与报告页同款两栏：左=待审风险与审批，右=结论预览/原文核对） -->
    <div v-else-if="detail.status === 'gate' && detail.gate_payload" class="gate">
      <div class="rep-grid">
        <div class="rep-main">
          <!-- 文件头：文件名 + 任务号 + 当前状态 -->
          <div class="card pad head">
            <div class="h-main">
              <p class="file serif">{{ detail.source }}</p>
              <p class="meta muted mono-num">{{ detail.thread_id }}</p>
            </div>
            <span class="stamp stamp-seal">待人工审批</span>
          </div>

          <!-- 警报条：解释为什么停在这里 -->
          <div class="card pad gate-alert">
            <div class="alert-top">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"></path><path d="M12 8v4M12 15.5v.5"></path></svg>
              <h3>高风险，需人工审批</h3>
            </div>
            <p class="muted">检测到 {{ detail.gate_payload.high_risks.length }} 项高风险：请核对原文条款与政策依据后选择放行或打回；审批意见与动作会写入最终报告留痕。</p>
          </div>

          <!-- 待审风险清单：高风险逐条展示（可点原文定位） -->
          <h4>待审风险</h4>
          <div v-for="(r, i) in detail.gate_payload.high_risks" :key="i" class="risk card">
            <div class="risk-top">
              <span class="stamp stamp-seal">{{ severityText.high }}</span>
              <span class="risk-type serif">{{ riskLabel(r) }}</span>
              <span v-if="r.origin === 'review'" class="origin-badge" title="独立复核盲审补抓，未参考主审结论">复核新增</span>
              <span v-if="r.policy_ref" class="mono-num ref">{{ r.policy_ref }}</span>
            </div>
            <button v-if="r.clause_ref || r.evidence" class="clause-link"
                    @click="openSource(r.clause_ref ?? '', r.evidence_quote || r.evidence || '')">
              {{ r.clause_ref ? `条款：${r.clause_ref} · 原文定位` : '原文定位' }}
            </button>
            <!-- 这种情况是：既无条款号也无摘录（字段类规则没抽到原文锚点）→
                 明说"定位不了"，别让人以为是功能坏了 -->
            <p v-else class="clause-none">正文里没有可直接指路的表述，请人工通读核对</p>
            <p v-if="r.evidence" class="quote">「{{ r.evidence }}」</p>
            <p v-if="r.suggestion" class="suggest">{{ prettyField(r.suggestion) }}</p>
          </div>

          <!-- 独立复核（双审）：闸口阶段报告还没生成，把复核结论一并展示，
               审批人放行/打回前就能看到盲审发现了什么、哪些只提示 -->
          <div v-if="gateReview && gateReview.mode === 'double'" class="card pad rv-card">
            <div class="rv-head">
              <h4 class="rv-title">独立复核（盲审）</h4>
              <span class="muted rv-desc">复核只看原文与政策，不看主审结论</span>
            </div>
            <p v-if="gateReview.error" class="err">{{ gateReview.error }}</p>
            <!-- 有分类统计时不再重复整句摘要（一句话与一排统计说的是同一件事）；
                 只有"无发现"那种没有 chips 的情况才用整句 -->
            <p v-else-if="gateReview.summary && !gateReviewChips.length" class="rv-summary">
              {{ gateReview.summary }}
            </p>
            <div v-if="gateReviewChips.length" class="rv-chips">
              <span class="rv-chips-label">结论统计</span>
              <span v-for="c in gateReviewChips" :key="c.key" class="chip" :class="outcomeClass[c.key]">
                {{ c.label }} {{ c.count }} 条
              </span>
            </div>
            <ul v-if="gateReview.details?.length" class="rv-list">
              <li v-for="(d, i) in gateReview.details" :key="i" class="rv-row">
                <div class="rv-line">
                  <span class="chip" :class="outcomeClass[d.outcome]">{{ outcomeText[d.outcome] ?? d.outcome }}</span>
                  <span v-if="d.severity" class="rv-sev" :class="severityClass[d.severity]">{{ severityText[d.severity] }}</span>
                  <span class="rv-type serif">{{ riskLabel(d) }}</span>
                  <span v-if="d.clause_ref" class="rv-clause"><span class="rv-clause-lbl">条款</span>{{ d.clause_ref }}</span>
                  <span v-if="d.policy_ref" class="mono-num ref">{{ d.policy_ref }}</span>
                </div>
                <p v-if="d.evidence" class="rv-ev">「{{ d.evidence }}」</p>
                <p v-if="d.note" class="rv-note">处理：{{ d.note }}</p>
                <button v-if="d.clause_ref || d.evidence" class="clause-link"
                        @click="openSource(d.clause_ref ?? '', d.evidence ?? '')">原文定位</button>
              </li>
            </ul>
            <!-- 结果术语解释：不解释的话"仅记录不并入/取高升级"这类词看不懂 -->
            <div class="rv-legend">
              <p class="rv-legend-t">结果说明</p>
              <ol>
                <li><b>与主审一致</b>：双方都报，无分歧</li>
                <li><b>复核新增</b>：主审漏检、复核补抓，已并入风险清单</li>
                <li><b>取高升级</b>：双方都报、复核级别更高，按高的记</li>
                <li><b>仅记录不并入</b>：复核发现未过确定性校验、或类型不属可并入范围，只记录、不进风险清单</li>
              </ol>
            </div>
          </div>

          <!-- 人工审批：意见 + 放行/打回/编辑重审 -->
          <div class="card pad approval-card">
            <h3>人工审批</h3>
            <div class="approval">
              <textarea v-model="note" rows="2" placeholder="审批意见（打回必填原因，留痕可追溯）"></textarea>
              <p v-if="actionError" class="err">{{ actionError }}</p>
              <div class="btns">
                <button class="btn btn-primary" :disabled="acting" @click="doApprove">放行</button>
                <button class="btn btn-ghost reject" :disabled="acting" @click="doReject">打回</button>
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

        <!-- 右栏：结论预览 + 原文核对（闸口阶段还没有最终报告/抽取字段落库） -->
        <aside class="rep-side">
          <div class="card s-card">
            <h3>审批结论预览</h3>
            <div class="s-verdict">
              <span class="ring ring-seal">{{ gradeText[detail.grade ?? 'fail'] ?? '不通过' }}</span>
              <div class="s-verdict-meta">
                <span class="stamp stamp-seal">待审批</span>
                <span class="s-mode muted">高风险 {{ detail.gate_payload.high_risks.length }} 项</span>
              </div>
            </div>
          </div>

          <div class="card s-card">
            <h3>原文核对</h3>
            <template v-if="hitClauses.length">
              <button
                v-for="h in hitClauses"
                :key="h.clause"
                class="s-clause"
                @click="openSource(h.clause, h.quote)"
              >
                <span class="dot-sev" :class="h.sev"></span>
                <span class="c-txt">{{ h.clause }}</span>
                <span class="mono-num c-cnt">命中 {{ h.count }}</span>
              </button>
            </template>
            <p v-else class="s-empty muted">本任务暂无命中条款，可打开原文人工核对</p>
            <button class="btn btn-ghost s-more" @click="openSource()">打开原文全文</button>
          </div>
        </aside>
      </div>
    </div>

    <!-- 完成：报告（宽屏两栏：左=主流程，右=结论速览/关键字段/原文核对） -->
    <div v-else-if="detail.status === 'done' && detail.report" class="report">
      <div class="rep-grid">
        <div class="rep-main">
          <!-- 报告头：文件名 + 元信息 + 导出 -->
          <div class="card pad head">
            <div class="h-main">
              <p class="file serif">{{ detail.source }}</p>
              <p class="meta muted mono-num">{{ detail.thread_id }}</p>
            </div>
            <button class="btn btn-ghost" @click="downloadReport">导出 JSON</button>
          </div>

          <!-- 审批留痕：done 报告回显最近一次审批动作与意见(动作徽章 + 意见块, 避免横排挤成三列) -->
          <div v-if="detail.report.approval" class="card pad appr">
            <div class="appr-head">
              <span class="muted">审批记录</span>
              <span class="chip" :class="detail.report.approval.action">
                {{ detail.report.approval.action === 'approved' ? '放行' : detail.report.approval.action === 'rejected' ? '打回' : '编辑重审' }}
              </span>
            </div>
            <p v-if="detail.report.approval.reviewer_note" class="note">{{ detail.report.approval.reviewer_note }}</p>
            <p v-else class="note none">（未填写审批意见）</p>
          </div>

          <!-- 结论条：疑似空白模板单独成结论，不混进下方风险清单 -->
          <div v-if="templateNotice" class="card pad tpl-notice">
            <p class="tpl-title serif">结论：疑似空白模板，未填写内容较多</p>
            <p v-if="templateNotice.evidence" class="tpl-ev">占位示例：「{{ templateNotice.evidence }}」</p>
            <p class="tpl-sug">{{ prettyField(templateNotice.suggestion ?? '') }}</p>
          </div>

          <!-- 双审复核段：盲审独立结论与主审 diff 的结果（一致/新增/升级/仅记录不并入） -->
          <div v-if="review && review.mode === 'double'" class="card pad rv-card">
            <div class="rv-head">
              <h4 class="rv-title">独立复核（盲审）</h4>
              <span class="muted rv-desc">复核只看原文与政策，不看主审结论</span>
            </div>
            <p v-if="reviewError" class="err">{{ reviewError }}</p>
            <!-- 同上：统计交给下面的"结论统计"chips，避免同一件事说两遍 -->
            <p v-else-if="review.summary && !reviewChips.length" class="rv-summary">
              {{ review.summary }}
            </p>
            <div v-if="reviewChips.length" class="rv-chips">
              <span class="rv-chips-label">结论统计</span>
              <span v-for="c in reviewChips" :key="c.key" class="chip" :class="outcomeClass[c.key]">
                {{ c.label }} {{ c.count }} 条
              </span>
            </div>
            <ul v-if="review.details?.length" class="rv-list">
              <li v-for="(d, i) in review.details" :key="i" class="rv-row">
                <div class="rv-line">
                  <span class="chip" :class="outcomeClass[d.outcome]">{{ outcomeText[d.outcome] ?? d.outcome }}</span>
                  <span v-if="d.severity" class="rv-sev" :class="severityClass[d.severity]">{{ severityText[d.severity] }}</span>
                  <span class="rv-type serif">{{ riskLabel(d) }}</span>
                  <span v-if="d.clause_ref" class="rv-clause"><span class="rv-clause-lbl">条款</span>{{ d.clause_ref }}</span>
                  <span v-if="d.policy_ref" class="mono-num ref">{{ d.policy_ref }}</span>
                </div>
                <!-- 复核看到的原文（最多两行，点开详情仍可在原文抽屉里定位） -->
                <p v-if="d.evidence" class="rv-ev">「{{ d.evidence }}」</p>
                <!-- 处理结果说明：为什么并入/为什么只提示（复核门未过、类型白名单、已降级等） -->
                <p v-if="d.note" class="rv-note">处理：{{ d.note }}</p>
                <button v-if="d.clause_ref || d.evidence" class="clause-link"
                        @click="openSource(d.clause_ref ?? '', d.evidence ?? '')">原文定位</button>
              </li>
            </ul>
            <div class="rv-legend">
              <p class="rv-legend-t">结果说明</p>
              <ol>
                <li><b>与主审一致</b>：双方都报，无分歧</li>
                <li><b>复核新增</b>：主审漏检、复核补抓，已并入风险清单</li>
                <li><b>取高升级</b>：双方都报、复核级别更高，按高的记</li>
                <li><b>仅记录不并入</b>：复核发现未过确定性校验、或类型不属可并入范围，只记录、不进风险清单</li>
              </ol>
            </div>
          </div>

          <!-- 风险清单：空=自动放行提示，非空逐条展示 -->
          <template v-if="listRisks.length">
            <h4>风险清单</h4>
            <div v-for="(r, i) in listRisks" :key="i" class="risk card">
              <div class="risk-top">
                <span class="stamp" :class="severityClass[r.severity]">{{ severityText[r.severity] }}</span>
                <span class="risk-type serif">{{ riskLabel(r) }}</span>
                <span v-if="r.origin === 'review'" class="origin-badge" title="独立复核盲审补抓，未参考主审结论">复核新增</span>
                <span v-if="r.policy_ref" class="mono-num ref">{{ r.policy_ref }}</span>
              </div>
              <button v-if="r.clause_ref || r.evidence" class="clause-link"
                @click="openSource(r.clause_ref ?? '', r.evidence_quote || r.evidence || '')">
                {{ r.clause_ref ? `条款：${r.clause_ref} · 原文定位` : '原文定位' }}
              </button>
              <!-- 同上：没有条款号也没有摘录时明说定位不了，避免被当成漏做功能 -->
              <p v-else class="clause-none">正文里没有可直接指路的表述，请人工通读核对</p>
              <p v-if="r.evidence" class="quote">「{{ r.evidence }}」</p>
              <p v-if="r.suggestion" class="suggest">{{ prettyField(r.suggestion) }}</p>
            </div>
          </template>
          <template v-else-if="!templateNotice">
            <p class="none ok-text serif">未发现风险 · 自动放行</p>
          </template>

          <!-- 政策引用：policy_ref + 相似度 + 制度原文片段 -->
          <template v-if="detail.report.policy_hits?.length">
            <h4>政策引用</h4>
            <div v-for="(h, i) in detail.report.policy_hits" :key="i" class="card pad hit">
              <!-- 逐行排版：编号 / 相似度 / 标题 / 元信息 / 条文各占一行 -->
              <p class="pl pl-ref"><span class="mono-num ref">{{ h.policy_ref }}</span></p>
              <p v-if="h.score != null" class="pl pl-score muted">相似度 {{ Number(h.score).toFixed(3) }}</p>
              <template v-if="policyReflow(h.snippet ?? '').length">
                <p
                  v-for="(ln, li) in policyReflow(h.snippet ?? '')"
                  :key="li"
                  class="pl"
                  :class="policyRowClass(li, ln)"
                >
                  <template v-if="policyRowParts(ln).lbl">
                    <span class="lbl">{{ policyRowParts(ln).lbl }}</span>{{ policyRowParts(ln).val }}
                  </template>
                  <template v-else>{{ ln }}</template>
                </p>
              </template>
              <!-- 完整条文默认收起，需要核对政策依据时展开看全文（不再只看截断片段） -->
              <details v-if="h.full_text || h.text" class="policy-more">
                <summary class="muted">查看完整条文</summary>
                <div class="policy-full">
                  <p v-for="(ln, li) in policyReflow(h.full_text ?? h.text ?? '')" :key="li" :class="policyRowClass(li, ln)">
                    <template v-if="policyRowParts(ln).lbl">
                      <span class="lbl">{{ policyRowParts(ln).lbl }}</span>{{ policyRowParts(ln).val }}
                    </template>
                    <template v-else>{{ ln }}</template>
                  </p>
                </div>
              </details>
            </div>
          </template>
        </div>

        <!-- 右栏：sticky 速览（窄屏自动落到主流程下方） -->
        <aside class="rep-side">
          <div class="card s-card">
            <h3>报告结论</h3>
            <div class="s-verdict">
              <span class="ring" :class="ringClass[detail.report.grade ?? ''] ?? 'ring-mute'">
                {{ gradeDisplay(detail.report.grade) }}
              </span>
              <div class="s-verdict-meta">
                <!-- 疑似空白模板在展示层标"待确认"（琥珀），避免"已完成"的误导 -->
                <span class="stamp" :class="detail.template ? 'stamp-warn' : 'stamp-ok'">
                  {{ detail.template ? '待确认' : '已完成' }}
                </span>
                <span class="s-mode muted">审查模式：{{ reviewModeText[detail.report.review_mode ?? ''] ?? '单审' }}</span>
              </div>
            </div>
            <div class="s-stats">
              <div class="s-stat">
                <span>风险</span>
                <b class="mono-num" :class="{ bad: listRisks.length > 0 }">{{ listRisks.length }}</b>
              </div>
              <div class="s-stat">
                <span>政策引用</span>
                <b class="mono-num">{{ detail.report.policy_hits?.length ?? 0 }}</b>
              </div>
            </div>
            <!-- LLM 用量（成本可见）：服务端与评测链路同口径（只计 chat 调用，不含本地规则/检索） -->
            <p v-if="detail.report.llm" class="muted s-llm mono-num">
              大模型调用 {{ detail.report.llm.calls }} 次 · 合计 {{ detail.report.llm.seconds }} 秒
            </p>
          </div>

          <!-- 关键抽取字段：只展示有值字段（null 不占行），右栏速览用 -->
          <div v-if="detail.report.extracted" class="card s-card">
            <h3>关键字段</h3>
            <div class="s-kvs">
              <template v-for="(label, key) in fieldLabels" :key="key">
                <div v-if="extVal(key) != null" class="s-kv">
                  <dt>{{ label }}</dt>
                  <dd class="mono-num">{{ extText(key) }}</dd>
                </div>
              </template>
              <div v-if="paymentText(extVal('payment_schedule'))" class="s-kv">
                <dt>付款期次</dt>
                <dd class="mono-num">{{ paymentText(extVal('payment_schedule')) }}</dd>
              </div>
            </div>
          </div>

          <!-- 原文核对：聚合命中条款，点任意一条即滑出抽屉定位 -->
          <div class="card s-card">
            <h3>原文核对</h3>
            <template v-if="hitClauses.length">
              <button
                v-for="h in hitClauses"
                :key="h.clause"
                class="s-clause"
                @click="openSource(h.clause, h.quote)"
              >
                <span class="dot-sev" :class="h.sev"></span>
                <span class="c-txt">{{ h.clause }}</span>
                <span class="mono-num c-cnt">命中 {{ h.count }}</span>
              </button>
            </template>
            <p v-else class="s-empty muted">本任务暂无命中条款，可打开原文通读核对</p>
            <button class="btn btn-ghost s-more" @click="openSource()">打开原文全文</button>
          </div>
        </aside>
      </div>
    </div>

    <!-- 失败 -->
    <div v-else-if="detail.status === 'error'" class="card pad err-box">
      <h3>审查失败</h3>
      <p>{{ detail.error || detail.report?.error || '未知错误' }}</p>
    </div>

    <!-- U2 原文抽屉：锚点与当前任务号绑定 -->
    <SourceDrawer
      v-if="showSource"
      :thread-id="props.threadId"
      :anchor="sourceAnchor"
      :risks="drawerRisks"
      @close="showSource = false"
    />
  </section>
</template>

<style scoped>
.task {
  max-width: 1240px;
}

.bar {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 16px;
}

.btn-source {
  margin-left: auto;
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
  font-size: 20px;
  letter-spacing: 0.04em;
  color: var(--info);
}

.risk-top {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.report h4 {
  margin: 0;
  font-weight: 700;
}

/* 评级徽章在卡片头/右栏里适当收敛（全局 .ring 是 108px 宽的色块） */
.s-verdict .ring {
  min-width: 92px;
  height: 38px;
  font-size: 13.5px;
}

/* ---- 闸口页（与报告页同款两栏）---- */
.gate h4 {
  margin: 8px 0 10px;
  padding-left: 10px;
  border-left: 3px solid var(--seal);
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.02em;
  line-height: 1.4;
}

.gate-alert {
  border: 1px solid rgba(224, 69, 79, 0.2);
  border-left: 3px solid var(--seal);
  background: var(--seal-soft);
}

.alert-top {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--seal-deep);
}

.alert-top svg {
  width: 16px;
  height: 16px;
  flex: none;
}

.alert-top h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--ink);
}

.gate-alert p {
  margin: 6px 0 0;
  font-size: 13px;
}

.approval-card h3 {
  margin: 0 0 6px;
  font-size: 14.5px;
  font-weight: 700;
}

.approval-card .approval {
  margin-top: 0;
}

/* 打回 = 拦截动作，用红描边表达否定语义（区别于主操作的靛蓝） */
.btn-ghost.reject {
  color: var(--seal-deep);
  border-color: rgba(224, 69, 79, 0.45);
}

.btn-ghost.reject:hover:not(:disabled) {
  color: var(--seal-deep);
  border-color: var(--seal);
  background: var(--seal-soft);
}

.risk {
  padding: 12px 16px;
  margin: 10px 0;
  border-left: 3px solid var(--line);
  border-radius: 8px;
}

.risk-type {
  font-weight: 700;
}

.ref {
  font-size: 12px;
  color: var(--pri);
  border: 1px solid rgba(52, 86, 209, 0.22);
  background: var(--pri-soft);
  border-radius: 6px;
  padding: 1px 8px;
}

.quote {
  /* 证据 = 原文章节摘录：靛蓝细边 + 浅灰蓝底，保持等宽数字与正文区分 */
  border-left: 3px solid var(--pri);
  background: var(--card-2);
  padding: 8px 12px;
  margin: 6px 0;
  color: var(--ink-2);
  font-size: 14px;
  border-radius: 0 8px 8px 0;
}

.suggest {
  margin: 4px 0;
}

/* U2：条款引用行做成可点的原文定位入口（hover 下划线提示可点） */
.clause-link {
  border: 0;
  background: transparent;
  padding: 0;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  color: var(--pri);
  font-weight: 600;
}

.clause-link:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
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
  gap: 14px;
}

.head .file {
  font-size: 16px;
  font-weight: 700;
  margin: 0 0 2px;
  word-break: break-all;
}

.h-main {
  min-width: 0;
}

.head p {
  margin: 0;
}

.head .meta {
  font-size: 12px;
  margin-top: 2px;
}

.appr {
  margin-top: 12px;
  background: var(--card-2);
  border-radius: 8px;
  padding: 12px 16px;
  font-size: 14.5px;
}

.appr-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

/* 标签不随全局 .muted 走浅灰, 在卡片内加深加粗, 保证可读 */
.appr-head .muted {
  color: var(--ink);
  font-size: 14px;
  font-weight: 600;
}

/* 动作徽章: 放行绿 / 打回红 / 编辑重审靛蓝(与闸口操作语义一致) */
.appr .chip {
  padding: 2px 12px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
}

.appr .chip.approved {
  color: var(--ok);
  background: var(--ok-soft);
}

.appr .chip.rejected {
  color: var(--seal-deep);
  background: var(--seal-soft);
}

.appr .chip.edited {
  color: var(--pri-deep);
  background: var(--pri-soft);
}

.appr .note {
  margin: 0;
  color: var(--ink);
  font-size: 14.5px;
  white-space: pre-wrap; /* 保留打回原因里的换行, 条目式意见按行展示 */
  word-break: break-word;
  line-height: 1.7;
}

.appr .note.none {
  color: var(--ink-2);
}

/* 结论条：疑似空白模板 = 待确认（琥珀语义色面板，区别于风险清单卡片） */
.tpl-notice {
  margin-top: 12px;
  border-left: 4px solid var(--warn);
  background: var(--warn-soft);
  border-radius: 8px;
}

.tpl-title {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.03em;
  color: var(--warn);
}

.tpl-ev {
  margin: 0 0 6px;
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--ink-2);
}

.tpl-sug {
  margin: 0;
  font-size: 13.5px;
  color: var(--ink-2);
}

.report h4 {
  margin: 22px 0 8px;
  padding-left: 10px;
  border-left: 3px solid var(--pri);
  font-size: 15.5px;
  letter-spacing: 0.02em;
  line-height: 1.4;
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

/* 政策卡逐行排版（C 语义色分层）：标题=靛蓝、相似度=琥珀、元信息=灰蓝、
   适用范围标签=靛蓝、条文头=墨色加粗 */
.pl {
  margin: 0;
  font-size: 14.5px;
  line-height: 1.9;
  font-weight: 500;
  color: var(--ink-2);
}

.pl-ref {
  margin-bottom: 2px;
}

.hit .ref {
  font-size: 13px;
  padding: 2px 11px;
}

.pl-score {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 700;
  color: var(--warn);
}

/* 细则标题：靛蓝、最大最粗 */
.pl-title {
  color: var(--pri);
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

/* 文件编号/版本/生效日期：小号元信息，标签琥珀强调 */
.pl-meta {
  font-size: 13px;
  color: var(--muted);
}

.pl-meta .lbl {
  color: var(--warn);
  font-weight: 700;
  margin-right: 0.2em;
}

/* 归口部门/适用范围：正文墨色，标签靛蓝 */
.pl-scope .lbl {
  color: var(--pri);
  font-weight: 700;
  margin-right: 0.2em;
}

.pl-scope {
  font-size: 14.5px;
  color: var(--ink);
}

/* 条文头（第X条）：墨色加粗 */
.pl-article {
  color: var(--ink);
  font-weight: 700;
}

.pl-article .lbl {
  color: var(--pri);
}

.snip {
  margin: 6px 0 0;
  color: var(--ink-2);
  font-size: 13.5px;
  white-space: pre-line;
}

/* 政策完整条文：默认收起的展开块，浅底等宽便于核对原文 */
.policy-more {
  margin-top: 8px;
  font-size: 13px;
}

.policy-more summary {
  cursor: pointer;
  user-select: none;
  letter-spacing: 0.04em;
}

.policy-full {
  margin: 8px 0 0;
  padding: 10px 12px;
  background: var(--card-2);
  border: 1px solid var(--line);
  border-radius: 8px;
  font-size: 14px;
  line-height: 1.9;
  color: var(--ink-2);
}

/* 每条逻辑行按容器整宽自然换行，避免源文件手工折行造成右半空白 */
.policy-full p {
  margin: 0 0 0.35em;
  overflow-wrap: break-word;
}

/* ---- 报告两栏：左主流程 + 右侧 sticky 速览（窄屏自动回落单栏） ---- */
.rep-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 304px;
  gap: 18px;
  align-items: start;
}

.rep-main {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

.rep-side {
  position: sticky;
  top: 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

@media (max-width: 1080px) {
  .rep-grid {
    grid-template-columns: 1fr;
  }

  .rep-side {
    position: static;
  }
}

/* 右栏卡片 */
.s-card {
  padding: 14px 16px;
}

.s-card h3 {
  font-size: 15px;
  font-weight: 700;
  margin: 0 0 12px;
}

.s-verdict {
  display: flex;
  align-items: center;
  gap: 13px;
}

.s-verdict-meta {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.s-mode {
  font-size: 13px;
  color: var(--ink-2);
}

.s-stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

/* LLM 用量行：成本可见（口径 = chat 调用，双读/盲审都算一次） */
.s-llm {
  margin: 10px 0 0;
  font-size: 12.5px;
}

.s-stat {
  display: flex;
  flex-direction: column;
}

.s-stat span {
  font-size: 13px;
  color: var(--ink-2);
  font-weight: 600;
}

.s-stat b {
  font-size: 20px;
  line-height: 1.3;
}

.s-stat b.bad {
  color: var(--seal-deep);
}

.s-kvs {
  display: flex;
  flex-direction: column;
}

.s-kv {
  padding: 7px 0;
  border-bottom: 1px solid var(--line);
}

.s-kv:last-child {
  border-bottom: 0;
}

.s-kv dt {
  font-size: 12.5px;
  color: var(--ink-2);
  font-weight: 500;
  letter-spacing: 0.02em;
}

.s-kv dd {
  margin: 2px 0 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
  word-break: break-all;
}

/* 原文核对条目：可点滑出抽屉并定位条款 */
.s-clause {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  text-align: left;
  background: none;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 7px 10px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: border-color 0.12s ease, background 0.12s ease;
}

.s-clause:hover {
  border-color: var(--pri);
  background: #f8faff;
}

.dot-sev {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
  background: var(--muted);
}

.dot-sev.high {
  background: var(--seal);
}

.dot-sev.medium {
  background: var(--warn);
}

.c-txt {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.c-cnt {
  font-size: 12px;
  color: var(--ink-2);
  flex: none;
}

.s-empty {
  font-size: 13px;
  color: var(--ink-2);
  margin-bottom: 10px;
}

.s-more {
  width: 100%;
  justify-content: center;
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
  font-size: 12.5px;
  letter-spacing: 0.02em;
  font-weight: 500;
}

.step + .step::before {
  content: '';
  width: 26px;
  height: 1px;
  background: var(--line);
  margin-right: 4px;
}

.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  border: 2px solid var(--line-strong);
  background: #fff;
}

.step.on {
  color: var(--pri);
  font-weight: 700;
}

.step.on .dot {
  border-color: var(--pri);
  background: var(--pri);
  box-shadow: 0 0 0 3px var(--pri-soft);
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
  background: var(--seal);
  box-shadow: 0 0 0 3px var(--seal-soft);
}

/* ---- 双审复核段（report.review）与"复核新增"徽标 ---- */
.origin-badge {
  align-self: center;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--pri-soft);
  color: var(--pri);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.rv-card {
  margin: 12px 0;
  border-left: 3px solid var(--pri);
}

.rv-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}

.rv-title {
  margin: 0;
  font-size: 15px;
}

.rv-desc {
  font-size: 11.5px;
}

.rv-summary {
  margin: 4px 0 8px;
  font-size: 13px;
  color: #444;
}

.rv-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

/* 统计行前缀：不加说明的话"仅提示 1"会被读成一个名字 */
.rv-chips-label {
  font-size: 12px;
  color: var(--muted);
}

/* 结论徽标基础样式：原来只有底色、没有内边距，看起来像被选中的文字而不是标签 */
.chip {
  display: inline-flex;
  align-items: center;
  padding: 2px 9px;
  border-radius: 6px;
  font-size: 12.5px;
  font-weight: 600;
  line-height: 1.5;
  white-space: nowrap;
}

.rv-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.rv-row {
  padding: 8px 0;
  font-size: 13.5px;
}

.rv-row + .rv-row {
  border-top: 1px dashed var(--line);
}

/* 首行：结果徽标 + 严重级 + 类型 + 条款 + 政策（字号比原来大一号、类型加粗） */
.rv-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.rv-type {
  font-weight: 800;
  font-size: 14px;
  color: #111;
}

.rv-sev {
  font-size: 12.5px;
  font-weight: 600;
}

/* 条款引用：原来只有 muted 小字，淡到看不出指什么
   → 加"条款"标签 + 提到正文色，与右侧政策号形成同一种"可读小标签"语言 */
.rv-clause {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  padding: 1px 8px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: var(--card-2);
  font-size: 12.5px;
  color: var(--ink-2);
}

.rv-clause-lbl {
  font-size: 11px;
  color: var(--muted);
}

/* 无原文定位时的说明：字段类风险找不到锚点，明说"指不了路"而不是留白 */
.clause-none {
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--muted);
}

/* 复核看到的原文：最多两行，避免长段落撑爆卡片 */
.rv-ev {
  margin: 6px 0 0 4px;
  padding-left: 10px;
  border-left: 3px solid var(--line);
  font-size: 13px;
  line-height: 1.55;
  color: #333;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* 处理说明：为什么"仅记录不并入"（复核门未过/类型白名单/已降级）——不写清楚用户会以为漏判 */
.rv-note {
  margin: 4px 0 0 4px;
  font-size: 12.5px;
  color: #8a5a00;
}

/* 结果术语说明：原来 12px 浅灰一行、用分号分点，字太小太细看不清 → 放大加深、
   改成分点列表，并加浅底细框让它像"说明卡"而不是正文边角料 */
.rv-legend {
  margin: 10px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--card-2);
  font-size: 13px;
  line-height: 1.7;
  color: var(--ink-2);
}

.rv-legend-t {
  margin: 0 0 4px;
  font-weight: 700;
  color: var(--ink);
}

.rv-legend ol {
  margin: 0;
  padding-left: 20px;
}

.rv-legend li + li {
  margin-top: 2px;
}

.rv-legend b {
  color: var(--ink);
}

/* outcome 徽标色：一致=灰绿、新增=主色、升级=朱、仅记录不并入=灰 */
.chip.rv-agree {
  background: var(--ok-soft);
  color: var(--ok);
}

.chip.rv-added {
  background: var(--pri-soft);
  color: var(--pri);
}

.chip.rv-upgrade {
  background: var(--seal-soft);
  color: var(--seal);
}

.chip.rv-note {
  background: var(--line);
  color: var(--muted);
}

</style>
