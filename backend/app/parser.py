"""合同解析与切分。

职责：文件 → 全文文本；全文 → 按「第X条」切条款(条款成块不截断,超长条款带条款头续切,无条文结构退回句子级通用切分)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 条款头：行首的「第X条[标题]」，X 支持阿拉伯数字与中文数字
_CLAUSE_HEADER_RE = re.compile(r"(?m)^\s*(第[0-9一二三四五六七八九十百千万零〇]+条[^\n]*)")


@dataclass
class Clause:
    """一个条款块: ref=条款号（如"第一条"), title=条款头整行, text=含条款头的全文。"""

    ref: str  # 条款号（如"第一条"）
    title: str  # 条款头整行
    text: str  # 含条款头的全文


def extract_text(path: str | Path) -> str:
    """读取 PDF / Word / 文本文件为全文(PDF 扫描件暂不支持，属 P2 OCR)"""
    path = Path(path)
    suffix = path.suffix.lower()
    # 分支 1：纯文本类（.txt/.md）→ UTF-8 直读全文
    if suffix in {".txt", ".md", ".markdown"}:
        return path.read_text(encoding="utf-8", errors="replace")
    # 分支 2：PDF → pypdf 逐页抽取文本（扫描件无文本层，不在此范围）
    if suffix == ".pdf":
        # 延迟导入：只在真遇到 PDF 时拉 pypdf，避免拖慢纯文本路径
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    # 分支 3：Word → python-docx 按段落取文本
    if suffix == ".docx":
        from docx import Document  # 延迟导入，同 pypdf

        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    # 分支 4：其他后缀 → 明确报不支持，提示可上传格式
    raise ValueError(f"暂不支持 {suffix} 格式，请上传 PDF / Word / 文本文件")


def split_clauses(text: str) -> list[Clause]:
    """按「第X条」边界把全文切成条款块; 无条文结构返回空列表（调用方走兜底)"""
    if not text or not text.strip():
        return []
    matches = list(_CLAUSE_HEADER_RE.finditer(text))
    if not matches:
        return []

    clauses: list[Clause] = []
    # 条款前的合同头（标题/编号/甲乙方）单独成块，便于证据回指
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            clauses.append(Clause(ref="", title="前言", text=preamble))

    for i, m in enumerate(matches):
        # 每个条款块从条款头开始，到下一个条款头或文本结束
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[m.start() : end].strip()
        header = m.group(1).strip()
        ref = header[: header.index("条") + 1]  # "第一条 付款方式" -> "第一条"
        clauses.append(Clause(ref=ref, title=header, text=chunk))
    return clauses


def _sentence_units(text: str) -> list[str]:
    """按句末标点/换行切成小段（保留标点），供贪心打包。"""
    return [s for s in re.split(r"(?<=[。！？；\n])", text) if s.strip()]


def _pack_chunks(units: list[str], max_chars: int) -> list[str]:
    """句子级贪心打包：单块尽量不超过 max_chars；单句超长时硬切兜底。"""
    chunks: list[str] = []
    cur = ""
    for unit in units:
        # 单句超长：先按 max_chars 硬切再继续打包
        while len(unit) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(unit[:max_chars])
            unit = unit[max_chars:]
        if len(cur) + len(unit) <= max_chars or not cur:
            cur += unit
        else:
            chunks.append(cur)
            cur = unit
    if cur:
        chunks.append(cur)
    return chunks


def chunk_for_index(text: str, max_chars: int = 600) -> list[Clause]:
    """把全文切成入库检索块：
    - 有条款结构：每个条款一块；超长条款正文续切，续块带条款头保证独立可读；
    - 无条款结构：整体按句子通用切分（兜底，等价通用文本切分器）。
    """
    clauses = split_clauses(text)
    # 分支 1：全文没有条款结构 → 整篇按句子通用切分（兜底，供 RAG 入库）
    if not clauses:
        return [Clause(ref="", title="", text=c) for c in _pack_chunks(_sentence_units(text), max_chars)]

    out: list[Clause] = []
    for clause in clauses:
        # 分支 2：条款未超长 → 整块作为一个检索单元，直接保留
        if len(clause.text) <= max_chars:
            out.append(clause)
            continue
        # 正文 = 去掉条款头那一行；续块头比首块头长，预算按两者较紧者算
        body = clause.text.split("\n", 1)[1].strip() if "\n" in clause.text else ""
        cont_header = f"{clause.ref}（续）"
        budget = min(max_chars - len(clause.title) - 1, max_chars - len(cont_header) - 1)
        parts = _pack_chunks(_sentence_units(body), max(budget, 1))
        out.append(Clause(ref=clause.ref, title=clause.title, text=f"{clause.title}\n{parts[0]}"))
        for part in parts[1:]:
            out.append(Clause(ref=clause.ref, title=cont_header, text=f"{cont_header}\n{part}"))
    return out
