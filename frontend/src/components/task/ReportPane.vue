<!--
  报告面板（详情页左栏，已完成态）：文件头与导出、审批留痕、空白模板结论、
  双审复核结论、引用核对、风险清单、政策引用。展示为主，动作（导出/定位/问助手）转给详情页。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { citationCheckClass, citationCheckText, policyReflow, prettyField } from '../../labels'
import ReviewBlock from './ReviewBlock.vue'
import RiskCard from './RiskCard.vue'
import type { ReviewSection, RiskCardItem, TaskDetail } from '../../types'

const props = defineProps<{
  detail: TaskDetail
  risks: RiskCardItem[]
  templateNotice: RiskCardItem | null
  review: ReviewSection | null
}>()

const emit = defineEmits<{
  export: []
  locate: [clause: string, quote: string]
  ask: [risk: RiskCardItem]
}>()

/** 引用核对段：报告没有这一段（老报告）时不显示。 */
const citationCheck = computed(() => props.detail.report?.citation_checks ?? null)

/** 站不住的引用：编号不存在、正文读不到、引错政策、阈值在原文找不到——列出来请人核对。 */
const citationProblems = computed(() =>
  (citationCheck.value?.items ?? []).filter((item) => !item.ok),
)

/** 只带提示的引用：政策自己写明"这类合同不适用"之类，不影响结论，单独一行免得混进问题里。 */
const citationNotes = computed(() =>
  (citationCheck.value?.items ?? []).filter((item) => item.ok && item.notes.length),
)

/** 结论徽章：没有引用可核对时说"无引用"，别给"通过"的错觉。 */
const citationBadge = computed(() => {
  const summary = citationCheck.value?.summary
  if (!summary?.cited) return '无引用'
  return citationProblems.value.length ? '需核对' : '通过'
})

/** 政策行解析：已知标签行（文件编号/版本/生效日期/归口部门/适用范围/第X条）
 * 拆出标签与内容，标签用强调色、内容保持正文色——关键信息一眼可分。 */
function policyRowParts(line: string): { lbl: string | null; val: string } {
  const m = line.match(
    /^(文件编号|版本|生效日期|归口部门|适用范围|第[一二三四五六七八九十百\d]+条)[：:](.*)$/,
  )
  return m ? { lbl: `${m[1]}：`, val: m[2] } : { lbl: null, val: line }
}

/** 政策行样式归类：首行=细则标题；元信息/归口适用范围/条文头各有色调。 */
function policyRowClass(index: number, line: string): string {
  if (index === 0) return 'pl-title'
  if (/^(文件编号|版本|生效日期)[：:]/.test(line)) return 'pl-meta'
  if (/^(归口部门|适用范围)[：:]/.test(line)) return 'pl-scope'
  if (/^第[一二三四五六七八九十百\d]+条/.test(line)) return 'pl-article'
  return 'pl-body'
}
</script>

