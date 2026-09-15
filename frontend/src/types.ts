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
  // 原文摘录（后端定位 pass 填）：evidence 是规则说明句时，用它做原文定位/高亮
  evidence_quote?: string
}

/** 闸口载荷：ask 是给审批人看的引导文案。 */
export interface GatePayload {
  ask?: string
  grade?: string
  high_risks: GateHighRisk[]
  // 双审复核结论：闸口阶段报告还没生成，靠它让审批人先看到盲审发现
  review?: ReviewSection | null
}

/** 一条风险：字段与 rules 的 RiskItem 对齐，evidence 是原文摘录。 */
export interface RiskItem {
  risk_type: string
  label?: string | null // 中文展示名（同 GateHighRisk.label）
  severity: Severity
  clause_ref?: string
  evidence?: string
  evidence_quote?: string // 原文摘录（定位/高亮优先用它；evidence 可能是规则说明句）
  policy_ref?: string | null
  suggestion?: string
  field?: string | null // 关联的 ContractModel 字段名（前端高亮预留）
  origin?: 'rules' | 'review' | null // 来源：rules=主审 / review=复核新增（报告徽标）
}

/** 风险卡的输入形态：闸口待审项与报告风险共用。
 *  闸口项不带 severity（那一屏全是 high），展示时按 high 处理。 */
export interface RiskCardItem {
  risk_type: string
  label?: string | null
  severity?: Severity
  clause_ref?: string
  evidence?: string
  evidence_quote?: string
  policy_ref?: string | null
  suggestion?: string
  field?: string | null
  origin?: 'rules' | 'review' | null
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
  outcome: 'agreed' | 'added' | 'upgraded' | 'noted' // 一致/复核新增/取高升级/仅记录不并入
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
    noted: number // 仅记录不并入（不进风险清单）
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
  policy_library?: PolicyLibrary | null
  extracted?: Record<string, unknown> | null
  approval?: Approval | null
  review_mode?: string // single/double/parallel（多智能体决策钩子）
  review?: ReviewSection | null // 双审复核段（double 模式报告）
  llm?: LlmUsage | null // 大模型用量（calls/stages/seconds；老报告可能没有）
  error?: string
  status?: string
}

/** 政策库版本段：本次报告依据的语料版本（内容指纹）与份数/条数。 */
export interface PolicyLibrary {
  version: string // 语料内容指纹（如 PL-3f9a1c8b2d4e）
  files: number // 政策文件份数
  units: number // 检索单元（分条后的条文）条数
  revisions: string[] // 各份政策的编号与版本号（如 "P-01 V2.0"）
}

