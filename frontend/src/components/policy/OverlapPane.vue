<!--
  重叠分级面板：新政策逐条 vs 现有政策库（余弦分）。
  高分在前；低重叠默认折起来——它们只是"查过但不相近"的记录，铺开会把真正要看的压下去。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { OVERLAP_CLASS, OVERLAP_TEXT } from '../../labels'
import type { PolicyOverlapItem } from '../../types'

const props = defineProps<{
  overlaps: PolicyOverlapItem[]
}>()

const showLow = ref(false)

const order = { high: 0, medium: 1, low: 2 }
// 分级优先、同级按最高分排序：要人工判断的是最像的那几条
const sorted = computed(() =>
  [...props.overlaps].sort((a, b) => order[a.level] - order[b.level] || topScore(b) - topScore(a)),
)
const shown = computed(() => sorted.value.filter((item) => showLow.value || item.level !== 'low'))
const lowCount = computed(() => sorted.value.filter((item) => item.level === 'low').length)

/** 一条重叠记录的最高余弦分（没命中记 0）。 */
function topScore(item: PolicyOverlapItem): number {
  return item.hits.length ? item.hits[0].score : 0
}
</script>

<template>
  <section class="card pad">
    <div class="head">
      <span class="title serif">重叠分级</span>
      <span class="muted note">逐条比现有政策库；分越高越像，须人工确认是补充还是替代</span>
    </div>

    <ul class="list">
      <li v-for="item in shown" :key="item.article" class="row">
        <div class="line">
          <span class="stamp" :class="OVERLAP_CLASS[item.level]">{{
            OVERLAP_TEXT[item.level]
          }}</span>
          <span class="article">{{ item.article }}</span>
        </div>
        <div class="hits">
          <div v-for="hit in item.hits" :key="hit.source + hit.score" class="hit">
            <span class="ref mono-num">{{ hit.policy_ref }}</span>
            <span class="score mono-num">{{ hit.score.toFixed(3) }}</span>
            <span class="snippet muted">{{ hit.text_head }}</span>
          </div>
          <p v-if="!item.hits.length" class="muted empty-hit">无命中</p>
        </div>
      </li>
    </ul>

    <p v-if="!shown.length" class="muted empty">未检索到相近的既有条文</p>
    <button v-if="lowCount" type="button" class="btn btn-plain more" @click="showLow = !showLow">
      {{ showLow ? `收起低重叠（${lowCount}）` : `展开低重叠（${lowCount}）` }}
    </button>
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

.list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.row {
  padding: 9px 0;
  border-top: 1px solid var(--line);
}

.line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.article {
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: 0.01em;
}

.hits {
  margin-top: 6px;
  padding-left: 2px;
}

.hit {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 2px 0;
  font-size: 12.5px;
}

.score {
  color: var(--pri);
  font-weight: 600;
}

.snippet {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty-hit,
.empty {
  font-size: 12.5px;
}

.empty {
  margin: 6px 0 0;
}

.more {
  margin-top: 8px;
  font-size: 12.5px;
}
</style>
