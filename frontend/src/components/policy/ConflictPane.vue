<!--
  冲突面板：只列可核对的矛盾（编号撞号 / 元信息缺失 / 同主题阈值数字不一致）。
  语义上"像不像、要不要合并"仍由人判断，这里不给结论。
-->
<script setup lang="ts">
import type { PolicyConflict } from '../../types'

defineProps<{
  conflicts: PolicyConflict[]
}>()
</script>

<template>
  <section class="card pad">
    <div class="head">
      <span class="title serif">冲突提示</span>
      <span class="muted note">可核对的三类：编号撞号、元信息缺失、同主题阈值数字不一致</span>
    </div>
    <ul v-if="conflicts.length" class="list">
      <li v-for="(item, index) in conflicts" :key="index" class="row">
        <span class="stamp stamp-warn">{{ item.kind }}</span>
        <span class="detail">{{ item.detail }}</span>
      </li>
    </ul>
    <p v-else class="muted empty">未发现可核对的冲突（口径上是否冲突仍需人工读一遍）</p>
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
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 8px 0;
  border-top: 1px solid var(--line);
}

.detail {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--ink-2);
}

.empty {
  margin: 4px 0 0;
  font-size: 12.5px;
}
</style>
