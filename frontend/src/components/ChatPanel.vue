<!--
  对话助手面板：详情页右下角浮动入口 → 点开弹出小面板（网页客服那种形态）。
  只做解释与检索——改不了风险清单/评级/审批；回答逐 token 渲染，引用由后端按
  "这一轮工具真正返回过的条目"汇总，模型复述不出不存在的引用。

  Teleport 到 body：浮动入口与面板要贴**视口**右下角，而详情页容器带进场动画
  （.rise 的 animation 一直占着 transform），会把 position: fixed 的包含块锁在那个
  容器上——不 Teleport 就成了"贴在合同区块右下角"，得往下滑才看得见。

  视觉：沿用全站"白底仪表盘 + 靛蓝主色"的语言（法务工位的克制感）——发丝线分区、
  编号与数字走等宽、引用按类别分形态（政策/条款/无法核实）、动效只做"升起 + 光标呼吸"，
  并遵守 prefers-reduced-motion。
-->
<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { askChat, clearChat, fetchChatHistory, streamChat } from '../api'
import type { ChatCitation, ChatUsage } from '../types'

const props = defineProps<{
  threadId: string
  fileName?: string
}>()

const emit = defineEmits<{
  // 合同条款引用点击 → 交给详情页打开原文抽屉并高亮那句（复用现有能力）
  openClause: [clause: string, quote: string]
  // 动作按钮「查看这条风险」→ 详情页滚到对应风险卡
  focusRisk: [clause: string]
}>()

/** 面板里一轮问答的渲染态：过程状态、工具命中、引用与用量都在这里。 */
interface LiveTurn {
  question: string
  answer: string // 逐 token 追加的正文
  status: string // 过程状态行（"正在查政策库…"→"命中政策 P-01"）
  hits: ChatCitation[] // 工具真正取到的条文（点开可看）
  citations: ChatCitation[] // 这一轮的引用（工具返回记录汇总）
  unverified: string[] // 回答提到但查不到出处的政策编号
  usage: ChatUsage | null
  error: string
  running: boolean
  stopped: boolean
  copied: boolean
}

const open = ref(false)
const draft = ref('')
const turns = ref<LiveTurn[]>([])
const busy = ref(false)
const expanded = ref('') // 展开看全文的引用键
const confirmClear = ref(false) // 面板内二次确认（不用原生 confirm，免得弹出浏览器弹窗）
const clearError = ref('')
const bodyEl = ref<HTMLElement | null>(null)
let controller: AbortController | null = null

// 面板空态的建议问题：用户不用猜能问什么，演示也顺
const suggestions = ['这份合同为什么判高风险？', '预付款上限是多少？', '把跟发票有关的条款找出来']

/** 会话号：一份合同固定一个（存本地），刷新页面、后端重启后再打开面板还能回读同一段历史。 */
const sessionId = resolveSession()

function resolveSession(): string {
  const key = `contractAgent.chatSession.${props.threadId}`
  try {
    const saved = localStorage.getItem(key)
    if (saved) return saved
    const created = `s${Math.random().toString(36).slice(2, 10)}`
    localStorage.setItem(key, created)
    return created
  } catch {
    // 分支：隐私模式等禁用本地存储 → 退回默认会话，面板功能不受影响
    return 'default'
  }
}

/** 本次会话累计用量（面板角落的一行小字）。 */
const sessionUsage = computed(() => {
  let calls = 0
  let seconds = 0
  for (const turn of turns.value) {
    calls += turn.usage?.calls ?? 0
    seconds += turn.usage?.seconds ?? 0
  }
  return { calls, seconds: Math.round(seconds * 10) / 10 }
})

function scrollToBottom() {
  const el = bodyEl.value
  if (el) el.scrollTop = el.scrollHeight
}

function openPanel() {
  open.value = true
  void loadHistory()
}

function closePanel() {
  // 关面板不停生成：流还在跑就让它跑完（用户切回报告看别的），结果留在面板里
  open.value = false
}

/** 风险卡上的「问这条」入口：点开面板并直接问，把"点风险 → 问为什么"做成一步。 */
function openWith(question: string) {
  open.value = true
  void loadHistory().then(() => send(question))
}

defineExpose({ openWith })

