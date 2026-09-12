"""合同解析与切分。

职责：文件 → 全文文本；全文 → 按「第X条」切条款(条款成块不截断,超长条款带条款头续切,无条文结构退回句子级通用切分)。
docx文件走python-docx解析,按文档顺序抽段落+表格
pdf文件走pypdf逐页抽取文本；**文本层为空的图片型 PDF（扫描件）与图片文件走 OCR**
（pymupdf 渲染 + rapidocr，依赖可选、惰性导入，见 _ocr_pdf/_ocr_image_file）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 条款头：行首的「第X条[标题]」，X 支持阿拉伯数字与中文数字
_CLAUSE_HEADER_RE = re.compile(r"(?m)^\s*(第[0-9一二三四五六七八九十百千万零〇]+条[^\n]*)")
# 章节头：行首的「一、标题」（校服/政采示范文本常用），中文序号支持到「十五、」以上
_CHAPTER_HEADER_RE = re.compile(r"(?m)^\s*([一二三四五六七八九十]+、[^\n]*)")

# ---- 扫描件/图片 OCR 通路（第 5 步）----
# 图片后缀：相机拍照件、截图件与"图片装进 PDF"的扫描件同属无文本层输入
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
# 文本层字数阈值：真实扫描件用 pypdf 只能抽出 2~18 字（页码/零散数字），
# 低于该阈值即判定为图片型 PDF；正常电子版合同动辄上千字，不会误触发
PDF_OCR_MIN_CHARS = 40
# 渲染倍率：2 倍（约 144dpi）——实测关键字段可读且 3~17s/页，再高收益有限、耗时翻倍
OCR_RENDER_SCALE = 2
# OCR 引擎单例（首次调用建模型约 1s；None=尚未初始化）
_OCR_ENGINE = None


@dataclass
class Clause:
    """一个条款块: ref=条款号（如"第一条"), title=条款头整行, text=含条款头的全文。"""

    ref: str  # 条款号（如"第一条"）
    title: str  # 条款头整行
    text: str  # 含条款头的全文


def extract_text(path: str | Path, ocr: bool = True) -> str:
    """读取 PDF / Word / 文本 / 图片文件为全文（图片型 PDF 与图片走 OCR，见 _ocr_* 注释）。

    ocr=False 时只走文本层（离线评测/对照用）：扫描件会返回空文本而不是触发 OCR。
    """
    path = Path(path)
    suffix = path.suffix.lower()
    # 分支 1：纯文本类（.txt/.md）→ UTF-8 直读全文
    if suffix in {".txt", ".md", ".markdown"}:
        return path.read_text(encoding="utf-8", errors="replace")
    # 分支 2：PDF → 先取文本层；文本层为空（图片型 PDF/扫描件）→ 走 OCR 通路
    if suffix == ".pdf":
        text = _pdf_text_layer(path)
        # 这种情况是：文本层字数低于阈值 → 判定为扫描件（真实扫描件只抽出 2~18 字）
        if ocr and len(text.strip()) < PDF_OCR_MIN_CHARS:
            ocr_text = _ocr_pdf(path)
            # 分支：OCR 也没读出内容（如纯空白页）→ 保留文本层结果，不返回误导性的空串
            if ocr_text.strip():
                return ocr_text
        return text
    # 分支 3：图片文件（.jpg/.png…）→ 直接 OCR（相机拍照件/截图件）
    if suffix in IMAGE_SUFFIXES:
        return _ocr_image_file(path) if ocr else ""
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
    # 分支 5：其他后缀 → 明确报不支持，提示可上传格式
    raise ValueError(f"暂不支持 {suffix} 格式，请上传 PDF / Word / 文本 / 图片文件")


def _pdf_text_layer(path: Path) -> str:
    """pypdf 取 PDF 文本层（图片型 PDF 只能抽到零散数字/页码，属预期）。"""
    # 延迟导入：只在真遇到 PDF 时拉 pypdf，避免拖慢纯文本路径
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _ocr_engine():
    """惰性初始化 RapidOCR 引擎（首次约 1s 建模型；缺依赖时给可执行的报错）。

    易错点：rapidocr/onnxruntime 体积大，只在真遇到扫描件时才导入，否则拖慢服务启动。
    """
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # 依赖未装（部署环境可能只跑文本件）
            raise ValueError(
                "扫描件/图片需要 OCR 依赖，请先安装：pip install pymupdf rapidocr-onnxruntime"
            ) from exc
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _ocr_image_bytes(image: bytes) -> list[str]:
    """对一张图片跑 OCR，按阅读顺序返回文本行（低置信度行一并保留，宁全勿缺）。

    口径：不过滤低分行的原因——盖章/手写导致的低分里常含关键信息（日期、金额），
    过滤反而丢线索；质量问题由评测环节用"低置信页清单"暴露，不在这里静默丢弃。
    """
    result, _ = _ocr_engine()(image)
    return [item[1] for item in (result or []) if len(item) >= 2 and item[1]]


def _ocr_pdf(path: Path) -> str:
    """图片型 PDF → 逐页渲染成图片再 OCR，拼接时插入"--- 第 N 页 ---"便于人工核对。

    渲染倍率取 2（约 144dpi）：实测 3~17s/页、关键字段（甲乙方/金额/日期）可读；
    再高倍率识别率提升有限但耗时翻倍。
    页级并行**不可取**：推理本身已吃满多核，多页并发只是抢核、反而更慢，故保持串行。
    """
    import pymupdf  # 延迟导入：只有扫描件才需要

    doc = pymupdf.open(str(path))
    pages: list[str] = []
    for index in range(doc.page_count):
        pix = doc[index].get_pixmap(matrix=pymupdf.Matrix(OCR_RENDER_SCALE, OCR_RENDER_SCALE))
        lines = _ocr_image_bytes(pix.tobytes("png"))
        body = "\n".join(lines) or "（本页无可识别文字）"
        pages.append(f"--- 第 {index + 1} 页 ---\n{body}")
    doc.close()
    return "\n".join(pages)


def _ocr_image_file(path: Path) -> str:
    """图片文件（.jpg/.png…）直接 OCR；不渲染、不加页标记（单页无页码意义）。"""
    return "\n".join(_ocr_image_bytes(path.read_bytes()))


def split_clauses(text: str) -> list[Clause]:
    """按条款/章节边界把全文切成块，两种结构都没有时返回空列表（调用方走句子兜底）。

    优先按「第X条」切，其次按「一、二、三」章节头切，且只认行首章节头——
    章节内的子条（1、2、（一））必须留在原章节里，不能当成新的顶级边界。
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
    """句子级贪心打包：单块尽量不超过 max_chars; 单句超长时硬切兜底。"""
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
        #装得下 -> 继续装
        if len(cur) + len(unit) <= max_chars or not cur:
            cur += unit
        # 装不下 -> 封箱,开新箱 
        else:
            chunks.append(cur)
            cur = unit
        # 最后一个箱子也要封上
    if cur:
        chunks.append(cur)
    return chunks

#最终切分策略
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
