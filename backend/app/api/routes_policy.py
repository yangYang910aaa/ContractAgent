"""政策库只读起稿接口：上传或粘贴一份新政策 → 规范化草稿 + 重叠分级 + 冲突初筛 + 配套清单。

只起稿、不改库：不写向量库、不写 data/policies/*.md，产物只落 data/policies/_drafts/；
入库仍走 `python -m backend.app.policy.admin --sync`（前端"批准入库"是后续单独一批）。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.app.policy import drafts, drafter, publish
from backend.app.review.parser import extract_text
from backend.app.policy.corpus import corpus_fingerprint
from backend.app.policy.rag import POLICY_DIR, get_store
from backend.app.usage import track_usage

router = APIRouter(prefix="/api/policy", tags=["policy"])

# 起稿接受的输入格式：政策是正式文本，不收图片（扫描件请先转文本再传）
ALLOWED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}
# 粘贴文本的兜底来源名：编号从文件名前缀解析，兜底名不带编号时按正文里的「文件编号」认
PASTED_NAME = "粘贴政策.md"
# 上传原件暂存目录：解析完即删，草稿目录里只留起稿产物
INCOMING_DIR = drafts.DRAFTS_DIR / "_incoming"


def _policy_dir() -> Path:
    """语料目录：单独成函数，演练与测试可以指到副本目录。"""
    return POLICY_DIR


def _publish_store():
    """入库用的检索库（Milvus 优先，连不上时内存兜底）：同样留出注入位置。"""
    return get_store()


def _drafter():
    """模型起草器：默认真调模型；单独成函数便于离线测试注入假起草器。"""
    return None


class PublishIn(BaseModel):
    """入库入参：confirm=false 只算计划；content 留空就用草稿原文。"""

    file_name: str = Field(default="", description="入库文件名，如 P-16_解除与善后.md")
    content: str = Field(default="", description="人工修订后的正文（留空则用草稿原文）")
    confirm: bool = Field(default=False, description="false=只预览计划；true=落盘并同步入库")
    allow_missing_meta: bool = Field(default=False, description="缺元信息时是否仍入库")


def _draft_summary(result: dict) -> dict:
    """起稿产物 → 页面摘要（人工起稿与模型起草共用一份口径）。"""
    parsed = result["parsed"]
    return {
        "draft_id": result["draft_id"],
        "source": parsed["source"],
        "ref": parsed["ref"],
        "title": parsed["title"],
        "articles": len(parsed["articles"]),
        "missing": parsed["missing"],
        "suggested_file": drafts.suggest_file_name(parsed),
        "overlap": _overlap_counts(result["overlaps"]),
        "conflicts": result["conflicts"],
    }


def _overlap_counts(overlaps: list[dict]) -> dict:
    """按等级数一遍重叠条目，供页面显示"几处高、几处中"。"""
    counts = {"high": 0, "medium": 0, "low": 0}
    for item in overlaps:
        level = item.get("level", "low")
        counts[level] = counts.get(level, 0) + 1
    return counts


def _source_name(filename: str) -> str:
    """来源名只取文件名部分：防把调用方给的多级路径写进草稿目录与 meta。"""
    name = Path(filename).name
    return name if Path(name).suffix else f"{name}.md"


async def _read_input(file: UploadFile | None, text: str, name: str) -> tuple[str, str]:
    """取待起稿的正文与来源名：文件走解析器，粘贴文本直接用；两者都空则 400。"""
    # 这种情况是：传了文件 → 后缀白名单 + 落临时文件走解析器（pdf/docx 要按格式解析）
    if file is not None and file.filename:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"不支持 {suffix or '空'} 格式，请上传 md / txt / pdf / docx",
            )
        INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        temp = INCOMING_DIR / f"{uuid4().hex}{suffix}"
        try:
            temp.write_bytes(await file.read())
            # 解析（含 OCR）是阻塞活：丢到线程池，别占住事件循环
            return await run_in_threadpool(extract_text, temp), _source_name(file.filename)
        finally:
            temp.unlink(missing_ok=True)  # 原件不留库：草稿目录只存起稿产物
            # 暂存目录空了就一并删掉，免得草稿目录里留个空壳
            try:
                INCOMING_DIR.rmdir()
            except OSError:
                pass
    # 这种情况是：粘贴文本 → 直接用，来源名取调用方给的标题（没有就用兜底名）
    if text.strip():
        return text, _source_name(name.strip() or PASTED_NAME)
    raise HTTPException(status_code=400, detail="请上传政策文件或粘贴政策正文")


@router.post("/drafts")
async def create_draft(
    file: UploadFile | None = File(None),
    text: str = Form(""),
    name: str = Form(""),
) -> dict:
    """起稿：解析 + 重叠分级 + 冲突初筛，产物落盘后返回摘要（不写政策库）。"""
    content, source = await _read_input(file, text, name)
    # 这种情况是：读不出正文（空文件/加密 PDF）→ 400，不落一份空草稿
    if not content.strip():
        raise HTTPException(status_code=400, detail="没有读到政策正文（文件为空或无法解析）")
    # 起稿里的逐条向量检索是阻塞活（每条一次 embedding 往返）→ 同样丢线程池
    result = await run_in_threadpool(drafts.write_draft, content, source)
    return _draft_summary(result)


@router.post("/ai-drafts")
def create_ai_draft(
    brief: str = Form(..., description="需求或半成品条文；模型据此按体例起草"),
    ref: str = Form("", description="政策编号（如 P-16）；留空则草稿里写「待填」"),
    group: str = Form("", description="归口部门（可选）"),
    effective_date: str = Form("", description="生效日期（可选）"),
) -> dict:
    """模型起草：按需求写条文 + 配解释与判定要点，产出照旧走起稿管线（重叠/冲突/清单）。

    与人工起稿的区别只在正文来源；模型不自动入库，成文里的新数字会单独列出来等人确认。
    """
    # 这种情况是：需求为空 → 400 明确提示，别白花一次调用
    if not brief.strip():
        raise HTTPException(status_code=400, detail="请先写清要起草什么（需求或要点）")
    with track_usage() as usage:
        result = drafter.draft_policy(
            brief.strip(),
            ref=ref.strip(),
            group=group.strip(),
            effective_date=effective_date.strip(),
            drafter=_drafter(),
        )
    summary = _draft_summary(result)
    detail = drafts.load_draft(result["draft_id"]) or {}
    summary["origin"] = "ai"
    summary["new_numbers"] = (detail.get("ai") or {}).get("new_numbers", [])
    summary["suggestions"] = (detail.get("ai") or {}).get("suggestions", {})
    # 用量：页面显示"这份草稿花了几次调用"，与报告的成本口径一致
    summary["llm"] = usage.to_dict()
    return summary


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str) -> dict:
    """草稿详情：回读磁盘产物（草稿全文 / 重叠分级 / 冲突 / 配套清单）。"""
    detail = drafts.load_draft(draft_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="草稿不存在或已被清理")
    return detail


@router.get("/library")
def get_library() -> dict:
    """当前政策库：版本号 + 逐份清单（只读盘，0 次模型调用）。"""
    fingerprint = corpus_fingerprint(policy_dir=_policy_dir())
    return {
        "version": fingerprint["version"],
        "files": fingerprint["files"],
        "units": fingerprint["units"],
        "documents": [
            {
                "ref": doc["ref"],
                "source": doc["source"],
                "title": doc["title"],
                "version": doc["version"],
                "effective_date": doc["effective_date"],
                "units": doc["units"],
            }
            for doc in fingerprint["documents"]
        ],
    }


@router.post("/drafts/{draft_id}/apply")
def apply_draft(draft_id: str, payload: PublishIn) -> dict:
    """入库：confirm=false 只算计划，confirm=true 落盘 + 按单元同步 + 执行后核对。

    这是整页唯一会改真库的动作（改 data/policies/ 与向量库）；核对不过或中途报错会自动退回。
    """
    draft = drafts.load_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在或已被清理")
    # 分支：调用方没给正文 → 用在页面上过的那份草稿（含按体例重排的结果）
    content = payload.content.strip() or draft["draft"]
    file_name = payload.file_name.strip() or draft.get("suggested_file", "")
    # 分支：预览 → 只返回计划，落盘与库都不动
    if not payload.confirm:
        try:
            plan = publish.plan_publish(
                content, file_name, policy_dir=_policy_dir(), store=_publish_store()
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"读不到检索库，无法核算入库改动：{exc}") from exc
        return {"applied": False, "plan": plan}

    try:
        result = publish.publish(
            content,
            file_name,
            policy_dir=_policy_dir(),
            store=_publish_store(),
            allow_missing_meta=payload.allow_missing_meta,
        )
    except publish.PublishBlocked as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    drafts.mark_applied(draft_id, result)  # 给这份草稿记上"已入库"
    return result