/** 回读历史：打开面板时拉一次，恢复之前的气泡（不用重新问一遍）。 */
async function loadHistory() {
  try {
    const history = await fetchChatHistory(props.threadId, sessionId)
    turns.value = history.map((item) => ({
      question: item.question,
      answer: item.answer,
      status: '',
      hits: [],
      citations: item.citations ?? [],
      unverified: item.unverified ?? [],
      usage: null,
      error: '',
      running: false,
      stopped: false,
      copied: false,
    }))
  } catch {
    // 分支：历史读不回来（后端重启中/网络抖动）→ 面板照常能问，只是没有旧气泡
    turns.value = []
  }
  await nextTick()
  scrollToBottom()
}

function newTurn(question: string): LiveTurn {
  return {
    question,
    answer: '',
    status: '正在读这份合同的上下文…',
    hits: [],
    citations: [],
    unverified: [],
    usage: null,
    error: '',
    running: true,
    stopped: false,
    copied: false,
  }
}

/** 提问：走逐 token 流；读流失败自动退回普通 POST（降级不另开接口）。 */
async function send(text?: string) {
  const question = (text ?? draft.value).trim()
  if (!question || busy.value) return
  draft.value = ''
  const turn = newTurn(question)
  turns.value.push(turn)
  busy.value = true
  await nextTick()
  scrollToBottom()
  controller = new AbortController()
  try {
    await streamChat(props.threadId, question, sessionId, (event, data) => applyEvent(turn, event, data), controller.signal)
  } catch (err) {
    // 分支：用户点了「停止生成」→ 保留已出的内容，不算失败
    if (controller?.signal.aborted) turn.stopped = true
    else await fallback(turn, question)
  } finally {
    turn.running = false
    turn.status = ''
    busy.value = false
    controller = null
    await nextTick()
    scrollToBottom()
  }
}

/** 读流失败降级：同一个路由改用普通 POST 拿整段回答。 */
async function fallback(turn: LiveTurn, question: string) {
  try {
    const result = await askChat(props.threadId, question, sessionId)
    turn.answer = result.answer
    turn.citations = result.citations ?? []
    turn.unverified = result.unverified ?? []
    turn.usage = result.usage ?? null
  } catch (err) {
    turn.error = err instanceof Error ? err.message : '回答失败，请重试'
  }
}

/** 流事件 → 渲染态：status/tool 更新过程行，token 追加正文，其余在收尾时落到气泡上。 */
function applyEvent(turn: LiveTurn, event: string, data: any) {
  // 分支：过程状态行（阶段或工具命中摘要）
  if (event === 'status') {
    turn.status = data.text ?? ''
    return
  }
  // 分支：工具返回 → 状态行更新成命中摘要，并带上检索到的条文供点开
  if (event === 'tool') {
    turn.status = data.summary ?? ''
    for (const item of data.items ?? []) addHit(turn, item)
    return
  }
  // 分支：答案增量 → 逐字追加（这是 v1 的体感核心）
  if (event === 'token') {
    turn.answer += data.text ?? ''
    void nextTick(scrollToBottom)
    return
  }
  if (event === 'citations') {
    turn.citations = data.citations ?? []
    turn.unverified = data.unverified ?? []
    return
  }
  if (event === 'usage') {
    turn.usage = data
    return
  }
  if (event === 'error') {
    turn.error = data.message ?? '回答失败，请重试'
    return
  }
  // 分支：收尾 → 用完整回答校对一次（防丢字），过程行收起
  if (event === 'done') {
    if (data.answer) turn.answer = data.answer
    turn.status = ''
  }
}

function addHit(turn: LiveTurn, item: ChatCitation) {
  if (!item?.ref) return
  const exists = turn.hits.some((h) => h.kind === item.kind && h.ref === item.ref && h.text === item.text)
  if (!exists) turn.hits.push(item)
}

function stop() {
  controller?.abort()
}

/** 失败重试：把这一轮清掉重问一遍（输入框内容不动）。 */
function retry(turn: LiveTurn) {
  turns.value = turns.value.filter((t) => t !== turn)
  void send(turn.question)
}

