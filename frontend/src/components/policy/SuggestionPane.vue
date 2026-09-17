<!--
  模型起草的配套建议：这条政策该挂哪些风险类型、建议造什么验证样本、建议的检索标准答案。
  都是草稿——风险类型编码不在册的单列红字等人定，样本与标准答案照着改就行。
-->
<script setup lang="ts">
import { gradeDisplay, kindLabel } from '../../labels'
import type { AiDraftSuggestions } from '../../types'

defineProps<{ suggestions: AiDraftSuggestions }>()
</script>

<template>
  <section class="card pad">
    <div class="head">
      <span class="title serif">配套建议</span>
      <span class="muted note">起草时一并给出的落地下手处：先定风险类型，再造样本与检索标准答案</span>
    </div>

    <p v-if="suggestions.notes.length" class="warn">
      模型给的取值被归位：{{ suggestions.notes.join('；') }}
    </p>

    <div class="group">
      <span class="tag">建议挂的风险类型</span>
      <ul class="list">
        <li v-for="item in suggestions.risk_types" :key="item.risk_type + item.label" class="row">
          <p class="r-head">
            <code class="chip">{{ item.risk_type }}</code>
            <span class="r-label">{{ item.label }}</span>
            <span v-if="!item.known" class="err">库里没有这个编码，需人工新增</span>
          </p>
          <p class="r-why">{{ item.why }}</p>
          <p v-if="item.evidence_hint" class="muted hint">怎么认：{{ item.evidence_hint }}</p>
        </li>
      </ul>
      <p v-if="!suggestions.risk_types.length" class="muted empty">模型没给风险类型建议</p>
    </div>

    <div class="group">
      <span class="tag">建议造的验证样本</span>
      <ul class="list">
        <li v-for="(item, index) in suggestions.samples" :key="index" class="row">
          <p class="r-head">
            <span class="kind">{{ kindLabel(item.kind) }}</span>
            <span class="grade">{{
              item.expected_grade ? gradeDisplay(item.expected_grade) : '评级待定'
            }}</span>
          </p>
          <p v-if="item.goal" class="r-why">{{ item.goal }}</p>
          <p class="hint">注入缺陷：{{ item.defect }}</p>
        </li>
      </ul>
      <p v-if="!suggestions.samples.length" class="muted empty">模型没给样本建议</p>
    </div>

    <div class="group">
      <span class="tag">检索标准答案建议</span>
      <ul class="list">
        <li v-for="(item, index) in suggestions.retrievals" :key="index" class="row">
          <p class="r-head">
            <span class="r-label">{{ item.query }}</span>
            <code class="chip">{{ item.policy_ref }}</code>
          </p>
          <p v-if="item.expect" class="muted hint">期望命中：{{ item.expect }}</p>
        </li>
      </ul>
      <p v-if="!suggestions.retrievals.length" class="muted empty">模型没给检索标准答案建议</p>
    </div>
  </section>
</template>

<style scoped>
.group {
  margin-top: 14px;
}

.list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
}

/* 每类建议一行：标题行（编码 + 名称/徽标）+ 说明 */
.row {
  margin-top: 10px;
  padding-left: 10px;
  border-left: 2px solid var(--line, #e5e7eb);
}

.row:first-child {
  margin-top: 0;
}

.r-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
}

.r-label {
  font-weight: 600;
}

.r-why {
  margin: 4px 0 0;
  color: var(--ink-2, #374151);
}

.hint {
  margin: 4px 0 0;
  font-size: 13px;
}

.kind,
.grade {
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: var(--chip, #f3f4f6);
}

.empty {
  margin: 6px 0 0;
  font-size: 13px;
}
</style>
