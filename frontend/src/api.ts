/**
 * 后端 /api 客户端：上传 / 队列 / 详情 / 审批。
 * 开发环境经 vite 代理（/api → 127.0.0.1:8000），生产由同机静态托管或反代。
 * 约定：非 2xx 统一抛 Error(detail)，页面 catch 后展示即可。
 */

import type {
  ChatCitation,
  ChatTurn,
  ChatUsage,
  PolicyDraftDetail,
  PolicyDraftSummary,
  PolicyLibrary,
  PublishPlan,
  PublishResult,
  SourceDoc,
  TaskDetail,
  TaskList,
} from './types'

/** 解包响应：失败时优先取后端的 detail 文案（FastAPI HTTPException）。 */
async function j<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail ?? `请求失败 (HTTP ${resp.status})`)
  }
  return resp.json() as Promise<T>
}

/** 上传一份合同（multipart），返回任务 thread_id；reviewMode 选单审/双审。 */
export async function uploadContract(
  file: File,
  reviewMode: 'single' | 'double' = 'single',
): Promise<{ thread_id: string; status: string; review_mode: string }> {
  const form = new FormData()
  form.append('file', file)
  form.append('review_mode', reviewMode)
  return j(await fetch('/api/tasks', { method: 'POST', body: form }))
}

/** 任务队列列表（倒序；队列页轮询用）。 */
export async function listTasks(): Promise<TaskList> {
  return j(await fetch('/api/tasks'))
}

/** 删除任务：记录与上传的原文件一并删除（不可恢复）；队列页清理历史用。 */
export async function deleteTask(threadId: string): Promise<{ deleted: string }> {
  return j(await fetch(`/api/tasks/${encodeURIComponent(threadId)}`, { method: 'DELETE' }))
}

/** 批量删除结果：deleted 成功删除，skipped 被跳过的任务及原因。 */
export interface BatchDeleteResult {
  deleted: string[]
  skipped: { thread_id: string; reason: string }[]
}

/** 批量删除任务（处理中的自动跳过并说明原因）。 */
export async function batchDeleteTasks(threadIds: string[]): Promise<BatchDeleteResult> {
  return j(
    await fetch('/api/tasks/batch-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ thread_ids: threadIds }),
    }),
  )
}

/** 任务详情（详情页轮询用；threadId 来自后端，仍需转义防路径注入）。 */
export async function getTask(threadId: string): Promise<TaskDetail> {
  return j(await fetch(`/api/tasks/${encodeURIComponent(threadId)}`))
}

/** 任务原合同（全文 + 条款块）：原文抽屉数据源（U2）。 */
export async function getSource(threadId: string): Promise<SourceDoc> {
  return j(await fetch(`/api/tasks/${encodeURIComponent(threadId)}/source`))
}

/** 原文件下载/预览 URL：pdf 内嵌 iframe 预览、docx/md 下载打开都用它（U2）。 */
export function taskFileUrl(threadId: string): string {
  return `/api/tasks/${encodeURIComponent(threadId)}/file`
}

/** 审批-放行：高风险留档但人工确认可接受。 */
export async function approve(threadId: string, note: string): Promise<TaskDetail> {
  return j(
    await fetch(`/api/tasks/${encodeURIComponent(threadId)}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note }),
    }),
  )
}

/** 审批-打回：note 必填原因（后端只要求留痕，前端强校验非空）。 */
export async function reject(threadId: string, note: string): Promise<TaskDetail> {
  return j(
    await fetch(`/api/tasks/${encodeURIComponent(threadId)}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note }),
    }),
  )
}

/** 审批-编辑重审：patches 是字段补丁，后端会回 rules 重算（可能再次停闸口）。 */
export async function editFields(
  threadId: string,
  patches: Record<string, unknown>,
  note: string,
): Promise<TaskDetail> {
  return j(
    await fetch(`/api/tasks/${encodeURIComponent(threadId)}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patches, note }),
    }),
  )
}

// ---- 对话助手：逐字流式（SSE）+ 历史回读 + 清空会话 + 非流式降级 ----

/** 对话流的事件名：与后端 stream_chat 推的帧一一对应。 */
export type ChatEvent = 'status' | 'tool' | 'token' | 'citations' | 'usage' | 'error' | 'done'

/** 对话回答结果（非流式/降级路径的响应）。 */
export interface ChatAnswer {
  answer: string
  citations: ChatCitation[]
  unverified: string[]
  usage: ChatUsage
}

/** 一轮问答的流式入口：逐帧回调事件名与数据，读完即返回。
 *
 * 用 fetch 读流而不是 EventSource——后者只能发 GET，带不了消息体；
 * signal 用于"停止生成"：中止后已收到的内容保留在前端。
 */
export async function streamChat(
  threadId: string,
  message: string,
  sessionId: string,
  onEvent: (event: ChatEvent, data: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`/api/tasks/${encodeURIComponent(threadId)}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail ?? `请求失败 (HTTP ${resp.status})`)
  }
  // 分支：后端没给流式体（异常/代理改写）→ 抛错让调用方走非流式降级
  if (!resp.body) throw new Error('浏览器未收到流式响应')

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE 帧之间用空行分隔；逐帧解析，避免把半截 JSON 当数据
    let split = buffer.indexOf('\n\n')
    while (split >= 0) {
      const frame = buffer.slice(0, split)
      buffer = buffer.slice(split + 2)
      const parsed = parseFrame(frame)
      if (parsed) onEvent(parsed.event, parsed.data)
      split = buffer.indexOf('\n\n')
    }
  }
}