/** 一键复制：法务要把结论粘进邮件或审批意见里。 */
async function copy(turn: LiveTurn) {
  try {
    // 复制的是清洗后的正文（与页面所见一致，不带 Markdown 标记）
    await navigator.clipboard.writeText(cleanAnswer(turn.answer))
    turn.copied = true
    window.setTimeout(() => (turn.copied = false), 1500)
  } catch {
    turn.copied = false
  }
}

/** 清空这个会话：面板内二次确认后删后端检查点线程，回到空态。 */
function askClear() {
  if (!turns.value.length) return
  clearError.value = ''
  confirmClear.value = true
}

async function doClear() {
  try {
    await clearChat(props.threadId, sessionId)
    turns.value = []
    expanded.value = ''
    confirmClear.value = false
  } catch (err) {
    // 分支：清空失败就把原因留在确认条里，不用浏览器弹窗打断
    clearError.value = err instanceof Error ? err.message : '清空失败'
  }
}

function onKeydown(event: KeyboardEvent) {
  // Esc 随时关面板；回车只在输入框里发送（面板别处的回车不该触发提问）
  if (event.key === 'Escape') {
    closePanel()
    return
  }
  if (event.key === 'Enter' && !event.shiftKey && event.target instanceof HTMLTextAreaElement) {
    event.preventDefault()
    void send()
  }
}

function citationKey(citation: ChatCitation, index: number): string {
  return `${citation.kind}-${citation.ref}-${index}`
}

/** 悬浮预览：政策/条款原文的前几十字（点开可看全文）。 */
function preview(citation: ChatCitation): string {
  const text = (citation.text ?? '').replace(/\s+/g, ' ').trim()
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}

/** 芯片文字限长：条款号可能是整行标题（"八、违约责任："），太长会把芯片撑开。 */
function chipLabel(citation: ChatCitation): string {
  const ref = citation.ref ?? ''
  return ref.length > 14 ? `${ref.slice(0, 14)}…` : ref
}

/** 回答清洗：模型爱写 Markdown，这里只去掉标记本身，不做 HTML 渲染（不引注入面）。
 *
 * 注意在渲染时清洗而不是收到 token 就清洗——`**` 可能被切成两个片段，
 * 逐片段去标记会留下孤立的星号。 */
