"""FastAPI 任务路由：上传/队列/详情/审批

约定：所有任务状态都写在 manager.runner.store(ThreadStore)，路由层只做
校验 + 转译，不直接碰图。审批动作 approve/reject/edit 复用 ReviewRunner.resume。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.app.config import BASE_DIR
from backend.app.parser import split_clauses
from backend.app.tasks import TaskManager

router = APIRouter(prefix="/api", tags=["tasks"])

# 上传白名单：文本型合同（扫描件无文字层，服务端 parser 会明确报错）
ALLOWED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
CONTRACTS_DIR = BASE_DIR / "data" / "contracts"  # 内置演示样本所在目录（kind 判断用）

# 原文件下载时的 Content-Type：docx/pdf 给浏览器可识别的类型（pdf 可内嵌预览）
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def get_manager(request: Request) -> TaskManager:
    """取 app.state 上的任务管理器（测试可替换成 worker=False 的假 runner 实例）。"""
    return request.app.state.manager


class ApprovalIn(BaseModel):
    """审批入参：动作差异在路由，这里只收意见。"""

    note: str = Field(default="", description="审批意见（打回必填原因，前端提示）")


class EditIn(BaseModel):
    """编辑重审入参：字段补丁（键=ContractModel 字段名，值=目标值）。"""

    patches: dict = Field(description="字段补丁，如 {'warranty_months': 24}")
    note: str = Field(default="", description="修改说明")


def _summary(record) -> dict:
    """TaskRecord → 列表/详情用的精简 dict(不外泄内部对象)"""
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
    return {
        "thread_id": record.thread_id,
        # 展示用原始文件名（name）；兼容旧记录回退到路径 basename
        "source": (record.name or Path(record.source).name) if (record.name or record.source) else "",
        "status": record.status,
        "grade": (record.report or {}).get("grade") if record.report else None,
        "gate_payload": record.gate_payload,
        "risk_count": risk_count,
        "template": is_template,
        "error": record.error,
    }


def _source_kind(source: str) -> str:
    """来源类别：登记路径在 data/contracts 下的是内置演示样本，否则算用户上传。

    用解析后的绝对路径前缀判断（demo 接口登记的是样本绝对路径）。
    """
    return "sample" if str(Path(source).resolve()).startswith(str(CONTRACTS_DIR.resolve())) else "upload"


def _clause_blocks(text: str) -> list[dict]:
    """全文 → 条款块列表（前端原文抽屉按块渲染，块标题留作证据回指锚点）。"""
    clauses = split_clauses(text)
    # 这种情况是：全文无「第X条/章节」结构 → 整篇当作一块，仍可展示与回指
    if not clauses:
        return [{"ref": "", "title": "全文", "text": text.strip()}] if text.strip() else []
    return [{"ref": c.ref, "title": c.title, "text": c.text} for c in clauses]


# ---- 上传与队列查询 ----


@router.post("/tasks")
async def upload_task(request: Request, file: UploadFile) -> dict:
    """上传一份合同 → 登记任务入队，返回 thread_id（审核在 worker 后台跑）。"""
    manager = get_manager(request)
    suffix = Path(file.filename or "").suffix.lower()
    # 分支：后缀不在白名单 → 400 明确提示（防任意文件写入）
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持 {suffix or '空'} 格式，请上传 PDF/Word/文本")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    
    # 两段式登记：先建任务拿 thread_id（落盘文件名用），再补 source 并入队——
    # 落盘路径依赖 thread_id，不能像 submit 那样一步到位（易错点）
    thread_id = manager.register(file.filename or "contract")
    target = UPLOAD_DIR / f"{thread_id}{suffix}"
    content = await file.read()
    target.write_bytes(content)
    # 登记簿 source 补成落盘路径（worker 取盘解析）
    manager.runner.store.update(thread_id, source=str(target))
    manager.enqueue(thread_id)
    return {"thread_id": thread_id, "status": "pending"}


@router.get("/tasks")
def list_tasks(request: Request) -> dict:
    """任务队列列表（倒序，供队列页轮询）；附并发上限（前端展示"并发 n"）。"""
    manager = get_manager(request)
    records = manager.runner.store.list_records()
    return {"tasks": [_summary(r) for r in records], "concurrency": manager.worker_count}


class DemoIn(BaseModel):
    """一键演示入参：队列内置合成样本的份数（剧本 3 批量演示用）。"""

    count: int = Field(default=3, ge=1, le=9, description="内置 sample_*.md 取前 N 份")


@router.post("/tasks/demo")
def enqueue_demo(body: DemoIn, request: Request) -> dict:
    """把 data/contracts 的内置合成样本直接入队（免上传，演示/验收提速）。

    只入队本地合成语料，不接收任意路径（红线：真实合同不进服务队列）。
    """
    manager = get_manager(request)
    samples = sorted((BASE_DIR / "data" / "contracts").glob("sample_*.md"))[: body.count]
    # 这种情况是：本地样本缺失（生成器没跑过）→ 明确 404 提示先生成
    if not samples:
        raise HTTPException(status_code=404, detail="data/contracts 下没有 sample_*.md，请先运行样本生成器")
    queued: list[dict] = []
    for path in samples:
        tid = manager.register(path.name)
        # register 已把文件名存进 name（展示用），这里再补落盘 source
        manager.runner.store.update(tid, source=str(path))
        manager.enqueue(tid)
        queued.append({"thread_id": tid, "source": path.name})
    return {"tasks": queued}


@router.get("/tasks/{thread_id}")
def get_task(thread_id: str, request: Request) -> dict:
    """任务详情：状态 + 闸口载荷 + 最终报告（报告在 done 后有值）。"""
    manager = get_manager(request)
    record = manager.runner.store.get(thread_id)
    # 这种情况是：thread_id 不存在 → 404
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {thread_id}")
    return {**_summary(record), "report": record.report}


@router.get("/tasks/{thread_id}/source")
def get_task_source(thread_id: str, request: Request) -> dict:
    """任务原合同：解析后的全文 + 条款块（U2 任务页「查看原合同」的数据源）。

    text 在任务跑过 parse（status 离开 pending）后才有值；源文件本体由
    GET /tasks/{id}/file 提供（docx/pdf 预览/下载）。blocks 用于前端按
    条款块渲染并给证据高亮留锚点（块标题 ≈ 风险项 clause_ref）。
    """
    manager = get_manager(request)
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
def get_task_file(thread_id: str, request: Request) -> FileResponse:
    """原文件下载/预览：上传的 docx/pdf 或内置样本 md，按后缀定 Content-Type。

    pdf 用 inline（浏览器内嵌预览）；其余默认 attachment（下载）——否则
    前端 iframe 预览 pdf 会变成直接下载（2026-09-05 用户实测反馈）。
    """
    manager = get_manager(request)
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


def _resume_or_409(request: Request, thread_id: str, answer: dict) -> dict:
    """公共审批执行：仅 gate 状态可续跑，否则 409（防对完成/排队任务误操作）。"""
    manager = get_manager(request)
    record = manager.runner.store.get(thread_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {thread_id}")
    # 这种情况是：任务不在待审批闸口 → 拒绝续跑并说明当前状态
    if record.status != "gate":
        raise HTTPException(status_code=409, detail=f"任务当前状态为 {record.status}，不在待审批")
    manager.runner.resume(thread_id, **answer)
    updated = manager.runner.store.get(thread_id)
    return {**_summary(updated), "report": updated.report}


# ---- 审批三动作：都走 _resume_or_409（仅 gate 状态可续跑）----


@router.post("/tasks/{thread_id}/approve")
def approve_task(thread_id: str, body: ApprovalIn, request: Request) -> dict:
    """审批-放行：风险留档但人工确认可接受。"""
    return _resume_or_409(request, thread_id, {"action": "approved", "note": body.note})


@router.post("/tasks/{thread_id}/reject")
def reject_task(thread_id: str, body: ApprovalIn, request: Request) -> dict:
    """审批-打回：附原因写进报告（剧本 2 演示路径）。"""
    return _resume_or_409(request, thread_id, {"action": "rejected", "note": body.note})


@router.post("/tasks/{thread_id}/edit")
def edit_task(thread_id: str, body: EditIn, request: Request) -> dict:
    """审批-编辑重审：按字段补丁回 rules 重算（仍 high 会再次停闸口）。"""
    return _resume_or_409(
        request, thread_id, {"action": "edited", "note": body.note, "patches": body.patches}
    )
