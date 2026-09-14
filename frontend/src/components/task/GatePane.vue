<!--
  闸口待审面板（详情页左栏）：高风险提示 + 逐条待审风险 + 双审复核结论 + 人工审批表单。
  审批动作与意见属于页面级状态（提交后要清空/复用），所以状态留在详情页，这里只展示与转发。
-->
<script setup lang="ts">
import ReviewBlock from './ReviewBlock.vue'
import RiskCard from './RiskCard.vue'
import type { ReviewSection, RiskCardItem, TaskDetail } from '../../types'

defineProps<{
  detail: TaskDetail
  review: ReviewSection | null
  actionError: string
  acting: boolean
  showEdit: boolean
}>()

// 审批意见与字段补丁：双向绑定到详情页的状态
const note = defineModel<string>('note', { required: true })
const patchText = defineModel<string>('patchText', { required: true })

const emit = defineEmits<{
  approve: []
  reject: []
  edit: []
  toggleEdit: []
  locate: [clause: string, quote: string]
  ask: [risk: RiskCardItem]
}>()
</script>

<template>
  <!-- 布局类（.rep-main）由详情页传入 -->
  <div>
    <!-- 文件头：文件名 + 任务号 + 当前状态 -->
    <div class="card pad head">
      <div class="h-main">
        <p class="file serif">{{ detail.source }}</p>
        <p class="meta muted mono-num">{{ detail.thread_id }}</p>
      </div>
      <span class="stamp stamp-seal">待人工审批</span>
    </div>

    <!-- 警报条：解释为什么停在这里 -->
    <div class="card pad gate-alert">
      <div class="alert-top">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"></path><path d="M12 8v4M12 15.5v.5"></path></svg>
        <h3>高风险，需人工审批</h3>
      </div>
      <p class="muted">
        检测到 {{ detail.gate_payload?.high_risks.length ?? 0 }} 项高风险：请核对原文条款与政策依据后选择放行或打回；
        审批意见与动作会写入最终报告留痕。
      </p>
    </div>

    <!-- 待审风险清单：高风险逐条展示（可点原文定位、可问助手） -->
    <h4>待审风险</h4>
    <RiskCard
      v-for="(r, i) in detail.gate_payload?.high_risks ?? []"
      :key="i"
      :risk="r"
      @locate="(clause, quote) => emit('locate', clause, quote)"
      @ask="(risk) => emit('ask', risk)"
    />

    <!-- 独立复核（双审）：闸口阶段报告还没生成，把复核结论一并展示，
         审批人放行/打回前就能看到盲审发现了什么、哪些只提示 -->
    <ReviewBlock :review="review" @locate="(clause, quote) => emit('locate', clause, quote)" />

    <!-- 人工审批：意见 + 放行/打回/编辑重审 -->
    <div class="card pad approval-card">
      <h3>人工审批</h3>
      <div class="approval">
        <textarea v-model="note" rows="2" placeholder="审批意见（打回必填原因，留痕可追溯）"></textarea>
        <p v-if="actionError" class="err">{{ actionError }}</p>
        <div class="btns">
          <button class="btn btn-primary" :disabled="acting" @click="emit('approve')">放行</button>
          <button class="btn btn-ghost reject" :disabled="acting" @click="emit('reject')">打回</button>
          <button class="btn btn-plain" @click="emit('toggleEdit')">{{ showEdit ? '收起' : '编辑字段重审' }}</button>
        </div>
        <div v-if="showEdit" class="edit-panel">
          <label class="muted">字段补丁（JSON，键=ContractModel 字段名）</label>
          <textarea v-model="patchText" rows="4" class="mono-num"></textarea>
          <button class="btn btn-ghost" :disabled="acting" @click="emit('edit')">提交并重审</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 小标题：左侧红规，提示"这一屏是在拦风险" */
h4 {
  margin: 8px 0 10px;
  padding-left: 10px;
  border-left: 3px solid var(--seal);
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.02em;
  line-height: 1.4;
}

.gate-alert {
  border: 1px solid rgba(224, 69, 79, 0.2);
  border-left: 3px solid var(--seal);
  background: var(--seal-soft);
}

.alert-top {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--seal-deep);
}

.alert-top svg {
  width: 16px;
  height: 16px;
  flex: none;
}

.alert-top h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--ink);
}

.gate-alert p {
  margin: 6px 0 0;
  font-size: 13px;
}

.approval-card h3 {
  margin: 0 0 6px;
  font-size: 14.5px;
  font-weight: 700;
}

.approval-card .approval {
  margin-top: 0;
}

/* 打回 = 拦截动作，用红描边表达否定语义（区别于主操作的靛蓝） */
.btn-ghost.reject {
  color: var(--seal-deep);
  border-color: rgba(224, 69, 79, 0.45);
}

.btn-ghost.reject:hover:not(:disabled) {
  color: var(--seal-deep);
  border-color: var(--seal);
  background: var(--seal-soft);
}

.approval {
  margin-top: 16px;
}

.btns {
  display: flex;
  gap: 10px;
  margin-top: 10px;
  align-items: center;
}

.edit-panel {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 460px;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
}

.head .file {
  font-size: 16px;
  font-weight: 700;
  margin: 0 0 2px;
  word-break: break-all;
}

.h-main {
  min-width: 0;
}

.head p {
  margin: 0;
}

.head .meta {
  font-size: 12px;
  margin-top: 2px;
}
</style>
