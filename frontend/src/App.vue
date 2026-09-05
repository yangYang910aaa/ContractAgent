<!--
  应用外壳：顶栏导航（上传审查 / 任务队列）+ 三视图切换。
  用 state 切换而非 vue-router：只有 3 个页面，引入路由依赖不值当；
-->
<script setup lang="ts">
import { ref } from 'vue'
import UploadView from './views/UploadView.vue'
import QueueView from './views/QueueView.vue'
import TaskView from './views/TaskView.vue'

type View = 'upload' | 'queue' | 'task'

const view = ref<View>('upload')
const activeThread = ref('') // 任务详情视图当前展示的任务号

/** 从上传结果或队列行跳进任务详情。 */
function openTask(threadId: string) {
  activeThread.value = threadId
  view.value = 'task'
}

/** 顶栏导航切换（详情页返回时回队列，任务列表会自动轮询刷新）。 */
function go(viewName: View) {
  view.value = viewName
}
</script>

<template>
  <div class="shell">
    <!-- 顶栏：品牌（点回上传页）+ 导航 -->
    <header class="top">
      <div class="brand" @click="go('upload')">
        <span class="seal-mark serif">审</span>
        <span class="brand-text">
          <b class="serif">合同审核工作台</b>
          <small>ContractAgent · 规则 + 政策 + 人工审批</small>
        </span>
      </div>
      <nav>
        <button :class="{ on: view === 'upload' }" @click="go('upload')">上传审查</button>
        <button :class="{ on: view === 'queue' }" @click="go('queue')">任务队列</button>
      </nav>
    </header>

    <!-- 主区：按 view 渲染页面；open 事件统一进任务详情 -->
    <main>
      <UploadView v-if="view === 'upload'" @open="openTask" @go-queue="go('queue')" />
      <QueueView v-else-if="view === 'queue'" @open="openTask" />
      <TaskView v-else-if="view === 'task'" :thread-id="activeThread" @back="go('queue')" />
    </main>

    <!-- 页脚：演示合规免责 -->
    <footer class="muted">
      演示环境使用合成合同与合成政策语料；系统产出为初审参考，不构成法律意见。
    </footer>
  </div>
</template>

<style scoped>
.shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.top {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px clamp(18px, 5vw, 64px);
  background: rgba(246, 241, 231, 0.92);
  backdrop-filter: blur(6px);
  border-bottom: 1px solid var(--line);
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  cursor: pointer;
}

.seal-mark {
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  border: 2px solid var(--seal);
  color: var(--seal);
  border-radius: 4px;
  font-size: 22px;
  box-shadow: inset 0 0 0 1px rgba(178, 58, 58, 0.25);
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}

.brand-text b {
  font-size: 18px;
  letter-spacing: 0.1em;
}

.brand-text small {
  color: var(--muted);
  font-size: 11.5px;
  letter-spacing: 0.04em;
}

nav {
  display: flex;
  gap: 6px;
}

nav button {
  border: 0;
  background: transparent;
  padding: 8px 16px;
  border-bottom: 2px solid transparent;
  color: var(--ink-2);
  font-weight: 600;
}

nav button.on {
  color: var(--seal);
  border-bottom-color: var(--seal);
}

main {
  flex: 1;
  width: min(1120px, 100%);
  margin: 0 auto;
  padding: 34px clamp(18px, 5vw, 64px) 60px;
}

footer {
  text-align: center;
  font-size: 12px;
  padding: 18px 12px 26px;
  border-top: 1px solid var(--line);
}
</style>