<template>
  <!-- 布局类（.rep-main）由详情页传入 -->
  <div>
    <!-- 报告头：文件名 + 元信息 + 导出 -->
    <div class="card pad head">
      <div class="h-main">
        <p class="file serif">{{ detail.source }}</p>
        <p class="meta muted mono-num">{{ detail.thread_id }}</p>
      </div>
      <button class="btn btn-ghost" @click="emit('export')">导出 JSON</button>
    </div>

    <!-- 审批留痕：done 报告回显最近一次审批动作与意见 -->
    <div v-if="detail.report?.approval" class="card pad appr">
      <div class="appr-head">
        <span class="muted">审批记录</span>
        <span class="chip" :class="detail.report.approval.action">
          {{
            detail.report.approval.action === 'approved'
              ? '放行'
              : detail.report.approval.action === 'rejected'
                ? '打回'
                : '编辑重审'
          }}
        </span>
      </div>
      <p v-if="detail.report.approval.reviewer_note" class="note">
        {{ detail.report.approval.reviewer_note }}
      </p>
      <p v-else class="note none">（未填写审批意见）</p>
    </div>

    <!-- 结论条：疑似空白模板单独成结论，不混进下方风险清单 -->
    <div v-if="templateNotice" class="card pad tpl-notice">
      <p class="tpl-title serif">结论：疑似空白模板，未填写内容较多</p>
      <p v-if="templateNotice.evidence" class="tpl-ev">
        占位示例：「{{ templateNotice.evidence }}」
      </p>
      <p class="tpl-sug">{{ prettyField(templateNotice.suggestion ?? '') }}</p>
    </div>

    <!-- 双审复核段：盲审独立结论与主审 diff 的结果 -->
    <ReviewBlock :review="review" @locate="(clause, quote) => emit('locate', clause, quote)" />

    <!-- 风险清单：空=自动放行提示，非空逐条展示 -->
    <template v-if="risks.length">
      <h4>风险清单</h4>
      <RiskCard
        v-for="(r, i) in risks"
        :key="i"
        :risk="r"
        @locate="(clause, quote) => emit('locate', clause, quote)"
        @ask="(risk) => emit('ask', risk)"
      />
    </template>
    <template v-else-if="!templateNotice">
      <p class="none ok-text serif">未发现风险 · 自动放行</p>
    </template>

    <!-- 引用核对：编号/正文/对应政策/阈值逐条核对——结论只写在后端报告里时用户看不到 -->
    <template v-if="citationCheck">
      <h4>引用核对</h4>
      <div class="card pad ck" :class="citationCheckClass(citationCheck.summary)">
        <p class="ck-head">
          <span class="ck-badge">{{ citationBadge }}</span>
          <span class="ck-text">{{ citationCheckText(citationCheck.summary) }}</span>
        </p>
        <ul v-if="citationProblems.length" class="ck-list">
          <li v-for="(item, i) in citationProblems" :key="`bad-${i}`">
            <span class="ck-ref mono-num">{{ item.policy_ref }}</span>
            <span class="ck-label">{{ item.label }}</span>
            <span class="ck-msg">{{ item.issues.join('；') }}</span>
          </li>
        </ul>
        <ul v-if="citationNotes.length" class="ck-list">
          <li v-for="(item, i) in citationNotes" :key="`note-${i}`">
            <span class="ck-ref mono-num">{{ item.policy_ref }}</span>
            <span class="ck-label">{{ item.label }}</span>
            <span class="ck-msg">{{ item.notes.join('；') }}</span>
          </li>
        </ul>
      </div>
    </template>

    <!-- 政策引用：policy_ref + 相似度 + 制度原文片段 -->
    <template v-if="detail.report?.policy_hits?.length">
      <h4>政策引用</h4>
      <!-- 语料版本：答复"依据出自哪里"的语料层面，悬浮可看各份政策的版本号 -->
      <p
        v-if="detail.report?.policy_library"
        class="lib-meta muted"
        :title="detail.report.policy_library.revisions.join('、')"
      >
        政策库版本 <span class="mono-num">{{ detail.report.policy_library.version }}</span> ·
        {{ detail.report.policy_library.files }} 份 / {{ detail.report.policy_library.units }} 条
      </p>
      <div v-for="(h, i) in detail.report.policy_hits" :key="i" class="card pad hit">
        <!-- 逐行排版：编号 / 相似度 / 标题 / 元信息 / 条文各占一行 -->
        <p class="pl pl-ref">
          <span class="mono-num ref">{{ h.policy_ref }}</span>
        </p>
        <p v-if="h.score != null" class="pl pl-score muted">
          相似度 {{ Number(h.score).toFixed(3) }}
        </p>
        <template v-if="policyReflow(h.snippet ?? '').length">
          <p
            v-for="(ln, li) in policyReflow(h.snippet ?? '')"
            :key="li"
            class="pl"
            :class="policyRowClass(li, ln)"
          >
            <template v-if="policyRowParts(ln).lbl">
              <span class="lbl">{{ policyRowParts(ln).lbl }}</span
              >{{ policyRowParts(ln).val }}
            </template>
            <template v-else>{{ ln }}</template>
          </p>
        </template>
        <!-- 完整条文默认收起，需要核对政策依据时展开看全文 -->
        <details v-if="h.full_text || h.text" class="policy-more">
          <summary class="muted">查看完整条文</summary>
          <div class="policy-full">
            <p
              v-for="(ln, li) in policyReflow(h.full_text ?? h.text ?? '')"
              :key="li"
              :class="policyRowClass(li, ln)"
            >
              <template v-if="policyRowParts(ln).lbl">
                <span class="lbl">{{ policyRowParts(ln).lbl }}</span
                >{{ policyRowParts(ln).val }}
              </template>
              <template v-else>{{ ln }}</template>
            </p>
          </div>
        </details>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 报告页小标题：左侧靛蓝规 */
