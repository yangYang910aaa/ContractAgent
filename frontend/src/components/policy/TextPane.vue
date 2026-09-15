<!--
  文本产物面板：规范化草稿与配套清单共用（都是 markdown 文本 + 一键复制）。
  渲染成元素而不是 innerHTML——起稿产物含用户粘贴内容，不给注入面。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { mdBlocks } from '../../lib/markdown'

const props = defineProps<{
  title: string // 面板标题
  note?: string // 标题右侧的一行说明
  text: string // markdown 正文
  grows?: boolean // 草稿比清单长，单独控制高度上限
}>()

const blocks = computed(() => mdBlocks(props.text))
const copied = ref(false)

/** 一键复制：草稿要拿去改写成正式政策文件，逐字敲一遍不现实。 */
async function copyAll() {
  try {
    await navigator.clipboard.writeText(props.text)
    copied.value = true
    setTimeout(() => (copied.value = false), 1600)
  } catch {
    copied.value = false
  }
}
</script>

<template>
  <section class="card pad pane">
    <div class="head">
      <span class="title serif">{{ title }}</span>
      <span v-if="note" class="muted note">{{ note }}</span>
      <button class="btn btn-plain copy" type="button" @click="copyAll">
        {{ copied ? '已复制' : '复制' }}
      </button>
    </div>
    <div class="body" :class="{ grows }">
      <template v-for="(block, index) in blocks" :key="index">
        <p v-if="block.kind === 'h'" class="b-head" :class="`lv${block.level}`">{{ block.text }}</p>
        <p v-else-if="block.kind === 'li'" class="b-li">{{ block.text }}</p>
        <p v-else class="b-p">{{ block.text }}</p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.pane {
  display: flex;
  flex-direction: column;
}

.head {
  display: flex;
  align-items: baseline;
  gap: 10px;
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

.copy {
  margin-left: auto;
  font-size: 12.5px;
}

.body {
  overflow: auto;
  padding-right: 4px;
  border-top: 1px solid var(--line);
  padding-top: 8px;
  max-height: 340px;
}

.body.grows {
  max-height: 520px;
}

.b-head {
  margin: 10px 0 4px;
  font-weight: 700;
  color: var(--ink);
}

.b-head:first-child {
  margin-top: 2px;
}

.b-head.lv1 {
  font-size: 14.5px;
}

.b-head.lv2 {
  font-size: 13.5px;
  /* 条文头加一道左侧竖线：草稿是按条读的，条与条之间要能一眼分开 */
  border-left: 3px solid var(--pri-soft);
  padding-left: 8px;
}

.b-p {
  margin: 4px 0;
  font-size: 13px;
  line-height: 1.75;
  color: var(--ink-2);
}

.b-li {
  position: relative;
  margin: 4px 0 4px 14px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--ink-2);
}

.b-li::before {
  content: '·';
  position: absolute;
  left: -12px;
  color: var(--muted);
}
</style>
