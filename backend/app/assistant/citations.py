"""引用：把工具真正返回过的记录汇总成本轮引用，并做接地校验（纯函数，离线可测）。

工具用 `response_format="content_and_artifact"` 返回，结构化记录挂在 ToolMessage 的
artifact 上，所以"哪些条目出现过"有确定出处——引用是它的投影，模型复述不出不存在的引用。
另一头的护栏是"无法核实"：回答里提到、却谁都没给过的政策编号，标出来交给前端提示。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from langchain_core.documents import Document
from langchain_core.messages import BaseMessage, ToolMessage

# 引用栏上限：芯片放不下更多；超出的引用仍留在回答正文里
MAX_CITATIONS = 8
# 单条引用正文上限：读政策全文可能上千字，芯片要能展开也要防载荷过大
_CITATION_TEXT_LIMIT = 2000
# 回答里提到的政策编号：模型会写"P-14 第三条"或小写，按编号形态取值再统一成大写
_POLICY_MENTION_RE = re.compile(r"P-\d+", re.IGNORECASE)


@dataclass(frozen=True)
class Citation:
    """一条引用：政策库条文或合同条款。"""

    kind: str  # policy=政策库条文 / clause=合同条款
    ref: str  # 引用编号：政策 P-XX / 条款号（如"第五条"）
    title: str = ""  # 条文标题 / 条款标题（芯片标题位）
    text: str = ""  # 引用正文（芯片预览与展开）
    source: str = ""  # 政策来源文件名（合同条款引用留空）
    origin: str = "tool"  # tool=这轮工具检索到的 / report=报告里本来就有的这条依据

    def to_dict(self) -> dict:
        """JSON 形态：键固定存在，前端与流式事件都不必判空。"""
        return {
            "kind": self.kind,
            "ref": self.ref,
            "title": self.title,
            "text": self.text,
            "source": self.source,
            "origin": self.origin,
        }


def mentioned_policy_refs(text: str) -> list[str]:
    """回答文本里提到过的政策编号（去重、按首次出现顺序、统一大写）。"""
    seen: dict[str, None] = {}
    for match in _POLICY_MENTION_RE.finditer(text or ""):
        seen.setdefault(match.group(0).upper(), None)
    return list(seen)


def clip_text(text: str, limit: int = _CITATION_TEXT_LIMIT) -> str:
    """引用正文限长（读政策全文上千字，芯片展开也要防载荷过大）。"""
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def citation_from(item: Any) -> Citation | None:
    """工具返回记录里的单条 → 引用；认不出形态（没有编号）时返回 None。

    政策检索工具走框架的 create_retriever_tool，返回的是 Document（编号在 metadata）；
    自写工具按下面的 dict 约定返回。检查点回读时 Document 已序列化成普通 dict
    （page_content + metadata 两个键），按同一套字段认，否则刷新页面后检索来的芯片会丢。
    """
    # 分支：检索命中的 Document → 编号/来源取 metadata
    if isinstance(item, Document):
        ref = str(item.metadata.get("policy_ref") or "")
        if not ref:
            return None
        return Citation(
            kind="policy",
            ref=ref,
            title=str(item.metadata.get("title") or ""),
            text=clip_text(item.page_content),
            source=str(item.metadata.get("source") or ""),
        )
    # 分支：序列化后的 Document（从检查点读回来的形态）→ 字段仍在 metadata 里
    if isinstance(item, dict) and "page_content" in item:
        meta = item.get("metadata") or {}
        ref = str(meta.get("policy_ref") or "")
        if not ref:
            return None
        return Citation(
            kind="policy",
            ref=ref,
            title=str(meta.get("title") or ""),
            text=clip_text(str(item.get("page_content") or "")),
            source=str(meta.get("source") or ""),
        )
    # 分支：自写工具返回的引用 dict
    if isinstance(item, dict):
        ref = str(item.get("ref") or "")
        if not ref:
            return None
        return Citation(
            kind=str(item.get("kind") or "policy"),
            ref=ref,
            title=str(item.get("title") or ""),
            text=clip_text(str(item.get("text") or "")),
            source=str(item.get("source") or ""),
        )
    return None


def artifact_items(artifact: Any) -> list[Any]:
    """工具返回记录 → 条目列表：单条与列表都接受，没带记录时为空。"""
    if artifact is None:
        return []
    if isinstance(artifact, (list, tuple)):
        return list(artifact)
    return [artifact]


def collect_citations(messages: Sequence[BaseMessage] | None) -> list[Citation]:
    """汇总这轮工具真正返回过的引用（按返回顺序，同一编号只留一条）。

    只看 ToolMessage 的 artifact——那是工具实际返回给框架的记录。用户消息与模型消息
    一概不看，所以模型在回答里写出的引用不会变成芯片。同一个编号命中多段条文时合并成
    一条（正文拼起来），否则"找条款"命中同一条两次就会出两个同名芯片。
    """
    out: list[Citation] = []
    index: dict[tuple[str, str], int] = {}
    for message in messages or []:
        # 分支：只认工具返回的消息，其余（用户/模型/系统）不含工具返回记录
        if not isinstance(message, ToolMessage):
            continue
        for item in artifact_items(getattr(message, "artifact", None)):
            citation = citation_from(item)
            if citation is None:
                continue
            key = (citation.kind, citation.ref)
            # 分支：这个编号已经收过 → 合并正文，不再新增芯片
            if key in index:
                out[index[key]] = _merge_citations(out[index[key]], citation)
                continue
            index[key] = len(out)
            out.append(citation)
    return out[:MAX_CITATIONS]


def _merge_citations(first: Citation, another: Citation) -> Citation:
    """同一编号的两次引用合成一条：标题取先有的，正文不重不漏地拼起来。"""
    text = first.text
    # 分支：这段正文已经包含在已有引用里（重复返回）→ 不再拼接
    if another.text and another.text not in text:
        text = f"{text}\n\n{another.text}" if text else another.text
    return Citation(
        kind=first.kind,
        ref=first.ref,
        title=first.title or another.title,
        text=text,
        source=first.source or another.source,
        origin=first.origin,
    )


def summarize_citations(
    messages: Sequence[BaseMessage] | None,
    answer: str = "",
    declared_refs: Iterable[str] = (),
    declared_hits: dict[str, dict] | None = None,
) -> dict:
    """回答后的引用汇总：本轮引用 + 回答里查不到出处的政策编号。

    返回 {"citations": [...], "unverified": [...]}。citations 除了这轮工具返回过的条目，
    还会为"回答里提到、报告里本来就有原文"的政策编号补一条芯片（origin=report）——
    模型常直接拿上下文回答而不检索，若不补，用户就会看到正文写着"依据 P-03"却点不到条文。
    unverified 只装"既不在工具返回记录、也不在报告已声明依据里"的编号：那才是模型自己编的政策。
    """
    citations = collect_citations(messages)
    tool_refs = {c.ref.upper() for c in citations if c.kind == "policy"}
    hits = declared_hits or {}
    mentioned = mentioned_policy_refs(answer)
    for ref in mentioned:
        # 分支：这轮检索到过（已有芯片）或报告里没有这条依据 → 不补
        if ref in tool_refs or ref not in hits:
            continue
        info = hits[ref]
        citations.append(
            Citation(
                kind="policy",
                ref=ref,
                title=str(info.get("title") or ""),
                text=clip_text(str(info.get("text") or "")),
                origin="report",
            )
        )
        tool_refs.add(ref)
    grounded = set(tool_refs)
    grounded.update(str(ref).upper() for ref in declared_refs if ref)
    unverified = [ref for ref in mentioned if ref not in grounded]
    return {"citations": [c.to_dict() for c in citations[:MAX_CITATIONS]], "unverified": unverified}