h4 {
  margin: 22px 0 8px;
  padding-left: 10px;
  border-left: 3px solid var(--pri);
  font-size: 15.5px;
  letter-spacing: 0.02em;
  line-height: 1.4;
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

/* 审批留痕：浅底块，与风险卡区分开 */
.appr {
  margin-top: 12px;
  background: var(--card-2);
  border-radius: 8px;
  padding: 12px 16px;
  font-size: 14.5px;
}

.appr-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

/* 标签不随全局 .muted 走浅灰，在卡片内加深加粗保证可读 */
.appr-head .muted {
  color: var(--ink);
  font-size: 14px;
  font-weight: 600;
}

/* 动作徽章：放行绿 / 打回红 / 编辑重审靛蓝（与闸口操作语义一致） */
.appr .chip {
  padding: 2px 12px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
}

.appr .chip.approved {
  color: var(--ok);
  background: var(--ok-soft);
}

.appr .chip.rejected {
  color: var(--seal-deep);
  background: var(--seal-soft);
}

.appr .chip.edited {
  color: var(--pri-deep);
  background: var(--pri-soft);
}

.appr .note {
  margin: 0;
  color: var(--ink);
  font-size: 14.5px;
  white-space: pre-wrap; /* 保留打回原因里的换行，条目式意见按行展示 */
  word-break: break-word;
  line-height: 1.7;
}

.appr .note.none {
  color: var(--ink-2);
}

/* 结论条：疑似空白模板 = 待确认（琥珀语义色面板，区别于风险清单卡片） */
.tpl-notice {
  margin-top: 12px;
  border-left: 4px solid var(--warn);
  background: var(--warn-soft);
  border-radius: 8px;
}

.tpl-title {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.03em;
  color: var(--warn);
}

.tpl-ev {
  margin: 0 0 6px;
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--ink-2);
}

.tpl-sug {
  margin: 0;
  font-size: 13.5px;
  color: var(--ink-2);
}

/* 空清单：自动放行 */
.none {
  text-align: center;
  padding: 26px 0;
}

.ok-text {
  color: var(--ok);
  letter-spacing: 0.1em;
}

.hit {
  margin: 8px 0;
}

/* 引用核对：整块色调跟结论走（全通过=绿、有硬问题=红），一眼看出要不要人工看 */
.ck {
  margin: 8px 0;
  border-left: 4px solid var(--muted);
}

.ck-ok {
  border-left-color: var(--ok);
  background: var(--ok-soft);
}

.ck-warn {
  border-left-color: var(--seal-deep);
  background: var(--seal-soft);
}

.ck-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
}

.ck-badge {
  flex: none;
  padding: 2px 12px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
  color: var(--ink-2);
  background: var(--card-2);
}

.ck-ok .ck-badge {
  color: var(--ok);
  background: #fff;
}

.ck-warn .ck-badge {
  color: var(--seal-deep);
  background: #fff;
}

.ck-text {
  font-size: 14px;
  line-height: 1.7;
  color: var(--ink);
}

/* 条目行：编号与风险名在前、说明在后，问题与提示共用一套排版 */
.ck-list {
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.ck-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  padding: 4px 0;
  font-size: 13.5px;
  color: var(--ink-2);
}

.ck-ref {
  font-size: 12.5px;
  color: var(--pri);
}

.ck-label {
  color: var(--ink);
  font-weight: 600;
}

/* 政策库版本行：贴"政策引用"标题的元信息，悬浮显示各份政策的版本号 */
.lib-meta {
  margin: 4px 0 10px;
  font-size: 12.5px;
  letter-spacing: 0.02em;
}

/* 政策卡逐行排版：标题=靛蓝、相似度=琥珀、元信息=灰蓝、适用范围标签=靛蓝、条文头=墨色加粗 */
.pl {
  margin: 0;
  font-size: 14.5px;
  line-height: 1.9;
  font-weight: 500;
  color: var(--ink-2);
}

.pl-ref {
  margin-bottom: 2px;
}

.hit .ref {
  font-size: 13px;
  padding: 2px 11px;
}

.pl-score {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 700;
  color: var(--warn);
}

.pl-title {
  color: var(--pri);
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.pl-meta {
  font-size: 13px;
  color: var(--muted);
}

.pl-meta .lbl {
  color: var(--warn);
  font-weight: 700;
  margin-right: 0.2em;
}

.pl-scope {
  font-size: 14.5px;
  color: var(--ink);
}

.pl-scope .lbl {
  color: var(--pri);
  font-weight: 700;
  margin-right: 0.2em;
}

.pl-article {
  color: var(--ink);
  font-weight: 700;
}

.pl-article .lbl {
  color: var(--pri);
}

/* 政策完整条文：默认收起的展开块，浅底便于核对原文 */
.policy-more {
  margin-top: 8px;
  font-size: 13px;
}

.policy-more summary {
  cursor: pointer;
  user-select: none;
  letter-spacing: 0.04em;
}

.policy-full {
  margin: 8px 0 0;
  padding: 10px 12px;
  background: var(--card-2);
  border: 1px solid var(--line);
  border-radius: 8px;
  font-size: 14px;
  line-height: 1.9;
  color: var(--ink-2);
}

/* 每条逻辑行按容器整宽自然换行，避免源文件手工折行造成右半空白 */
.policy-full p {
  margin: 0 0 0.35em;
  overflow-wrap: break-word;
}
</style>
