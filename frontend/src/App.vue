<!--
  应用外壳：顶栏导航（上传审查 / 任务队列）+ 三视图切换。
  用 state 切换而非 vue-router：只有 3 个页面，引入路由依赖不值当；
  支持 ?view=upload|queue|task&thread=xxx 直达（演示/截图/书签用，非路由）。
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
        <!-- 品牌标：文书 + 签章小图标（方向 A：换掉左上大"审"字） -->
        <svg class="emblem" viewBox="0 0 32 32" width="30" height="30" aria-hidden="true">
          <path d="M9 3.8h9.6l6 6V26a2.2 2.2 0 0 1-2.2 2.2H9A2.2 2.2 0 0 1 6.8 26V6A2.2 2.2 0 0 1 9 3.8z"
                fill="#fcf8ec" stroke="#a5312c" stroke-width="1.5" />
          <path d="M18.6 3.8v4.6a2 2 0 0 0 2 2h4.6" fill="none" stroke="#a5312c" stroke-width="1.3" />
          <line x1="11" y1="13.4" x2="21" y2="13.4" stroke="#8d826a" stroke-width="1.7" stroke-linecap="round" />
          <line x1="11" y1="17.2" x2="21" y2="17.2" stroke="#8d826a" stroke-width="1.7" stroke-linecap="round" />
          <line x1="11" y1="21" x2="17.5" y2="21" stroke="#8d826a" stroke-width="1.7" stroke-linecap="round" />
          <circle cx="24.4" cy="25" r="3.4" fill="#a5312c" />
          <circle cx="24.4" cy="25" r="5.2" fill="none" stroke="#a5312c" stroke-width="1.1" opacity="0.85" />
        </svg>
        <span class="brand-text">
          <b class="serif">合同审核工作台</b>
          <small>ContractAgent · 规则 + 政策语料 + 人工审批</small>
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
  /* 红头式页眉：半透纸色 + 底部朱线由实渐虚（仿公函抬头） */
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px clamp(18px, 5vw, 64px);
  background: rgba(243, 234, 217, 0.9);
  backdrop-filter: blur(6px);
  box-shadow: 0 1px 0 rgba(90, 76, 45, 0.12);
}

.top::after {
  content: "";
  position: absolute;
  left: clamp(18px, 5vw, 64px);
  right: clamp(18px, 5vw, 64px);
  bottom: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--seal) 0 26%, rgba(165, 49, 44, 0.5) 40%, rgba(165, 49, 44, 0.08) 74%, transparent);
}

.brand {
  display: flex;
  align-items: center;
  gap: 13px;
  cursor: pointer;
}

.emblem {
  filter: drop-shadow(0 1px 1px rgba(127, 33, 28, 0.18));
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}

.brand-text b {
  font-size: 19px;
  letter-spacing: 0.3em;
  font-weight: 700;
}

.brand-text small {
  color: var(--muted);
  font-size: 10.5px;
  letter-spacing: 0.14em;
  margin-top: 2px;
}

nav {
  display: flex;
  gap: 10px;
  align-items: center;
}

nav button {
  border: 0;
  background: transparent;
  padding: 7px 20px;
  color: var(--ink-2);
  font-weight: 600;
  font-size: 14.5px;
  letter-spacing: 0.12em;
  border-radius: 3px;
  transition: background 0.14s ease, color 0.14s ease, box-shadow 0.14s ease;
}

nav button:hover {
  background: var(--paper-2);
}

nav button.on {
  /* 当前页 = 朱底印钮（白字楷体），纸面上唯一的高饱和主钮 */
  font-family: var(--kai);
  background: linear-gradient(#b5403a, #a5312c);
  color: #fdf3e3;
  box-shadow:
    0 2px 0 var(--seal-deep),
    inset 0 1px 0 rgba(255, 255, 255, 0.18);
}

main {
  flex: 1;
  position: relative;
  z-index: 1;
  width: min(1120px, 100%);
  margin: 0 auto;
  padding: 34px clamp(18px, 5vw, 64px) 60px;
}

footer {
  position: relative;
  z-index: 1;
  text-align: center;
  font-size: 12.5px;
  padding: 18px 12px 26px;
  letter-spacing: 0.08em;
}

footer::before {
  content: "";
  display: block;
  width: min(420px, 80%);
  height: 1px;
  margin: 0 auto 14px;
  background: linear-gradient(90deg, transparent, var(--line-strong), transparent);
}
</style>
