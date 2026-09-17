<!--
  单条风险卡：闸口待审与报告清单共用（同一套标记 + "原文定位 / 问这条"入口）。
  只管展示与转发交互：判定内容全部来自 props，动作交给详情页去做。
-->
<script setup lang="ts">
import { prettyField, riskLabel, SEVERITY_CLASS, SEVERITY_TEXT } from '../../labels'
import type { RiskCardItem } from '../../types'

defineProps<{ risk: RiskCardItem }>()

const emit = defineEmits<{
  // 原文定位：把条款号与原句交给详情页，由它打开原文抽屉并高亮
  locate: [clause: string, quote: string]
  // 问这条：交给详情页打开助手面板并提问
  ask: [risk: RiskCardItem]
}>()
</script>

<template>
  <!-- 展示用等级：闸口那一屏不带 severity（整屏都是高风险），按 high 处理 -->
  <div class="risk card" :data-clause="risk.clause_ref ?? ''">
    <div class="risk-top">
      <span class="stamp" :class="SEVERITY_CLASS[risk.severity ?? 'high']">
        {{ SEVERITY_TEXT[risk.severity ?? 'high'] }}
      </span>
      <span class="risk-type serif">{{ riskLabel(risk) }}</span>
      <span
        v-if="risk.origin === 'review'"
        class="origin-badge"
        title="独立复核盲审补抓，未参考主审结论"
        >复核新增</span
      >
      <span v-if="risk.policy_ref" class="mono-num ref">{{ risk.policy_ref }}</span>
    </div>
    <div class="risk-actions">
      <button
        v-if="risk.clause_ref || risk.evidence"
        class="clause-link"
        @click="emit('locate', risk.clause_ref ?? '', risk.evidence_quote || risk.evidence || '')"
      >
        {{ risk.clause_ref ? `条款：${risk.clause_ref} · 原文定位` : '原文定位' }}
      </button>
      <!-- 这种情况是：既无条款号也无摘录（字段类规则没抽到原文定位词）→
           明说"定位不了"，别让人以为是功能坏了 -->
      <p v-else class="clause-none">正文里没有可直接指路的表述，请人工通读核对</p>
      <button class="clause-link ask" title="让助手解释这条判定" @click="emit('ask', risk)">
        问这条
      </button>
    </div>
    <p v-if="risk.evidence" class="quote">「{{ risk.evidence }}」</p>
    <p v-if="risk.suggestion" class="suggest">{{ prettyField(risk.suggestion) }}</p>
  </div>
</template>

<style scoped>
.risk {
  padding: 12px 16px;
  margin: 10px 0;
  border-left: 3px solid var(--line);
  border-radius: 8px;
}

.risk-top {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.risk-type {
  font-weight: 700;
}

/* 证据 = 原文章节摘录：靛蓝细边 + 浅灰蓝底，与正文区分开 */
.quote {
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

/* 定位行：原文定位 + 问这条并排，窄屏可换行 */
.risk-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 12px;
}

/* 问这条：次一级动作（描边胶囊），与"原文定位"的链接样式区分开 */
.clause-link.ask {
  padding: 1px 8px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  font-weight: 500;
  color: var(--ink-2);
}

.clause-link.ask:hover {
  border-color: var(--pri);
  color: var(--pri);
  text-decoration: none;
}

/* 无原文定位时的说明：字段类风险找不到定位词，明说"指不了路"而不是留白 */
.clause-none {
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--muted);
}

/* 复核新增徽标：说明这条是盲审补抓的，不是主审结论 */
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

/* 对话里点「查看这条风险」后的短暂高亮：告诉用户跳到的是哪一条 */
.risk.flash-risk {
  border-left-color: var(--pri);
  box-shadow: 0 0 0 2px var(--pri-soft);
  transition: box-shadow 0.2s ease;
}
</style>
