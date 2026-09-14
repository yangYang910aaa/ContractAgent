<!--
  独立复核（盲审）段：闸口页与报告页共用同一块结论——复核发现与主审 diff 的结果
  （与主审一致 / 复核新增 / 取高升级 / 仅记录不并入），外加一行"结果说明"解释这四个词。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { OUTCOME_CLASS, OUTCOME_TEXT, outcomeChips, riskLabel, SEVERITY_CLASS, SEVERITY_TEXT } from '../../labels'
import type { ReviewSection } from '../../types'

const props = defineProps<{ review: ReviewSection | null | undefined }>()

const emit = defineEmits<{
  // 原文定位：交给详情页打开原文抽屉并高亮
  locate: [clause: string, quote: string]
}>()

const chips = computed(() => outcomeChips(props.review?.stats))
const error = computed(() => props.review?.error ?? '')
</script>

<template>
  <div v-if="review && review.mode === 'double'" class="card pad rv-card">
    <div class="rv-head">
      <h4 class="rv-title">独立复核（盲审）</h4>
      <span class="muted rv-desc">复核只看原文与政策，不看主审结论</span>
    </div>
    <p v-if="error" class="err">{{ error }}</p>
    <!-- 有分类统计时不再重复整句摘要（一句话与一排统计说的是同一件事）；
         只有"无发现"那种没有 chips 的情况才用整句 -->
    <p v-else-if="review.summary && !chips.length" class="rv-summary">{{ review.summary }}</p>
    <div v-if="chips.length" class="rv-chips">
      <span class="rv-chips-label">结论统计</span>
      <span v-for="c in chips" :key="c.key" class="chip" :class="OUTCOME_CLASS[c.key]">
        {{ c.label }} {{ c.count }} 条
      </span>
    </div>
    <ul v-if="review.details?.length" class="rv-list">
      <li v-for="(d, i) in review.details" :key="i" class="rv-row">
        <div class="rv-line">
          <span class="chip" :class="OUTCOME_CLASS[d.outcome]">{{ OUTCOME_TEXT[d.outcome] ?? d.outcome }}</span>
          <span v-if="d.severity" class="rv-sev" :class="SEVERITY_CLASS[d.severity]">{{ SEVERITY_TEXT[d.severity] }}</span>
          <span class="rv-type serif">{{ riskLabel(d) }}</span>
          <span v-if="d.clause_ref" class="rv-clause"><span class="rv-clause-lbl">条款</span>{{ d.clause_ref }}</span>
          <span v-if="d.policy_ref" class="mono-num ref">{{ d.policy_ref }}</span>
        </div>
        <!-- 复核看到的原文（最多两行，点开详情仍可在原文抽屉里定位） -->
        <p v-if="d.evidence" class="rv-ev">「{{ d.evidence }}」</p>
        <!-- 处理结果说明：为什么并入/为什么只提示（复核门未过、类型白名单、已降级等） -->
        <p v-if="d.note" class="rv-note">处理：{{ d.note }}</p>
        <button v-if="d.clause_ref || d.evidence" class="clause-link"
                @click="emit('locate', d.clause_ref ?? '', d.evidence ?? '')">原文定位</button>
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
        <li><b>复核按通用口径</b>：独立复核不看品类，也不套"政采/校服按范本执行"的豁免，政采类合同可能出现主审未报、复核报出的条目</li>
      </ol>
    </div>
  </div>
</template>

<style scoped>
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

/* 统计行前缀：不加说明的话"仅记录不并入 1"会被读成一个名字 */
.rv-chips-label {
  font-size: 12px;
  color: var(--muted);
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

/* 首行：结果徽标 + 严重级 + 类型 + 条款 + 政策 */
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

/* 条款引用：加"条款"标签并提到正文色，与右侧政策号形成同一种"可读小标签"语言 */
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

/* 结果术语说明：放大加深 + 分点 + 浅底细框，让它像"说明卡"而不是正文边角料 */
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

/* outcome 徽标色：一致=绿、新增=主色、升级=朱、仅记录不并入=灰 */
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
