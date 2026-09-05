"""合同解析与切分。

职责：文件 → 全文文本；全文 → 按「第X条」切条款(条款成块不截断,超长条款带条款头续切,无条文结构退回句子级通用切分)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 条款头：行首的「第X条[标题]」，X 支持阿拉伯数字与中文数字
_CLAUSE_HEADER_RE = re.compile(r"(?m)^\s*(第[0-9一二三四五六七八九十百千万零〇]+条[^\n]*)")
# 章节头：行首的「一、标题」（校服/政采示范文本常用），中文序号支持到「十五、」以上
_CHAPTER_HEADER_RE = re.compile(r"(?m)^\s*([一二三四五六七八九十]+、[^\n]*)")


@dataclass
class Clause:
    """一个条款块: ref=条款号（如"第一条"), title=条款头整行, text=含条款头的全文。"""

    ref: str  # 条款号（如"第一条"）
    title: str  # 条款头整行
    text: str  # 含条款头的全文


def extract_text(path: str | Path) -> str:
    """读取 PDF / Word / 文本文件为全文(PDF 扫描件暂不支持)"""
    
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
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(str(path))
        parts: list[str] = []
        # 按文档真实顺序（段落与表格交错）抽取：付款期次等常放表格里，
        # 若只读 doc.paragraphs 会整块丢失
        for child in doc.element.body.iterchildren():
            if child.tag == qn("w:p"):
                parts.append(Paragraph(child, doc).text)
            elif child.tag == qn("w:tbl"):
                # 表格逐行读出，单元格用 " | " 连接成一行，便于抽取/检索
                table = Table(child, doc)
                for row in table.rows:
                    parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(part for part in parts if part)
    # 分支 4：其他后缀 → 明确报不支持，提示可上传格式
    raise ValueError(f"暂不支持 {suffix} 格式，请上传 PDF / Word / 文本文件")


def split_clauses(text: str) -> list[Clause]:
    """按条款/章节边界把全文切成块。

    双模式判定：优先「第X条」（企业/GF 式）；没有第X条但有「一、二、三」章节头
    （校服/政采式）→ 按章节切；两者都没有 → 返回空列表（调用方走句子兜底）。
    易错点：章节文本里的子条（1、2、3 / （一）（二））必须留在所在章节内，
    不能当成新的顶级边界——所以这里只认「一、」行首章节头。
    """
    if not text or not text.strip():
        return []
    tiao_matches = list(_CLAUSE_HEADER_RE.finditer(text))
    zhang_matches = list(_CHAPTER_HEADER_RE.finditer(text))
    # 分支 1：出现「第X条」→ 以条款为顶级结构（章节行降级为正文，防误切）
    if tiao_matches:
        matches = tiao_matches
        ref_of = lambda header: header[: header.index("条") + 1]
    # 分支 2：无第X条但有章节头 → 以章节为顶级结构
    elif zhang_matches:
        matches = zhang_matches
        # 章节没有「第X条」式短引用，直接用整行标题当 ref（续块可读、证据可回指）
        ref_of = lambda header: header
    # 分支 3：都没有 → 无条文结构，交回调用方兜底
    else:
        return []
    if not matches:
        return []

    clauses: list[Clause] = []
    # 顶级结构前的合同头（标题/编号/甲乙方/鉴于段）单独成块，便于证据回指
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            clauses.append(Clause(ref="", title="前言", text=preamble))

    for i, m in enumerate(matches):
        # 每个块从条款/章节头开始，到下一个顶级头或文本结束
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[m.start() : end].strip()
        header = m.group(1).strip()
        clauses.append(Clause(ref=ref_of(header), title=header, text=chunk))
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