/** LLM 用量段：只计 chat 调用（抽取首读/二读、盲审），不含本地规则与检索。 */
export interface LlmUsage {
  calls: number // 调用总次数
  stages: Record<string, number> // 分阶段次数（extract/review）
  seconds: number // 调用累计耗时（秒）
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
  // 源文件已丢失、被服务端隐藏的历史任务数（前端提示一行，不做静默吞掉）
  hidden_stale?: number
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

/** 对话助手的一条引用：政策条文或合同条款。
 *  由后端按"这一轮工具真正返回过的条目"汇总，不是模型复述出来的内容。 */
export interface ChatCitation {
  kind: 'policy' | 'clause' // policy=政策库条文 / clause=合同条款
  ref: string // 政策编号（P-XX）或条款号（如"第五条"）
  title?: string // 条文标题 / 条款标题（芯片标题位）
  text?: string // 引用正文（芯片预览与展开）
  source?: string // 政策来源文件名（条款引用为空）
  origin?: 'tool' | 'report' // tool=这轮工具检索到的 / report=报告里本来就有的这条依据
}

/** 对话助手一轮问答：历史回读时引用由后端重算，与当时页面上的芯片一致。 */
export interface ChatTurn {
  question: string
  answer: string
  citations: ChatCitation[]
  unverified: string[] // 回答提到但查不到出处的政策编号（面板标"无法核实"）
}

/** 对话用量：成本行"本次 N 次调用 · M 秒"；token 由框架回调提供（拿不到时为 0）。 */
export interface ChatUsage {
  calls: number
  seconds: number
  tokens?: number
}

// ---- 政策库起稿（只读起稿页；与 backend/app/policy_assistant.py、routes_policy.py 对齐）----

/** 重叠分级：high=余弦分≥0.80（疑似重复或替代）/ medium=≥0.70（值得并读）/ low=其余。 */
export type OverlapLevel = 'high' | 'medium' | 'low'

/** 一条可核对的冲突：编号撞号 / 元信息缺失 / 同主题阈值数字不一致。 */
export interface PolicyConflict {
  kind: string // 冲突类型（后端给的短标签，直接展示）
  detail: string // 说明：撞了哪个号、缺哪项、两个数字各是多少
}

/** 重叠记录里的一条命中：命中的既有条文与它的余弦分。 */
export interface PolicyOverlapHit {
  policy_ref: string // 命中的政策编号（如 P-01）
  source: string // 命中的政策文件名
  score: number // 余弦相似度（0~1）
  text_head: string // 命中条文开头（去掉空白，截 60 字）
  numbers: { percent: string[]; months: string[] } // 命中条文里的百分比与月数
}

/** 逐条重叠分级：新政策的一条条文 vs 现有政策库。 */
export interface PolicyOverlapItem {
  article: string // 新政策的条文头（无标题时为"(无标题)"）
  level: OverlapLevel
  numbers: { percent: string[]; months: string[] } // 这条新条文里的百分比与月数
  hits: PolicyOverlapHit[] // 命中的既有条文（按相似度降序）
}

/** 起稿解析结果：元信息 + 条文逐条（缺项列在 missing 里）。 */
export interface PolicyParsed {
  title: string
  source: string
  ref: string // 政策编号
  version: string
  effective_date: string
  owner: string // 归口部门
  scope: string // 适用范围
  articles: { heading: string; body: string }[]
  missing: string[] // 缺哪些元信息（入库前要补齐）
}

/** 起稿摘要（POST /api/policy/drafts）：够页面先出概览，详情再取一次。 */
export interface PolicyDraftSummary {
  draft_id: string
  source: string
  ref: string
  title: string
  articles: number // 条文数
  missing: string[]
  overlap: { high: number; medium: number; low: number }
  conflicts: PolicyConflict[]
}

/** 草稿详情（GET /api/policy/drafts/{id}）：草稿全文 + 重叠 + 冲突 + 配套清单。 */
export interface PolicyDraftDetail {
  draft_id: string
  source: string
  ref: string
  title: string
  suggested_file: string // 建议的入库文件名（编号 + 标题短名）
  applied: DraftApplied | null // 已入库记录（没入过库为 null）
  created_at: string // 起稿时间（本地时间字符串）
  parsed: PolicyParsed
  conflicts: PolicyConflict[]
  overlaps: PolicyOverlapItem[]
  draft: string // 规范化草稿（markdown 文本）
  checklist: string // 配套改动清单（markdown 文本）
  files: string[] // 草稿目录里的产物文件名
}

// ---- 政策入库（B2：会用真库，所以计划与执行分成两步）----

/** 入库计划（confirm=false 时返回）：要写/要删几条、版本号怎么变、有没有硬冲突。 */
export interface PublishPlan {
  file_name: string
  ref: string
  exists: boolean // 目标文件已存在 → 这次是"更新那份政策"
  write_units: number // 要写入（或重写）的检索单元数
  delete_units: number // 要删掉的消失单元数
  delete_sources: string[] // 被删单元来自哪些文件
  missing: string[] // 缺哪些元信息（补齐或显式放行）
  version: string // 当前政策库版本
  next_version: string // 落盘后的政策库版本（预估）
  blockers: PolicyConflict[] // 硬冲突：撞号、文件名不合法
}

/** 入库结果（confirm=true）：落盘并同步后的回执。 */
export interface PublishResult {
  applied: boolean
  file_name: string
  ref: string
  updated: boolean // true=更新已有政策 / false=新增
  written: number
  removed: number
  check_ok: boolean // 执行后核对是否一致
  previous_version: string
  version: string
  units: number // 入库后语料单元总数
}

/** 草稿的入库记录（页面据此显示"已入库"徽标）。 */
export interface DraftApplied {
  file_name: string
  version: string
  updated: boolean
  written: number
  removed: number
  at: string
}

/** 政策库现状（GET /api/policy/library）：版本 + 逐份清单，纯读盘。 */
export interface PolicyLibrary {
  version: string
  files: number
  units: number
  documents: {
    ref: string
    source: string
    title: string
    version: string
    effective_date: string
    units: number
  }[]
}