function cleanAnswer(text: string): string {
  return (text ?? '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}>\s?/gm, '')
}

function onCitation(citation: ChatCitation, key: string) {
  // 分支：合同条款引用 → 打开原文抽屉并高亮那句；政策引用 → 就地展开全文
  if (citation.kind === 'clause') {
    emit('openClause', citation.ref, citation.text ?? '')
    return
  }
  expanded.value = expanded.value === key ? '' : key
}

/** 动作按钮：回答里若有条款引用，就能直接跳报告里的风险卡或原文。 */
function firstClause(turn: LiveTurn): ChatCitation | null {
  return turn.citations.find((c) => c.kind === 'clause') ?? null
}

onMounted(() => {
  // 演示/验收直达：?chat=1 时自动展开面板（与 ?source=1 同一套口径）
  if (new URLSearchParams(location.search).get('chat') === '1') openPanel()
})
</script>

<template>
  <Teleport to="body">
    <!-- 浮动入口：贴视口右下角，页面滚动时不动 -->
    <button v-if="!open" class="chat-fab" title="问这份合同（解释判定、查政策、找条款）" @click="openPanel">
      <span class="fab-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M21 12a8 8 0 0 1-8 8H8l-4 3v-5.5A8 8 0 0 1 12 4a8 8 0 0 1 9 8z"></path>
          <path d="M9 11.5h6M9 14.5h4" stroke-linecap="round"></path>
        </svg>
      </span>
      <span class="fab-text">问这份合同</span>
    </button>

    <section v-else class="chat-panel" role="dialog" aria-label="问这份合同" @keydown="onKeydown">
      <header class="cp-head">
        <span class="cp-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
            <path d="M6 4.5h8.5L19 9v10.5H6z" stroke-linejoin="round"></path>
            <path d="M9 12h6M9 15.5h4" stroke-linecap="round"></path>
          </svg>
        </span>
        <div class="cp-id">
          <b>问这份合同</b>
          <span class="cp-meta mono-num">
            {{ fileName || '当前合同' }}<template v-if="fileName"> · </template>{{ threadId }}
          </span>
        </div>
        <div class="cp-head-actions">
          <button class="cp-icon-btn" :disabled="!turns.length" title="清空对话" @click="askClear">清空</button>
          <button class="cp-icon-btn cp-close" title="关闭（Esc）" @click="closePanel">✕</button>
        </div>
      </header>

      <!-- 清空二次确认：面板内说清楚，不用浏览器弹窗打断 -->
      <div v-if="confirmClear" class="cp-confirm" role="alert">
        <p class="cp-confirm-text">清空这份合同的全部对话？清掉后无法恢复。</p>
        <p v-if="clearError" class="cp-error">{{ clearError }}</p>
        <div class="cp-confirm-actions">
          <button class="cp-btn sm ghost" @click="confirmClear = false">取消</button>
          <button class="cp-btn sm danger" @click="doClear">确认清空</button>
        </div>
      </div>

      <div ref="bodyEl" class="cp-body">
        <!-- 空态：告诉用户能问什么，点一下就问 -->
        <div v-if="!turns.length" class="cp-empty">
          <span class="cp-empty-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M12 3.5 4.5 7v5.2c0 4.3 3.1 7.3 7.5 8.3 4.4-1 7.5-4 7.5-8.3V7z" stroke-linejoin="round"></path>
              <path d="M9 12.2l2.1 2.1L15.4 10" stroke-linecap="round" stroke-linejoin="round"></path>
            </svg>
          </span>
          <p class="cp-empty-title">可以问我这三类</p>
          <p class="cp-empty-note">判定解释 · 政策口径 · 条款定位</p>
          <button v-for="q in suggestions" :key="q" class="cp-suggest" @click="send(q)">
            <span class="cp-suggest-text">{{ q }}</span>
            <span class="cp-suggest-go" aria-hidden="true">↵</span>
          </button>
        </div>

        <div v-for="(turn, i) in turns" :key="i" class="cp-turn">
          <p class="cp-q"><span class="cp-q-mark" aria-hidden="true">问</span>{{ turn.question }}</p>
          <div class="cp-a">
            <!-- 过程可见：工具在做什么、命中了什么 -->
            <p v-if="turn.status" class="cp-status"><i class="cp-pulse" aria-hidden="true"></i>{{ turn.status }}</p>
            <p v-if="turn.answer" class="cp-text">{{ cleanAnswer(turn.answer) }}<span v-if="turn.running" class="cp-caret" aria-hidden="true"></span></p>
            <p v-if="turn.stopped" class="cp-note">已停止生成，以上是已出的内容</p>
            <p v-if="turn.error" class="cp-error">
              {{ turn.error }}<button class="cp-link" @click="retry(turn)">重试</button>
            </p>

            <!-- 工具取到的条文：默认收起，点开看它到底查到了什么 -->
            <details v-if="turn.hits.length" class="cp-hits">
              <summary>检索轨迹 · {{ turn.hits.length }} 条</summary>
              <div v-for="(hit, hi) in turn.hits" :key="hi" class="cp-hit">
                <p class="cp-hit-ref">
                  <span class="cp-tag">{{ hit.kind === 'policy' ? '政策' : '条款' }}</span>
                  <span class="mono-num">{{ hit.ref }}</span>
                </p>
                <p class="cp-hit-text">{{ hit.text }}</p>
              </div>
            </details>

            <!-- 引用芯片：政策芯片悬浮预览、点击展开全文；条款芯片点击开原文并高亮 -->
            <div v-if="turn.citations.length || turn.unverified.length" class="cp-cites">
              <span class="cp-cites-label">引用</span>
              <button
                v-for="(c, ci) in turn.citations"
                :key="citationKey(c, ci)"
                class="cp-chip"
                :class="[c.kind, c.origin === 'report' ? 'report' : '']"
                :title="c.origin === 'report' ? `${preview(c)}（依据来自报告）` : preview(c)"
                @click="onCitation(c, citationKey(c, ci))"
              >
                <span class="cp-chip-glyph" aria-hidden="true">{{ c.kind === 'policy' ? '§' : '条' }}</span>{{ chipLabel(c) }}
              </button>
              <span
                v-for="ref in turn.unverified"
                :key="ref"
                class="cp-chip unverified"
                title="回答里提到了这个编号，但这轮没有检索到它的条文（可能只是提议去查），无法核实"
              >
                <span class="cp-chip-glyph" aria-hidden="true">!</span>未检索到 {{ ref }}
              </span>
            </div>
            <div v-for="(c, ci) in turn.citations" :key="`full-${ci}`">
              <div v-if="expanded === citationKey(c, ci)" class="cp-cite-full">
                <p class="cp-cite-head">
                  <span class="cp-tag">{{ c.kind === 'policy' ? '政策' : '条款' }}</span>{{ c.title || c.ref }}
                </p>
                <p class="cp-cite-text">{{ c.text }}</p>
              </div>
            </div>

            <!-- 动作按钮：把回答变成下一步动作（改判定的事仍由人来做） -->
            <div v-if="!turn.running && (turn.answer || turn.error)" class="cp-actions">
              <button v-if="turn.answer" class="cp-act" @click="copy(turn)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                  <rect x="9" y="9" width="10" height="10" rx="2"></rect>
                  <path d="M15 9V6.5A1.5 1.5 0 0 0 13.5 5h-7A1.5 1.5 0 0 0 5 6.5v7A1.5 1.5 0 0 0 6.5 15H9"></path>
                </svg>
                {{ turn.copied ? '已复制' : '复制' }}
              </button>
              <button v-if="firstClause(turn)" class="cp-act" @click="emit('focusRisk', firstClause(turn)!.ref)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                  <circle cx="11" cy="11" r="6.5"></circle>
                  <path d="M11 8.2v3.4M11 14.4v.4" stroke-linecap="round"></path>
                  <path d="M16 16l4 4" stroke-linecap="round"></path>
                </svg>
                查看这条风险
              </button>
              <button v-if="firstClause(turn)" class="cp-act" @click="onCitation(firstClause(turn)!, '')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                  <path d="M6 4.5h8.5L19 9v10.5H6z" stroke-linejoin="round"></path>
                  <path d="M9 13h6M9 16h4" stroke-linecap="round"></path>
                </svg>
                打开原文
              </button>
              <button v-if="turn.error" class="cp-act" @click="retry(turn)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                  <path d="M19 12a7 7 0 1 1-2.1-5" stroke-linecap="round"></path>
                  <path d="M19 4.5V7h-2.5" stroke-linecap="round" stroke-linejoin="round"></path>
                </svg>
                重试
              </button>
              <span v-if="turn.usage" class="cp-usage mono-num">
                本次 {{ turn.usage.calls }} 次调用 · {{ turn.usage.seconds }} 秒
              </span>
            </div>
          </div>
        </div>
      </div>

      <footer class="cp-foot">
        <div class="cp-input-row">
          <textarea
            v-model="draft"
            class="cp-input"
            rows="1"
            placeholder="问点什么…（Enter 发送，Shift+Enter 换行）"
          ></textarea>
          <button v-if="busy" class="cp-stop" title="停止生成（保留已出的内容）" @click="stop">停止</button>
          <button class="cp-send" :disabled="!draft.trim() || busy" title="发送" @click="send()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true">
              <path d="M5 12h13M12.5 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round"></path>
            </svg>
          </button>
        </div>
        <div class="cp-foot-meta">
          <span class="cp-bound">助手只做解释与检索，不会改动风险判定</span>
          <span v-if="sessionUsage.calls" class="cp-sum mono-num">
            累计 {{ sessionUsage.calls }} 次 · {{ sessionUsage.seconds }} 秒
          </span>
        </div>
      </footer>
    </section>
  </Teleport>
</template>

<style scoped>
/* ---- 浮动入口：胶囊 + 圆形徽标，hover 轻微上浮 ---- */
.chat-fab {
  position: fixed;
  right: 24px;
  bottom: 24px;
  /* 压在原文抽屉（z-index 40）之下：抽屉打开时它让位，关掉抽屉原地还在 */
  z-index: 30;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 9px 16px 9px 11px;
  border: 1px solid var(--pri-deep);
  border-radius: 999px;
  background: linear-gradient(180deg, #3d5fe0 0%, var(--pri) 60%, var(--pri-deep) 100%);
  color: #fff;
  font-size: 14px;
  letter-spacing: 0.2px;
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(23, 31, 48, 0.16), 0 12px 28px rgba(52, 86, 209, 0.3);
  animation: fab-in 0.34s cubic-bezier(0.22, 0.9, 0.24, 1) both;
  transition: transform 0.16s ease, box-shadow 0.16s ease;
}

.chat-fab:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 10px rgba(23, 31, 48, 0.18), 0 16px 34px rgba(52, 86, 209, 0.34);
}

.chat-fab:active {
  transform: translateY(0) scale(0.99);
}

.chat-fab:focus-visible {
  outline: 3px solid var(--pri-soft);
  outline-offset: 2px;
}

.fab-mark {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.16);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.26);
}

