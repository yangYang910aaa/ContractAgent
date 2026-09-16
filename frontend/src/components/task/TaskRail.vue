<!--
  详情页右栏（sticky 速览）：闸口态给"审批结论预览 + 原文核对"，报告态给
  "报告结论 + 关键字段 + 原文核对"。两种态共用外框，差异只在上面两张卡。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { gradeDisplay, GRADE_TEXT, kindLabel, REVIEW_MODE_TEXT, RING_CLASS } from '../../labels'
import type { RiskCardItem, TaskDetail } from '../../types'

const props = defineProps<{
  detail: TaskDetail
  mode: 'gate' | 'report'
  risks: RiskCardItem[] // 报告风险（不含"疑似空白模板"结论条）
  hits: { clause: string; sev: string; count: number; quote: string }[] // 命中的条款（去重聚合）
}>()

const emit = defineEmits<{
  locate: [clause: string, quote: string]
  openSource: []
}>()

// 关键字段展示名（右栏专用：带单位，方便不用回头看正文）
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

const extracted = computed(() => props.detail.report?.extracted ?? null)

/** 取抽取字段值（null-safe；模板里频繁判空，抽成函数避免内联表达式类型坑）。 */
function extVal(key: string): unknown {
  return extracted.value?.[key]
}

/** 付款期次：逐期拼"名称 金额 元（比例%）"，只显示抽到的部分。 */
function paymentText(raw: unknown): string {
  if (!Array.isArray(raw) || !raw.length) return ''
  return raw
    .map((p: Record<string, unknown>) => {
      const parts: string[] = []
      if (p?.amount != null) parts.push(`${p.amount} 元`)
      if (p?.percent != null) parts.push(`（${Number(p.percent)}%）`)
      return `${p?.name ?? '期次'} ${parts.join('')}`.trim()
    })
    .join('；')
}

/** 抽取字段展示文本：付款期次单独拼，品类转中文，其余直接转字符串。 */
function extText(key: string): string {
  if (key === 'payment_schedule') return paymentText(extVal(key))
  // 品类值是机器码（如 agri_goods），展示层永远给中文
  if (key === 'contract_kind') return kindLabel(extVal(key) as string | null | undefined)
  const value = extVal(key)
  return value == null ? '' : String(value)
}
</script>

<template>
  <!-- 布局类（.rep-side）由详情页传入 -->
  <aside>
    <!-- 闸口态：报告还没生成，只给结论预览 -->
    <div v-if="mode === 'gate'" class="card s-card">
      <h3>审批结论预览</h3>
      <div class="s-verdict">
        <span class="ring ring-seal">{{ GRADE_TEXT[detail.grade ?? 'fail'] ?? '不通过' }}</span>
        <div class="s-verdict-meta">
          <span class="stamp stamp-seal">待审批</span>
          <span class="s-mode muted"
            >高风险 {{ detail.gate_payload?.high_risks.length ?? 0 }} 项</span
          >
        </div>
      </div>
    </div>

    <!-- 报告态：结论 + 成本 + 关键字段 -->
    <template v-else>
      <div class="card s-card">
        <h3>报告结论</h3>
        <div class="s-verdict">
          <span class="ring" :class="RING_CLASS[detail.report?.grade ?? ''] ?? 'ring-mute'">
            {{ gradeDisplay(detail.report?.grade, detail.template) }}
          </span>
          <div class="s-verdict-meta">
            <!-- 疑似空白模板在展示层标"待确认"（琥珀），避免"已完成"的误导 -->
            <span class="stamp" :class="detail.template ? 'stamp-warn' : 'stamp-ok'">
              {{ detail.template ? '待确认' : '已完成' }}
            </span>
            <span class="s-mode muted"
              >审查模式：{{ REVIEW_MODE_TEXT[detail.report?.review_mode ?? ''] ?? '单审' }}</span
            >
          </div>
        </div>
        <div class="s-stats">
          <div class="s-stat">
            <span>风险</span>
            <b class="mono-num" :class="{ bad: risks.length > 0 }">{{ risks.length }}</b>
          </div>
          <div class="s-stat">
            <span>政策引用</span>
            <b class="mono-num">{{ detail.report?.policy_hits?.length ?? 0 }}</b>
          </div>
        </div>
        <!-- LLM 用量（成本可见）：只计 chat 调用，不含本地规则/检索 -->
        <p v-if="detail.report?.llm" class="muted s-llm mono-num">
          大模型调用 {{ detail.report.llm.calls }} 次 · 合计 {{ detail.report.llm.seconds }} 秒
        </p>
      </div>

      <!-- 关键抽取字段：只展示有值字段（null 不占行） -->
      <div v-if="extracted" class="card s-card">
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
    </template>

    <!-- 原文核对：聚合命中条款，点任意一条即滑出抽屉定位（两种态都有） -->
    <div class="card s-card">
      <h3>原文核对</h3>
      <template v-if="hits.length">
        <button
          v-for="h in hits"
          :key="h.clause"
          class="s-clause"
          @click="emit('locate', h.clause, h.quote)"
        >
          <span class="dot-sev" :class="h.sev"></span>
          <span class="c-txt">{{ h.clause }}</span>
          <span class="mono-num c-cnt">命中 {{ h.count }}</span>
        </button>
      </template>
      <p v-else class="s-empty muted">
        {{
          mode === 'gate'
            ? '本任务暂无命中条款，可打开原文人工核对'
            : '本任务暂无命中条款，可打开原文通读核对'
        }}
      </p>
      <button class="btn btn-ghost s-more" @click="emit('openSource')">打开原文全文</button>
    </div>
  </aside>
</template>

<style scoped>
.s-card {
  padding: 14px 16px;
}

.s-card h3 {
  font-size: 15px;
  font-weight: 700;
  margin: 0 0 12px;
}

/* 评级徽章在右栏里适当收敛（全局 .ring 是 108px 宽的色块） */
.s-verdict .ring {
  min-width: 92px;
  height: 38px;
  font-size: 13.5px;
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
  transition:
    border-color 0.12s ease,
    background 0.12s ease;
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
</style>
