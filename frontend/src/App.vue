<!--
  应用外壳：顶栏导航（上传审查 / 任务队列）+ 三视图切换。
  用 state 切换而非 vue-router：只有 3 个页面，引入路由依赖不值当；
  支持 ?view=upload|queue|task&thread=xxx 直达（演示/截图/书签用，非路由）。
  C 方向换皮：顶栏白底靛蓝品牌标，导航"当前页"用靛蓝浅底胶囊。
-->
<script setup lang="ts">
import { ref } from 'vue'
import UploadView from './views/UploadView.vue'
import QueueView from './views/QueueView.vue'
import TaskView from './views/TaskView.vue'

type View = 'upload' | 'queue' | 'task'

// 直达参数：?view=queue / ?view=task&thread=<id>；非法或缺 thread 时回落默认
const params = new URLSearchParams(location.search)
const viewParam = params.get('view')
const threadParam = params.get('thread') ?? ''
const view = ref<View>(viewParam === 'queue' || viewParam === 'task' || viewParam === 'upload' ? viewParam : 'upload')
const activeThread = ref(threadParam) // 任务详情视图当前展示的任务号
if (view.value === 'task' && !activeThread.value) view.value = 'queue'

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
        <!-- 品牌标：文档 + 核对勾（方向 C：靛蓝线性小标，无图标库依赖） -->
        <svg class="emblem" viewBox="0 0 32 32" width="30" height="30" aria-hidden="true">
          <path d="M9 3.8h9.6l6 6V26a2.2 2.2 0 0 1-2.2 2.2H9A2.2 2.2 0 0 1 6.8 26V6A2.2 2.2 0 0 1 9 3.8z"
                fill="#fff" stroke="#3456d1" stroke-width="1.5" />
          <path d="M18.6 3.8v4.6a2 2 0 0 0 2 2h4.6" fill="none" stroke="#3456d1" stroke-width="1.3" />
          <line x1="11" y1="13.4" x2="21" y2="13.4" stroke="#cdd6e4" stroke-width="1.7" stroke-linecap="round" />
          <line x1="11" y1="17.2" x2="21" y2="17.2" stroke="#cdd6e4" stroke-width="1.7" stroke-linecap="round" />
          <line x1="11" y1="21" x2="16.5" y2="21" stroke="#cdd6e4" stroke-width="1.7" stroke-linecap="round" />
          <circle cx="24.4" cy="25" r="5.1" fill="#3456d1" />
          <path d="M22 25.2l1.7 1.7 2.9-3.1" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <span class="brand-text">
          <b>合同审核工作台</b>
          <small>ContractAgent · 规则 + 政策语料 + 人工审批</small>
        </span>
      </div>
      <nav>
        <button :class="{ on: view === 'upload' }" @click="go('upload')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 16V5m0 0l-4 4m4-4l4 4"></path><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"></path></svg>
          上传审查
        </button>
        <button :class="{ on: view === 'queue' }" @click="go('queue')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"></path></svg>
          任务队列
        </button>
      </nav>
    </header>

    <!-- 主区：按 view 渲染页面；open 事件统一进任务详情 -->
    <main>
      <UploadView v-if="view === 'upload'" @open="openTask" @go-queue="go('queue')" />
      <QueueView v-else-if="view === 'queue'" @open="openTask" />
      <TaskView v-else-if="view === 'task'" :thread-id="activeThread" @back="go('queue')" />
    </main>

    <!-- 页脚：演示合规免责 -->
    <footer>
      <span class="muted">演示环境使用合成合同与合成政策语料 · 系统产出为初审参考，不构成法律意见</span>
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
  padding: 10px clamp(18px, 5vw, 64px);
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--line);
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}

.brand {
  display: flex;
  align-items: center;
  gap: 13px;
  cursor: pointer;
}

.emblem {
  filter: drop-shadow(0 1px 1px rgba(16, 24, 40, 0.12));
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}

.brand-text b {
  font-size: 16.5px;
  letter-spacing: 0.02em;
  font-weight: 700;
}

.brand-text small {
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 0.03em;
  margin-top: 1px;
}

nav {
  display: flex;
  gap: 10px;
  align-items: center;
}

nav button {
  border: 0;
  background: transparent;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 18px;
  color: var(--ink-2);
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0.04em;
  border-radius: 8px;
  transition: background 0.14s ease, color 0.14s ease, box-shadow 0.14s ease;
}

nav button svg {
  width: 15px;
  height: 15px;
}

nav button:hover {
  background: var(--paper-2);
  color: var(--ink);
}

nav button.on {
  /* 当前页 = 靛蓝浅底 + 靛蓝字（仪表盘分段控件感） */
  background: var(--pri-soft);
  color: var(--pri);
  font-weight: 700;
}

main {
  flex: 1;
  position: relative;
  z-index: 1;
  width: min(1200px, 100%);
  margin: 0 auto;
  padding: 28px clamp(18px, 5vw, 64px) 56px;
}

footer {
  position: relative;
  z-index: 1;
  text-align: center;
  font-size: 12px;
  padding: 14px 12px 22px;
  border-top: 1px solid var(--line);
}

footer::before {
  content: none;
}
</style>
