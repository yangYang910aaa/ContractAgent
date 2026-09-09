/**
 * 与后端 /api 契约对应的类型。
 * 字段与 backend/app/schemas.py、routes_tasks.py 对齐；改后端字段时同步这里。
 */

// 任务生命周期：pending(排队) → processing(审查中) → gate(待审批)/done/error。
// gate 是 HITL 闸口：只有这里允许 approve/reject/edit，其余状态后端会回 409。
export type TaskStatus = 'pending' | 'processing' | 'gate' | 'done' | 'error'
export type Severity = 'high' | 'medium' | 'low' // 与 rules 风险等级一致
export type Grade = 'pass' | 'conditional_pass' | 'fail' | null // 报告评级
export type ApprovalAction = 'approved' | 'rejected' | 'edited' // 审批三动作

/** 闸口待审的高风险摘要（后端 gate_payload.high_risks，供审批页展示）。 */
export interface GateHighRisk {
  risk_type: string
  label?: string | null // 中文展示名（后端 rules 填；旧任务缺失时前端映射兜底）
  clause_ref?: string
  evidence?: string
  policy_ref?: string | null
  suggestion?: string
  origin?: 'rules' | 'review' | null // 风险来源：review=盲审复核补抓（审批页标注）
}

/** 闸口载荷：ask 是给审批人看的引导文案。 */
export interface GatePayload {
  ask?: string
  grade?: string
  high_risks: GateHighRisk[]
}

/** 一条风险：字段与 rules 的 RiskItem 对齐，evidence 是原文摘录。 */
export interface RiskItem {
  risk_type: string
  label?: string | null // 中文展示名（同 GateHighRisk.label）
  severity: Severity
  clause_ref?: string
  evidence?: string
  policy_ref?: string | null
  suggestion?: string
  field?: string | null // 关联的 ContractModel 字段名（前端高亮预留）
  origin?: 'rules' | 'review' | null // 来源：rules=主审 / review=复核新增（报告徽标）
}

/** 政策引用：报告里 policy_ref 对应的政策原文片段与相似度。 */
export interface PolicyHit {
  policy_ref: string
  score?: number | null
  snippet?: string // 引用片段（后端已去 md 标记并按句截断）
  text?: string | null // 命中政策的完整条文（前端"查看完整条文"展开用）
  full_text?: string | null // 该政策的整份全文（分条后 text 是命中条文，展开用整份）
}

/** 审批记录：edited 时 patches 是字段补丁（回后端 rules 重审用）。 */
export interface Approval {
  action: ApprovalAction
  reviewer_note?: string
  patches?: Record<string, unknown> | null
}

/** 双审 review 段里一条复核发现的处理结果（outcome 与后端 merge_review 对齐）。 */
export interface ReviewDetail {
  risk_type: string
  severity: Severity
  clause_ref?: string
  evidence?: string
  policy_ref?: string | null
  suggestion?: string
  outcome: 'agreed' | 'added' | 'upgraded' | 'noted' // 一致/复核新增/取高升级/仅提示
  note?: string // outcome 的补充说明（如复核门拦截原因）
}

/** 双审复核结果段（review_mode=double 的报告才有值；merge_review 产出）。 */
export interface ReviewSection {
  mode: 'double'
  summary?: string // 一句话摘要（前端卡片首行）
  stats: {
    findings: number // 复核发现总数
    agreed: number // 与主审一致
    upgraded: number // 取高升级
    added: number // 复核新增（并入风险）
    noted: number // 仅提示不并入
  }
  details?: ReviewDetail[]
  error?: string | null // 盲审调用失败说明（best-effort）
}

/** 最终报告：grade 为 null 表示审查失败（见 error）。 */
export interface Report {
  contract_file?: string
  grade?: Grade
  risks?: RiskItem[]
  policy_hits?: PolicyHit[]
  extracted?: Record<string, unknown> | null
  approval?: Approval | null
  review_mode?: string // single/double/parallel（多智能体决策钩子）
  review?: ReviewSection | null // 双审复核段（double 模式报告）
  error?: string
  status?: string
}

/** 队列列表项（无报告正文，详情接口才带 report）。 */
export interface TaskSummary {
  thread_id: string
  source: string // 展示名：原始文件名
  status: TaskStatus
  review_mode?: string // single/double（队列行徽标）
  grade?: Grade
  risk_count?: number | null // done=报告风险数；gate=待审 high 数；其余 null
  template?: boolean // 报告含"疑似空白模板"结论（前端评级显示"待确认"）
  error?: string
}

/** 队列列表响应：任务数组 + 服务端并发上限。 */
export interface TaskList {
  tasks: TaskSummary[]
  concurrency?: number
}

/** 任务详情：gate 时才有 gate_payload；done 时才有 report。 */
export interface TaskDetail extends TaskSummary {
  gate_payload?: GatePayload | null
  report?: Report | null
}

/** U2 原文抽屉：/source 返回的一个条款块（ref=第X条/章节整行标题，title=块头）。 */
export interface SourceBlock {
  ref: string
  title: string
  text: string
}

/** U2 任务原合同数据（GET /api/tasks/{id}/source）。 */
export interface SourceDoc {
  thread_id: string
  name: string // 展示名（原始文件名）
  suffix: string // 源文件后缀（如 .md/.docx/.pdf），前端据此选预览方式
  kind: 'sample' | 'upload' // 内置演示样本 vs 用户上传
  file_available: boolean // 源文件是否还在磁盘（可下载/预览）
  text: string // 解析后的合同全文（pending 阶段为空）
  blocks: SourceBlock[] // 按条款/章节切分的块（证据高亮锚点载体）
}

/** U2 原文定位指令：clause 对齐风险项 clause_ref；无条款号的风险（如中风险
 *  建议项）可用 evidence 原文摘录定位到所在条款块；seq 递增保证重复点击仍触发。 */
export interface SourceAnchor {
  clause?: string
  evidence?: string
  seq: number
}