.fab-mark svg {
  width: 15px;
  height: 15px;
}

@keyframes fab-in {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

/* ---- 面板 ---- */
.chat-panel {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 30;
  display: flex;
  flex-direction: column;
  width: 400px;
  height: 560px;
  overflow: hidden;
  border: 1px solid var(--line-strong);
  border-radius: 16px;
  background: var(--card);
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06), 0 18px 44px rgba(16, 24, 40, 0.2);
  animation: panel-in 0.22s cubic-bezier(0.22, 0.9, 0.24, 1) both;
}

@keyframes panel-in {
  from {
    opacity: 0;
    transform: translateY(14px) scale(0.985);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

/* 窄屏：改成底部整宽抽屉（顶部留一道抓取条，暗示它是抽屉） */
@media (max-width: 1100px) {
  .chat-panel {
    right: 0;
    bottom: 0;
    left: 0;
    width: auto;
    height: 74vh;
    border-radius: 18px 18px 0 0;
  }
}

.cp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 12px 11px 14px;
  border-bottom: 1px solid var(--line);
  /* 极淡的靛蓝顶光，把头部与正文分开（不用渐变花活） */
  background: linear-gradient(180deg, rgba(52, 86, 209, 0.07), rgba(52, 86, 209, 0) 82%), var(--card-2);
}

.cp-mark {
  flex: none;
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background: var(--pri);
  color: #fff;
  box-shadow: 0 4px 10px rgba(52, 86, 209, 0.28);
}

.cp-mark svg {
  width: 17px;
  height: 17px;
}

.cp-id {
  flex: 1;
  min-width: 0;
}

.cp-id b {
  display: block;
  font-family: var(--serif);
  font-size: 14.5px;
  letter-spacing: 0.3px;
}

.cp-meta {
  display: block;
  overflow: hidden;
  font-size: 11px;
  color: var(--muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cp-head-actions {
  flex: none;
  display: flex;
  gap: 6px;
}

.cp-icon-btn {
  padding: 4px 9px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  background: var(--card);
  color: var(--ink-2);
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.14s ease, color 0.14s ease, background 0.14s ease;
}

.cp-icon-btn:hover:not(:disabled) {
  border-color: var(--pri);
  background: var(--pri-soft);
  color: var(--pri-deep);
}

.cp-icon-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.cp-close {
  font-size: 13px;
  line-height: 1.2;
}

/* 清空二次确认条 */
.cp-confirm {
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--seal-soft);
}

.cp-confirm-text {
  margin: 0;
  color: var(--seal-deep);
  font-size: 13px;
}

.cp-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

.cp-body {
  flex: 1;
  overflow-y: auto;
  padding: 14px;
  scrollbar-width: thin;
}

.cp-body::-webkit-scrollbar {
  width: 8px;
}

.cp-body::-webkit-scrollbar-thumb {
  border: 2px solid transparent;
  border-radius: 999px;
  background: var(--line-strong);
  background-clip: content-box;
}

/* ---- 空态 ---- */
.cp-empty {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 6px 2px 0;
}

.cp-empty-mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  margin-bottom: 2px;
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  background: var(--card-2);
  color: var(--pri);
}

.cp-empty-mark svg {
  width: 21px;
  height: 21px;
}

.cp-empty-title {
  margin: 0;
  font-family: var(--serif);
  font-size: 14px;
}

.cp-empty-note {
  margin: -4px 0 4px;
  color: var(--muted);
  font-size: 12px;
  letter-spacing: 0.4px;
}

.cp-suggest {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--card);
  color: var(--ink-2);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: transform 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease, color 0.14s ease;
}

