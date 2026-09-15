"""政策库只读起稿接口：上传或粘贴一份新政策 → 规范化草稿 + 重叠分级 + 冲突初筛 + 配套清单。

只起稿、不改库：不写向量库、不写 data/policies/*.md，产物只落 data/policies/_drafts/；
入库仍走 `policy_admin --sync`（前端"批准入库"是后续单独一批）。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.app import policy_assistant
from backend.app.parser import extract_text

router = APIRouter(prefix="/api/policy", tags=["policy"])

# 起稿接受的输入格式：政策是正式文本，不收图片（扫描件请先转文本再传）
ALLOWED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}
# 粘贴文本的兜底来源名：编号从文件名前缀解析，兜底名不带编号时按正文里的「文件编号」认
PASTED_NAME = "粘贴政策.md"
# 上传原件暂存目录：解析完即删，草稿目录里只留起稿产物
INCOMING_DIR = policy_assistant.DRAFTS_DIR / "_incoming"


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
            return extract_text(temp), _source_name(file.filename)
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
    result = policy_assistant.write_draft(content, source=source)
    parsed = result["parsed"]
    return {
        "draft_id": result["draft_id"],
        "source": source,
        "ref": parsed["ref"],
        "title": parsed["title"],
        "articles": len(parsed["articles"]),
        "missing": parsed["missing"],
        "overlap": _overlap_counts(result["overlaps"]),
        "conflicts": result["conflicts"],
    }


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str) -> dict:
    """草稿详情：回读磁盘产物（草稿全文 / 重叠分级 / 冲突 / 配套清单）。"""
    detail = policy_assistant.load_draft(draft_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="草稿不存在或已被清理")
    return detail
