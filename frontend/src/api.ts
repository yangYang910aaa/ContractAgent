/**
 * 后端 /api 客户端：上传 / 队列 / 详情 / 审批。
 * 开发环境经 vite 代理（/api → 127.0.0.1:8000），生产由同机静态托管或反代。
 * 约定：非 2xx 统一抛 Error(detail)，页面 catch 后展示即可。
 */

import type { SourceDoc, TaskDetail, TaskList } from './types'

/** 解包响应：失败时优先取后端的 detail 文案（FastAPI HTTPException）。 */
async function j<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail ?? `请求失败 (HTTP ${resp.status})`)
  }
  return resp.json() as Promise<T>
}

/** 上传一份合同（multipart），返回任务 thread_id。 */
export async function uploadContract(file: File): Promise<{ thread_id: string; status: string }> {
  const form = new FormData()
  form.append('file', file)
  return j(await fetch('/api/tasks', { method: 'POST', body: form }))
}

/** 任务队列列表（倒序；队列页轮询用）。 */
export async function listTasks(): Promise<TaskList> {
  return j(await fetch('/api/tasks'))
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