.cp-suggest:hover {
  transform: translateX(2px);
  border-color: var(--pri);
  box-shadow: 0 4px 12px rgba(52, 86, 209, 0.12);
  color: var(--pri-deep);
}

.cp-suggest-go {
  color: var(--muted);
  font-family: var(--mono);
  font-size: 12px;
}

/* ---- 一轮问答 ---- */
.cp-turn + .cp-turn {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px dashed var(--line);
}

.cp-q {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 0 0 8px;
  padding: 8px 12px;
  border-radius: 12px 12px 12px 4px;
  background: var(--pri-soft);
  color: var(--pri-deep);
  font-size: 13px;
  line-height: 1.6;
}

.cp-q-mark {
  flex: none;
  font-family: var(--serif);
  font-size: 11px;
  opacity: 0.72;
}

.cp-a {
  padding: 0 2px;
}

.cp-status {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 6px;
  color: var(--muted);
  font-size: 12px;
}

.cp-pulse {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--pri);
  animation: cp-pulse 1.1s ease-in-out infinite;
}

@keyframes cp-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.35;
    transform: scale(0.8);
  }
}

.cp-text {
  margin: 0;
  font-size: 14px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 逐字输出的光标：一根呼吸的靛蓝细条 */
.cp-caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: -0.12em;
  background: var(--pri);
  animation: cp-caret 0.9s steps(2, start) infinite;
}

