<!--
  模型起草的配套面板：逐条给出语义解释、审查看哪里、哪些情况不该报，
  并把"模型自己引入的数字"顶在最前面——那是要人确认或改掉的地方。
-->
<script setup lang="ts">
import type { AiDraftSection, AiNumberFinding } from '../../types'

defineProps<{
  ai: AiDraftSection
  newNumbers: AiNumberFinding[]
}>()

/** 把一处新引入的数字压成一行（如 "第二条 预付款比例上限：40%"）。 */
function numberLine(item: AiNumberFinding): string {
  const parts = [
    ...item.percent.map((value) => `${value}%`),
    ...item.months.map((value) => `${value} 个月`),
  ]
  return `${item.article}：${parts.join('、')}`
}
</script>

<template>
  <section class="card pad">
    <div class="head">
      <span class="title serif">条文解释与判定要点</span>
      <span class="muted note">模型起草时给出的解释、审查看哪里、哪些情况不该报</span>
    </div>

    <p v-if="newNumbers.length" class="warn">
      模型新引入的数字（需求里没有，需确认或改掉）：
      <span v-for="(item, index) in newNumbers" :key="item.article">
        {{ index ? '；' : '' }}{{ numberLine(item) }}
      </span>
    </p>

    <ul class="list">
      <li v-for="item in ai.articles" :key="item.heading" class="row">
        <p class="a-head">{{ item.heading }}</p>
        <p class="a-exp">{{ item.explanation }}</p>
        <div v-if="item.checkpoints.length" class="group">
          <span class="tag">判定要点</span>
          <ul class="points">
            <li v-for="(point, index) in item.checkpoints" :key="index">{{ point }}</li>
          </ul>
        </div>
        <div v-if="item.guards.length" class="group">
          <span class="tag guard">护栏</span>
          <ul class="points">
            <li v-for="(guard, index) in item.guards" :key="index">{{ guard }}</li>
          </ul>
        </div>
      </li>
    </ul>

    <p v-if="ai.notes.length" class="notes">
      模型标出的待确认项：{{ ai.notes.join('；') }}
    </p>
    <p v-if="ai.brief" class="muted brief">当时提的需求：{{ ai.brief }}</p>
  </section>
</template>

<style scoped>
.head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.title {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.note {
  font-size: 12px;
}

.warn {
  margin: 0 0 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--seal-soft);
  color: var(--seal-deep);
  font-size: 12.5px;
  line-height: 1.7;
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.row {
  padding: 10px 0;
  border-top: 1px solid var(--line);
}

.a-head {
  margin: 0;
  font-size: 13.5px;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.a-exp {
  margin: 4px 0 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--ink-2);
}

.group {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 6px;
}

.tag {
  flex: none;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--pri);
  background: var(--pri-soft);
  border-radius: 6px;
  padding: 1px 7px;
}

.tag.guard {
  color: #1c7c54;
  background: rgba(28, 124, 84, 0.1);
}

.points {
  margin: 0;
  padding-left: 16px;
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--ink-2);
}

.notes {
  margin: 10px 0 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--seal-deep);
}

.brief {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.7;
}
</style>
