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
  clause_ref?: string
  evidence?: string
  policy_ref?: string | null
  suggestion?: string
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
  severity: Severity
  clause_ref?: string
  evidence?: string
  policy_ref?: string | null
  suggestion?: string
  field?: string | null // 关联的 ContractModel 字段名（前端高亮预留）
}

/** 政策引用：报告里 policy_ref 对应的政策原文片段与相似度。 */
export interface PolicyHit {
  policy_ref: string
  score?: number | null
  snippet?: string
}

/** 审批记录：edited 时 patches 是字段补丁（回后端 rules 重审用）。 */
export interface Approval {
  action: ApprovalAction
  reviewer_note?: string
  patches?: Record<string, unknown> | null
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
  error?: string
  status?: string
}

/** 队列列表项（无报告正文，详情接口才带 report）。 */
export interface TaskSummary {
  thread_id: string
  source: string // 展示名：原始文件名
  status: TaskStatus
  grade?: Grade
  risk_count?: number | null // done=报告风险数；gate=待审 high 数；其余 null
  error?: string
}

/** 任务详情：gate 时才有 gate_payload；done 时才有 report。 */
export interface TaskDetail extends TaskSummary {
  gate_payload?: GatePayload | null
  report?: Report | null
}
