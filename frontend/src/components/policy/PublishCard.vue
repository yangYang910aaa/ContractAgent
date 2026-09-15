<!--
  入库卡片：本页唯一会改真库的动作，所以分两步——先算计划看改动，再确认执行。
  撞号与文件名不合法由后端硬阻止；缺元信息要显式放行（缺项的政策入库后，
  报告里"依据哪一版政策"就无从标注）。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { planPolicyPublish, publishPolicyDraft } from '../../api'
import { missingMetaText } from '../../labels'
import type { DraftApplied, PublishPlan, PublishResult } from '../../types'

const props = defineProps<{
  draftId: string
  suggestedFile: string // 后端给的建议文件名
  draftText: string // 规范化草稿（不展开编辑时就用它入库）
  missing: string[] // 缺哪些元信息
  applied: DraftApplied | null // 这份草稿的入库记录
}>()

const emit = defineEmits<{ applied: [result: PublishResult] }>()

const fileName = ref(props.suggestedFile)
const editing = ref(false)
const content = ref(props.draftText)
const allowMissing = ref(false)
const plan = ref<PublishPlan | null>(null)
const result = ref<PublishResult | null>(null)
const busy = ref(false)
const error = ref('')

// 重新起稿后换了一份草稿 → 表单回到新草稿的默认值，别把上一份的文件名与正文带过来
watch(
  () => props.draftId,
  () => {
    fileName.value = props.suggestedFile
    content.value = props.draftText
    editing.value = false
    allowMissing.value = false
    plan.value = null
    result.value = null
    error.value = ''
  },
)

const canPublish = computed(() => Boolean(plan.value) && plan.value!.blockers.length === 0)
// 缺项且没勾放行 → 后端一定会拦，这里提前把按钮压住并说明原因
const needAllow = computed(() => (plan.value?.missing.length ?? 0) > 0 && !allowMissing.value)

/** 预览：让后端算一遍要写/要删几条、版本号怎么变，看完再决定。 */
async function preview() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const resp = await planPolicyPublish(props.draftId, {
      file_name: fileName.value.trim(),
      content: editing.value ? content.value : '',
    })
    plan.value = resp.plan
  } catch (err) {
    plan.value = null
    error.value = err instanceof Error ? err.message : '算入库计划失败'
  } finally {
    busy.value = false
  }
}

/** 确认入库：落盘 + 同步进向量库；成功后把回执交给上层刷新政策库现状。 */
async function confirmPublish() {
  if (busy.value || !canPublish.value || needAllow.value) return
  busy.value = true
  error.value = ''
  try {
    const resp = await publishPolicyDraft(props.draftId, {
      file_name: fileName.value.trim(),
      content: editing.value ? content.value : '',
      allow_missing_meta: allowMissing.value,
    })
    result.value = resp
    // 计划框是"执行前"的快照：入库后它就过期了（文件已存在、条数也可能不同），撤掉只看回执
    plan.value = null
    emit('applied', resp)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '入库失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="card pad publish">
    <div class="head">
      <span class="title serif">入库</span>
      <span class="muted note">会真的写进政策库（语料文件 + 向量库），所以先看计划再执行</span>
      <span v-if="applied" class="stamp stamp-ok">已入库</span>
    </div>

    <p v-if="applied" class="done muted mono-num">
      已入库 {{ applied.file_name }} · {{ applied.updated ? '更新' : '新增' }} · 写入
      {{ applied.written }} 条 / 删除 {{ applied.removed }} 条 · 版本 {{ applied.version }} （{{ applied.at }}）
    </p>

    <label class="field">
      <span class="label">入库文件名</span>
      <input v-model="fileName" class="input mono-num" type="text" :disabled="busy" />
    </label>
    <p class="hint muted">
      编号从文件名前缀认（`P-XX_短名.md`）；填成已有政策的文件名，就是更新那一份。
    </p>

    <button type="button" class="btn btn-plain toggle" :disabled="busy" @click="editing = !editing">
      {{ editing ? '收起正文编辑' : '展开正文编辑（默认用草稿原文）' }}
    </button>
    <textarea v-if="editing" v-model="content" class="textarea mono-num" rows="12"></textarea>

    <div class="actions">
      <button class="btn btn-ghost" type="button" :disabled="busy" @click="preview">
        {{ busy && !result ? '处理中…' : '预览入库改动' }}
      </button>
      <button
        class="btn btn-primary"
        type="button"
        :disabled="busy || !canPublish || needAllow"
        @click="confirmPublish"
      >
        确认入库
      </button>
      <span class="hint muted">{{ canPublish ? '按上面的文件名写入政策库' : '先预览一次入库改动' }}</span>
    </div>

    <!-- 计划：写几条、删几条、版本号怎么走，都在执行前摊开 -->
    <div v-if="plan" class="plan">
      <p class="plan-line">
        {{ plan.exists ? '更新' : '新增' }} <b>{{ plan.file_name }}</b> · 写入
        <b class="mono-num">{{ plan.write_units }}</b> 条 / 删除
        <b class="mono-num">{{ plan.delete_units }}</b> 条 · 政策库版本
        <span class="mono-num">{{ plan.version }}</span> →
        <span class="mono-num next">{{ plan.next_version }}</span>
      </p>
      <p v-if="plan.delete_sources.length" class="muted small">
        将被清除的来源：{{ plan.delete_sources.join('、') }}
      </p>
      <p v-if="plan.missing.length" class="warn">
        元信息缺项：{{ missingMetaText(plan.missing) }}
        <label class="allow">
          <input v-model="allowMissing" type="checkbox" :disabled="busy" />
          按现状入库
        </label>
      </p>
      <p v-for="item in plan.blockers" :key="item.kind + item.detail" class="err">
        {{ item.kind }}：{{ item.detail }}
      </p>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-if="result" class="ok mono-num">
      已入库 {{ result.file_name }} · 写入 {{ result.written }} 条 / 删除 {{ result.removed }} 条 ·
      核对{{ result.check_ok ? '一致' : '不一致' }} · 版本 {{ result.previous_version }} →
      {{ result.version }}（共 {{ result.units }} 条单元）
    </p>
  </section>
</template>

<style scoped>
.publish {
  /* 改真库的动作：左侧一道靛蓝竖线，跟只读面板区分开 */
  border-left: 3px solid var(--pri);
}

.head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.title {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.note {
  font-size: 12px;
}

.head .stamp {
  margin-left: auto;
}

.done {
  margin: 0 0 10px;
  font-size: 12.5px;
}

.field {
  display: flex;
  align-items: center;
  gap: 10px;
}

.label {
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}

.input,
.textarea {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--ink);
  background: #fff;
}

.input:focus,
.textarea:focus {
  outline: none;
  border-color: var(--pri);
  box-shadow: 0 0 0 3px var(--pri-soft);
}

.textarea {
  margin-top: 8px;
  resize: vertical;
  line-height: 1.7;
}

.hint {
  margin: 6px 0 0;
  font-size: 12px;
}

.toggle {
  margin-top: 10px;
  font-size: 12.5px;
}

.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.plan {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  background: var(--paper-2);
}

.plan-line {
  margin: 0;
  font-size: 13px;
  line-height: 1.8;
}

.plan-line b {
  color: var(--ink);
}

.next {
  color: var(--pri);
  font-weight: 600;
}

.small {
  margin: 4px 0 0;
  font-size: 12px;
}

.warn {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--seal-deep);
}

.allow {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--ink-2);
}

.ok {
  margin: 10px 0 0;
  font-size: 12.5px;
  color: #1c7c54;
}
</style>