@keyframes cp-caret {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}

.cp-note {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.cp-error {
  margin: 6px 0 0;
  color: var(--seal-deep);
  font-size: 13px;
}

.cp-link {
  margin-left: 6px;
  padding: 0;
  border: none;
  background: none;
  color: var(--pri);
  font-size: 12px;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 3px;
}

/* ---- 检索轨迹 ---- */
.cp-hits {
  margin-top: 10px;
  font-size: 12px;
}

.cp-hits summary {
  color: var(--muted);
  cursor: pointer;
  list-style: none;
}

.cp-hits summary::before {
  content: "▸";
  display: inline-block;
  width: 12px;
  color: var(--line-strong);
  transition: transform 0.14s ease;
}

.cp-hits[open] summary::before {
  transform: rotate(90deg);
}

.cp-hit {
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-left: 3px solid var(--pri);
  border-radius: 0 8px 8px 0;
  background: var(--card-2);
}

.cp-hit-ref {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 3px;
  color: var(--pri-deep);
  font-size: 12px;
}

.cp-hit-text {
  margin: 0;
  max-height: 96px;
  overflow-y: auto;
  color: var(--ink-2);
  white-space: pre-wrap;
}

/* ---- 引用芯片与条文展开 ---- */
.cp-cites {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
}

.cp-cites-label {
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 0.6px;
}

.cp-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px 3px 7px;
  border: 1px solid rgba(52, 86, 209, 0.34);
  border-radius: 999px;
  background: var(--pri-soft);
  color: var(--pri-deep);
  font-size: 12px;
  cursor: pointer;
  transition: transform 0.14s ease, box-shadow 0.14s ease, border-color 0.14s ease;
}

.cp-chip:hover {
  transform: translateY(-1px);
  border-color: var(--pri);
  box-shadow: 0 3px 8px rgba(52, 86, 209, 0.16);
}

.cp-chip-glyph {
  font-family: var(--mono);
  font-size: 10px;
  opacity: 0.7;
}

/* 条款引用：中性底，与政策引用区分开 */
.cp-chip.clause {
  border-color: var(--line-strong);
  background: var(--card-2);
  color: var(--ink-2);
}

.cp-chip.clause:hover {
  border-color: var(--ink-2);
  box-shadow: 0 3px 8px rgba(28, 36, 51, 0.1);
}

/* 报告依据（这轮没检索、但报告里本来就有这条）：描边淡一点，表示出处是报告而不是本轮检索 */
.cp-chip.report {
  border-color: var(--line-strong);
  border-style: dashed;
  background: var(--card);
  color: var(--ink-2);
}

