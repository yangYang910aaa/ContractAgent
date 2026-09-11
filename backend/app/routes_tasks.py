"""FastAPI 任务路由: 上传/队列/详情/审批.

约定: 任务状态都写在 manager.runner.store(ThreadStore), 路由层只做校验+转译,
不直接碰图. 审批动作 approve/reject/edit 复用 ReviewRunner.resume.

用户上传合同 ──→ 查看进度/列表 ──→ 查看原文/下载 ──→ 人工审批决策
     │                │                  │                │
  upload_task     list_tasks      get_task_source    approve_task
  enqueue_samples get_task        get_task_file      reject_task
                                                       edit_task
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.app.config import BASE_DIR
from backend.app.parser import split_clauses
from backend.app.tasks import TaskManager

router = APIRouter(prefix="/api", tags=["tasks"])

# 上传白名单：文本型合同 + 图片型输入（扫描件 PDF/相机拍照件/截图件，走 OCR 通路）
ALLOWED_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
CONTRACTS_DIR = BASE_DIR / "data" / "contracts"

# 原文件下载时的 Content-Type：docx/pdf 给浏览器可识别的类型（pdf 可内嵌预览）
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    # 图片件原文件下载/内嵌展示用（扫描件多为 image/jpeg）
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def get_manager(request: Request) -> TaskManager:
    """依赖: 从 app.state 取 TaskManager"""
    return request.app.state.manager


def _remove_source_file(source: str) -> None:
    """删除任务对应的上传落盘文件(仅限 data/uploads 内, 防误删任意路径)。"""
    try:
        path = Path(source).resolve()
    except Exception:
        return
    # 这种情况是: 源不在上传目录(内置样本路径/脏数据) → 不动磁盘
    try:
        path.relative_to(UPLOAD_DIR.resolve())
    except ValueError:
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass  # 删文件失败不阻断任务删除(登记簿删除为主)


def _delete_one(manager: TaskManager, thread_id: str) -> str | None:
    """删单个任务(登记簿 + 检查点 + 上传文件); 成功返回 None, 失败返回原因。"""
    record = manager.runner.store.get(thread_id)
    if record is None:
        return "任务不存在"
    # 这种情况是: 正在处理中 → 拒绝删(防删到一半的文件/状态)
    if record.status in ("pending", "processing"):
        return "任务正在处理"
    manager.runner.delete_task(thread_id)
    _remove_source_file(record.source)
    return None


class ApprovalIn(BaseModel):
    """审批入参: 只收意见文本; 动作(放行/打回)由不同路由决定."""

    note: str = Field(default="", description="审批意见（打回必填原因，前端提示）")


class EditIn(BaseModel):
    """编辑重审入参: 字段补丁(键=ContractModel 字段名) + 修改说明."""

    patches: dict = Field(description="字段补丁，如 {'warranty_months': 24}")
    note: str = Field(default="", description="修改说明")


class BatchDeleteIn(BaseModel):
    """批量删除入参: 要删除的任务 thread_id 列表(去重后逐个删)。"""

    thread_ids: list[str] = Field(min_length=1, description="任务 thread_id 列表")


class TaskSummaryOut(BaseModel):
    """任务摘要响应模型: 列表/详情/审批接口共用.

    Swagger 自动展示字段结构; 前端队列行按此渲染.
    """

    thread_id: str  # 任务标识
    source: str  # 展示用原始文件名
    status: str  # pending/processing/gate/done/error
    review_mode: str = "single"  # 审查模式（single/double）；队列行与详情页展示
    grade: str | None = None  # 报告评级（pass/conditional_pass/fail，done 后有值）
    gate_payload: dict | None = None  # 闸口待审载荷（gate 状态有值）
    risk_count: int | None = None  # 风险数（done=报告风险数，gate=待审 high 数）
    template: bool = False  # 是否疑似空白模板（前端评级文案用"待确认"）
    error: str | None = None  # 失败原因（error 状态有值）


class TaskDetailOut(TaskSummaryOut):
    """任务详情响应: 摘要 + 完整报告.

    report 在 done/审批后才有值, gate 时为空.
    """

    report: dict | None = None  # 最终报告（报告结构随版本演化，暂不逐字段建模）


class TaskListOut(BaseModel):
    """任务列表响应: 摘要数组 + 当前并发上限(队列页轮询一次拿全)."""

    tasks: list[TaskSummaryOut]
    concurrency: int


def _summary(record) -> TaskSummaryOut:
    """把内部 TaskRecord 清洗成前端安全的 TaskSummaryOut 模型.

    被 list_tasks / get_task / _resume_or_409 调用.
    """
    # 风险数：done 看报告 risks（疑似空白模板是"结论"不是风险，排除）；
    # gate 看待审 high；其余无
    if record.report:
        risk_count = len(
            [r for r in (record.report.get("risks") or []) if r.get("risk_type") != "blank_template_suspected"]
        )
    elif record.gate_payload:
        risk_count = len(record.gate_payload.get("high_risks") or [])
    else:
        risk_count = None
    # 模板结论标记：报告含 blank_template_suspected → 前端评级文案用"待确认"
    is_template = bool(record.report) and any(
        r.get("risk_type") == "blank_template_suspected" for r in (record.report.get("risks") or [])
    )
    return TaskSummaryOut(
        thread_id=record.thread_id,
        # 展示用原始文件名（name）；兼容旧记录回退到路径 basename
        source=(record.name or Path(record.source).name) if (record.name or record.source) else "",
        status=record.status,
        review_mode=record.review_mode,
        grade=(record.report or {}).get("grade") if record.report else None,
        gate_payload=record.gate_payload,
        risk_count=risk_count,
        template=is_template,
        error=record.error,
    )


def _source_kind(source: str) -> str:
    """来源类别: 内置样本(sample)还是用户上传(upload).

    判断依据: 登记路径是否落在 data/contracts 下. 由 get_task_source 调用.
    """
    return "sample" if str(Path(source).resolve()).startswith(str(CONTRACTS_DIR.resolve())) else "upload"


def _clause_blocks(text: str) -> list[dict]:
    """全文 -> 条款块列表.

    前端原文抽屉按块渲染, 块标题留作风险证据回指锚点. 由 get_task_source 调用.
    """
    clauses = split_clauses(text)
    # 这种情况是：全文无「第X条/章节」结构 → 整篇当作一块，仍可展示与回指
    if not clauses:
        return [{"ref": "", "title": "全文", "text": text.strip()}] if text.strip() else []
    return [{"ref": c.ref, "title": c.title, "text": c.text} for c in clauses]


# ---- 上传与队列查询 ----


def _validated_mode(review_mode: str) -> str:
    """审查模式入参校验：只认 single/double（parallel 未实现，拒绝防误解）。"""
    # 这种情况是：前端/调用方传了没实现的模式 → 400 明确提示
    if review_mode not in ("single", "double"):
        raise HTTPException(status_code=400, detail=f"不支持的审查模式：{review_mode}（仅 single/double）")
    return review_mode


@router.post("/tasks")
async def upload_task(
    file: UploadFile,
    review_mode: str = Form("single"),
    manager: TaskManager = Depends(get_manager),
) -> dict:
    """上传合同并登记审查任务.

    什么时候用: 用户在前端选文件上传. 返回 thread_id 供轮询任务进度.
    review_mode: single=主审 / double=主审+独立盲审复核（多一次 LLM 调用）。
    """
    review_mode = _validated_mode(review_mode)
    suffix = Path(file.filename or "").suffix.lower()
    # 分支：后缀不在白名单 → 400 明确提示（防任意文件写入）
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持 {suffix or '空'} 格式，请上传 PDF/Word/文本/图片",
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 两段式登记：先建任务拿 thread_id（落盘文件名用），再补 source 并入队——
    # 落盘路径依赖 thread_id，不能像 submit 那样一步到位
    thread_id = manager.register(file.filename or "contract", review_mode=review_mode)
    target = UPLOAD_DIR / f"{thread_id}{suffix}"
    content = await file.read()
    target.write_bytes(content)
    # 登记簿 source 补成落盘路径（worker 取盘解析）
    manager.runner.store.update(thread_id, source=str(target))
    manager.enqueue(thread_id)
    return {"thread_id": thread_id, "status": "pending", "review_mode": review_mode}


@router.get("/tasks", response_model=TaskListOut)
def list_tasks(manager: TaskManager = Depends(get_manager)) -> dict:
    """列全部任务摘要与当前并发数(队列页轮询用)."""
    records = manager.runner.store.list_records()
    # worker_count 用 getattr 兜底：测试注入的假 manager 可能缺该属性，默认 1
    return {"tasks": [_summary(r) for r in records], "concurrency": getattr(manager, "worker_count", 1)}


@router.delete("/tasks/{thread_id}")
def delete_task(thread_id: str, manager: TaskManager = Depends(get_manager)) -> dict:
    """删除任务: 登记簿记录 + 检查点线程状态 + 上传落盘文件(不可恢复)。

    什么时候用: 队列页清理历史任务(任务列表只增不减)。
    口径: done/error/gate 可删(删 gate 即放弃待审批); pending/processing 409,
    防删到一半的文件/状态; 任务不存在 404。
    """
    reason = _delete_one(manager, thread_id)
    if reason is not None:
        code = 404 if reason == "任务不存在" else 409
        raise HTTPException(status_code=code, detail=reason)
    return {"deleted": thread_id}


@router.post("/tasks/batch-delete")
def batch_delete(
    payload: BatchDeleteIn, manager: TaskManager = Depends(get_manager)
) -> dict:
    """批量删除任务(单条失败不阻断整批, 结果汇总返回)。

    口径与单删一致: 处理中的任务跳过并说明; 已删除/重复 id 去重。
    """
    deleted: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for thread_id in payload.thread_ids:
        # 这种情况是: 同一批里重复传了同一 id → 只处理一次
        if thread_id in seen:
            continue
        seen.add(thread_id)
        reason = _delete_one(manager, thread_id)
        if reason is None:
            deleted.append(thread_id)
        else:
            skipped.append({"thread_id": thread_id, "reason": reason})
    return {"deleted": deleted, "skipped": skipped}


class SamplesIn(BaseModel):
    """样本批量入队入参: 取 data/contracts 前 N 份合成样本直接入队.

    什么时候用: 回归测试与 Phase 4 评测需要免上传直接跑内置样本;
    前端无此入口(一键演示入口已移除).
    """

    count: int = Field(default=3, ge=1, le=9, description="内置 sample_*.md 取前 N 份")
    review_mode: str = "single"  # 审查模式（single/double）；回归/评测可按需选双审


@router.post("/tasks/samples")
def enqueue_samples(
    body: SamplesIn,
    manager: TaskManager = Depends(get_manager),
) -> dict:
    """把内置合成样本直接入队(跳过上传步骤).

    什么时候用: 回归测试与 Phase 4 评测需要免上传跑内置样本; 前端无入口.
    """
    samples = sorted((BASE_DIR / "data" / "contracts").glob("sample_*.md"))[: body.count]
    review_mode = _validated_mode(body.review_mode)
    # 这种情况是：本地样本缺失（生成器没跑过）→ 明确 404 提示先生成
    if not samples:
        raise HTTPException(status_code=404, detail="data/contracts 下没有 sample_*.md，请先运行样本生成器")
    queued: list[dict] = []
    for path in samples:
        tid = manager.register(path.name, review_mode=review_mode)
        # register 已把文件名存进 name（展示用），这里再补落盘 source
        manager.runner.store.update(tid, source=str(path))
        manager.enqueue(tid)
        queued.append({"thread_id": tid, "source": path.name, "review_mode": review_mode})
    return {"tasks": queued}


@router.get("/tasks/{thread_id}", response_model=TaskDetailOut)
def get_task(
    thread_id: str,
    manager: TaskManager = Depends(get_manager),
) -> dict:
    """返回单个任务的摘要与最终报告(详情页/单任务轮询用)."""
    record = manager.runner.store.get(thread_id)
    # 这种情况是：thread_id 不存在 → 404
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {thread_id}")
    # _summary 返回 Pydantic 模型，拼额外字段前需 model_dump()（v2 不支持 ** 解包模型）
    return {**_summary(record).model_dump(), "report": record.report}


@router.get("/tasks/{thread_id}/source")
def get_task_source(
    thread_id: str,
    manager: TaskManager = Depends(get_manager),
) -> dict:
    """前端查看原合同时: 返回解析后的纯文本+条款块列表(供证据高亮锚点定位)
    """
    record = manager.runner.store.get(thread_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {thread_id}")
    text = record.source_text or ""
    return {
        "thread_id": thread_id,
        "name": (record.name or Path(record.source).name) if (record.name or record.source) else "",
        "suffix": Path(record.source).suffix.lower() if record.source else "",
        "kind": _source_kind(record.source) if record.source else "upload",
        "file_available": bool(record.source and Path(record.source).is_file()),
        "text": text,
        "blocks": _clause_blocks(text),
    }


@router.get("/tasks/{thread_id}/file")
def get_task_file(
    thread_id: str,
    manager: TaskManager = Depends(get_manager),
) -> FileResponse:
    """用户点击下载按钮/PDF内嵌预览: 返回原始二进制文件(PDF inline, 其余 attachment)
    """
    record = manager.runner.store.get(thread_id)
    # 这种情况是：任务不存在或登记路径没落盘文件 → 404（临时文件可能已被清理）
    if record is None or not record.source or not Path(record.source).is_file():
        raise HTTPException(status_code=404, detail=f"原文件不存在: {thread_id}")
    path = Path(record.source)
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    return FileResponse(
        path,
        media_type=media_type,
        filename=record.name or path.name,
        content_disposition_type="inline" if path.suffix.lower() == ".pdf" else "attachment",
    )


def _resume_or_409(manager: TaskManager, thread_id: str, answer: dict) -> dict:
    """审批状态守卫 + resume 调用封装.

    仅 gate 状态可续跑, 否则 409; 被 approve/reject/edit 三个路由共用.
    """
    record = manager.runner.store.get(thread_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {thread_id}")
    # 这种情况是：任务不在待审批闸口 → 拒绝续跑并说明当前状态
    if record.status != "gate":
        raise HTTPException(status_code=409, detail=f"任务当前状态为 {record.status}，不在待审批")
    manager.runner.resume(thread_id, **answer)
    updated = manager.runner.store.get(thread_id)
    # 同 get_task：Pydantic 模型拼额外字段前需 model_dump()
    return {**_summary(updated).model_dump(), "report": updated.report}


# ---- 审批三动作：都走 _resume_or_409（仅 gate 状态可续跑）----


@router.post("/tasks/{thread_id}/approve", response_model=TaskDetailOut)
def approve_task(
    thread_id: str,
    body: ApprovalIn,
    manager: TaskManager = Depends(get_manager),
) -> dict:
    """审批-放行: 风险留档但人工确认可接受."""
    return _resume_or_409(manager, thread_id, {"action": "approved", "note": body.note})


@router.post("/tasks/{thread_id}/reject", response_model=TaskDetailOut)
def reject_task(
    thread_id: str,
    body: ApprovalIn,
    manager: TaskManager = Depends(get_manager),
) -> dict:
    """审批-打回: 附原因写进报告留痕."""
    return _resume_or_409(manager, thread_id, {"action": "rejected", "note": body.note})


@router.post("/tasks/{thread_id}/edit", response_model=TaskDetailOut)
def edit_task(
    thread_id: str,
    body: EditIn,
    manager: TaskManager = Depends(get_manager),
) -> dict:
    """审批-编辑重审: 按字段补丁回 rules 重算.

    补丁后仍有 high 会再次停闸口.
    """
    return _resume_or_409(
        manager, thread_id, {"action": "edited", "note": body.note, "patches": body.patches}
    )