/** 解析一个 SSE 帧 → 事件名与数据；空帧或数据不合法时返回 null。 */
function parseFrame(frame: string): { event: ChatEvent; data: any } | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice('event: '.length).trim()
    // 分支：数据行可能有多行，按 SSE 约定拼起来再解析
    else if (line.startsWith('data: ')) dataLines.push(line.slice('data: '.length))
  }
  if (!dataLines.length) return null
  try {
    return { event: event as ChatEvent, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}

/** 非流式问一句：前端读流失败时降级用它，拿到整段回答而不是逐字。 */
export async function askChat(
  threadId: string,
  message: string,
  sessionId: string,
): Promise<ChatAnswer> {
  return j(
    await fetch(`/api/tasks/${encodeURIComponent(threadId)}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: sessionId }),
    }),
  )
}

/** 回读某个会话的历史：刷新页面/后端重启后再打开面板，问答与引用都还在。 */
export async function fetchChatHistory(threadId: string, sessionId: string): Promise<ChatTurn[]> {
  const body = await j<{ turns: ChatTurn[] }>(
    await fetch(`/api/tasks/${encodeURIComponent(threadId)}/chat/${encodeURIComponent(sessionId)}`),
  )
  return body.turns
}

/** 清空这个会话（后端删对话检查点线程），面板回到空态并显示建议问题。 */
export async function clearChat(
  threadId: string,
  sessionId: string,
): Promise<{ cleared: boolean; reason?: string }> {
  return j(
    await fetch(
      `/api/tasks/${encodeURIComponent(threadId)}/chat/${encodeURIComponent(sessionId)}`,
      { method: 'DELETE' },
    ),
  )
}

// ---- 政策库起稿（只读起稿：不写政策库，产物落草稿目录）----

/** 起稿：文件与粘贴正文二选一；返回摘要与 draft_id。 */
export async function createPolicyDraft(input: {
  file?: File
  text?: string
  name?: string
}): Promise<PolicyDraftSummary> {
  const form = new FormData()
  if (input.file) form.append('file', input.file)
  if (input.text) form.append('text', input.text)
  if (input.name) form.append('name', input.name)
  return j(await fetch('/api/policy/drafts', { method: 'POST', body: form }))
}

/** 草稿详情：草稿全文 / 重叠分级 / 冲突 / 配套清单（刷新页面后回看也用它）。 */
export async function getPolicyDraft(draftId: string): Promise<PolicyDraftDetail> {
  return j(await fetch(`/api/policy/drafts/${encodeURIComponent(draftId)}`))
}

/** 入库入参：file_name 留空用建议名，content 留空用草稿原文。 */
export interface PublishInput {
  file_name?: string
  content?: string
  allow_missing_meta?: boolean
}

/** 入库预览：只算计划（要写/要删几条、版本号怎么变），不落盘、不改库。 */
export async function planPolicyPublish(
  draftId: string,
  input: PublishInput,
): Promise<{ applied: false; plan: PublishPlan }> {
  return j(
    await fetch(`/api/policy/drafts/${encodeURIComponent(draftId)}/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...input, confirm: false }),
    }),
  )
}

/** 确认入库：落盘 + 按单元同步进向量库（会改真库；核对不过后端会自动退回）。 */
export async function publishPolicyDraft(
  draftId: string,
  input: PublishInput,
): Promise<PublishResult> {
  return j(
    await fetch(`/api/policy/drafts/${encodeURIComponent(draftId)}/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...input, confirm: true }),
    }),
  )
}

/** 政策库现状：版本号 + 逐份清单（纯读盘，0 次模型调用）。 */
export async function getPolicyLibrary(): Promise<PolicyLibrary> {
  return j(await fetch('/api/policy/library'))
}