.cp-chip.report:hover {
  border-color: var(--pri);
  color: var(--pri-deep);
}

/* 查不到出处的编号：琥珀虚线，明说"无法核实" */
.cp-chip.unverified {
  border-color: var(--warn);
  border-style: dashed;
  background: var(--warn-soft);
  color: var(--warn);
  cursor: default;
}

.cp-chip.unverified:hover {
  transform: none;
  box-shadow: none;
}

.cp-tag {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--pri-soft);
  color: var(--pri-deep);
  font-family: var(--serif);
  font-size: 11px;
}

.cp-cite-full {
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--card-2);
  font-size: 12.5px;
}

.cp-cite-head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 6px;
  color: var(--ink);
}

.cp-cite-text {
  margin: 0;
  max-height: 200px;
  overflow-y: auto;
  color: var(--ink-2);
  line-height: 1.75;
  white-space: pre-wrap;
}

/* ---- 动作行 ---- */
.cp-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}

.cp-act {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: none;
  color: var(--ink-2);
  font-size: 12px;
  cursor: pointer;
  transition: background 0.14s ease, color 0.14s ease, border-color 0.14s ease;
}

.cp-act:hover {
  border-color: var(--line-strong);
  background: var(--card-2);
  color: var(--pri-deep);
}

.cp-act svg {
  width: 13px;
  height: 13px;
}

.cp-usage {
  margin-left: auto;
  color: var(--muted);
  font-size: 11px;
}

/* ---- 输入区 ---- */
.cp-foot {
  padding: 10px 12px 12px;
  border-top: 1px solid var(--line);
  background: var(--card-2);
}

.cp-input-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.cp-input {
  flex: 1;
  max-height: 108px;
  padding: 9px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  background: var(--card);
  font-size: 13px;
  line-height: 1.6;
  resize: none;
  transition: border-color 0.14s ease, box-shadow 0.14s ease;
}

.cp-input:focus {
  border-color: var(--pri);
  outline: none;
  box-shadow: 0 0 0 3px var(--pri-soft);
}

.cp-send {
  flex: none;
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border: 1px solid var(--pri);
  border-radius: 50%;
  background: var(--pri);
  color: #fff;
  cursor: pointer;
  transition: background 0.14s ease, transform 0.14s ease, box-shadow 0.14s ease;
}

.cp-send svg {
  width: 17px;
  height: 17px;
}

.cp-send:hover:not(:disabled) {
  background: var(--pri-deep);
  box-shadow: 0 4px 12px rgba(52, 86, 209, 0.32);
}

.cp-send:active:not(:disabled) {
  transform: scale(0.96);
}

.cp-send:disabled {
  border-color: var(--line-strong);
  background: var(--line-strong);
  cursor: not-allowed;
}

.cp-stop {
  flex: none;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  background: var(--card);
  color: var(--ink-2);
  font-size: 12px;
  cursor: pointer;
}

.cp-stop:hover {
  border-color: var(--seal);
  color: var(--seal-deep);
}

.cp-foot-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 7px;
}

.cp-bound {
  color: var(--muted);
  font-size: 11px;
}

.cp-sum {
  flex: none;
  color: var(--muted);
  font-size: 11px;
}

/* ---- 小按钮（清空确认条用） ---- */
.cp-btn {
  padding: 6px 14px;
  border: 1px solid var(--pri);
  border-radius: 8px;
  background: var(--pri);
  color: #fff;
  font-size: 13px;
  cursor: pointer;
}

.cp-btn.sm {
  padding: 4px 11px;
  font-size: 12px;
}

.cp-btn.ghost {
  border-color: var(--line-strong);
  background: var(--card);
  color: var(--ink-2);
}

.cp-btn.danger {
  border-color: var(--seal);
  background: var(--seal);
}

.cp-btn.danger:hover {
  background: var(--seal-deep);
  border-color: var(--seal-deep);
}

/* 尊重"减少动态效果"：只留必要的过渡，不做入场/呼吸动画 */
@media (prefers-reduced-motion: reduce) {
  .chat-fab,
  .chat-panel,
  .cp-pulse,
  .cp-caret {
    animation: none;
  }

  .cp-suggest:hover,
  .chat-fab:hover {
    transform: none;
  }
}
</style>
