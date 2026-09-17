"""三个工具：查政策库、读政策全文、在合同里找条款。

都是确定性实现，不调大模型——政策检索复用项目现成的混合检索（向量+BM25+RRF），
找条款在规则侧同一口径的干净正文上做关键词匹配。工具一律只读：改不了风险清单、
评级与审批状态。返回都带引用记录（响应 artifact），供引用汇总用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate
from langchain_core.retrievers import BaseRetriever
from langchain_core.tools import BaseTool, create_retriever_tool, tool
from pydantic import PrivateAttr

from backend.app.assistant.citations import Citation, clip_text
from backend.app.assistant.context import (
    POLICY_FILE_RE,
    ContractContext,
    clause_blocks,
    first_line_title,
    policy_title,
)
from backend.app.policy.rag import POLICY_DIR, load_policy_full, retrieve_policies
from backend.app.review.rules.locator import clean_rule_text, find_quote_pos, sentence_quote

# 政策检索取几条：够模型判断即可，多取只会把上下文撑长、引用栏变噪声
POLICY_SEARCH_K = 3
# 找条款最多返回几块：问"跟发票有关的条款"常命中好几处，全给会把回答淹掉
FIND_CLAUSES_LIMIT = 5


class PolicyRetriever(BaseRetriever):
    """政策检索器的 LangChain 适配：把项目现成的混合检索（向量+BM25+RRF）包成检索器。

    命中统一转成 Document（正文进 page_content，编号/来源/标题进 metadata），框架的
    检索工具与 tracing 都按这套惯例走；检索实现仍是 policy.rag 自己那一套。
    """

    k: int = POLICY_SEARCH_K  # 取几条命中
    _search: Callable[..., list] | None = PrivateAttr(default=None)  # 注入的检索函数（测试用）

    def __init__(
        self,
        retriever: Callable[..., list] | None = None,
        k: int = POLICY_SEARCH_K,
        **kwargs: Any,
    ) -> None:
        """retriever 可注入（离线测试给假检索器）；不传则走真实的混合检索。"""
        super().__init__(k=k, **kwargs)
        self._search = retriever

    def _hits(self, query: str) -> list:
        """取检索命中：注入的检索器优先（它自己决定取几条），缺省走混合检索。"""
        if self._search is not None:
            return list(self._search(query) or [])
        return list(retrieve_policies(query, k=self.k))

    def _get_relevant_documents(self, query: str, *, run_manager: Any = None) -> list[Document]:
        """同步检索：返回命中条文的 Document 列表。"""
        return [_hit_document(hit) for hit in self._hits(query)]


def _hit_document(hit: Any) -> Document:
    """检索命中（PolicyHit）→ Document：编号/来源/首行标题进 metadata。"""
    text = getattr(hit, "text", "") or ""
    return Document(
        page_content=text,
        metadata={
            "policy_ref": getattr(hit, "policy_ref", "") or "",
            "source": getattr(hit, "source", "") or "",
            "title": first_line_title(text),
            "score": getattr(hit, "score", None),
        },
    )


def _policy_path(policy_ref: str) -> tuple[str, Path] | None:
    """按政策编号找政策文件：返回（规范化编号, 路径），编号认不出或没有该文件返回 None。"""
    match = POLICY_FILE_RE.match((policy_ref or "").strip())
    if not match:
        return None
    ref = match.group(1).upper()
    for path in sorted(POLICY_DIR.glob(f"{ref}*.md")):
        return ref, path
    return None


def _search_clauses(context: ContractContext, keyword: str) -> tuple[str, list[dict]]:
    """在合同分条里按关键词找原句：返回（给模型看的文本, 引用记录）。

    匹配在规则侧同一口径的干净正文上做——PDF/OCR 的硬换行会把关键词切开（"乙方不/得转包"）、
    页标记会把句子从中间截断、汉字之间还会多出空格（"发 票"），按原文直接比会漏。
    """
    word = (keyword or "").strip()
    # 分支：没给关键词 → 直接告诉模型，别拿空词在整篇里乱命中
    if not word:
        return "请给出要查找的关键词（如“发票”“验收”“违约金”）", []
    text = clean_rule_text(context.text)
    hits: list[tuple[str, str, str]] = []  # (条款号, 条款标题, 命中原句)
    for block in clause_blocks(text):
        quote = _clause_quote(block, word)
        if quote is None:
            continue
        hits.append((block["ref"], block["title"], quote))
        if len(hits) >= FIND_CLAUSES_LIMIT:
            break
    # 分支：一条都没命中 → 照实回报，模型据此答"合同里没有涉及该话题的条款"
    if not hits:
        return f"合同条款里没有找到包含「{word}」的表述", []
    content_lines = [
        f"{ref}（{title}）{index}. {sentence}" if ref else f"{title}{index}. {sentence}"
        for index, (ref, title, sentence) in enumerate(hits, start=1)
    ]
    citations = [
        Citation(kind="clause", ref=ref or title, title=title, text=sentence).to_dict()
        for ref, title, sentence in hits
    ]
    return "\n".join(content_lines), citations


def _clause_quote(block: dict, word: str) -> str | None:
    """条款块里命中关键词的那句原文；没命中返回 None。

    先在条款正文里找、找不到再看标题行：关键词常常既是标题也是正文的话题
    （"第二条 发票" / "乙方开具增值税专用发票…"），正文那句信息量更大；而标题行
    开始处直接取整句会把标题连进正文（"第二条付款方式合同签订后…"），标题另有位置展示。
    """
    text = block["text"]
    title = block["title"]
    body = text
    # 分支：块正文以标题行开头（条款块常态）→ 正文从标题之后算起
    if title and text.startswith(title):
        body = text[len(title) :].lstrip("\n")
    pos = find_quote_pos(body, word) if body else -1
    # 分支：正文里命中 → 摘录这句原文
    if pos >= 0:
        return sentence_quote(body, pos)
    # 分支：正文里没有 → 关键词可能只出现在条款标题里（如"第六条 验收标准"）
    pos = find_quote_pos(text, word)
    return sentence_quote(text, pos) if pos >= 0 else None


def _read_policy(policy_ref: str) -> tuple[str, list[dict]]:
    """读某份政策全文：返回（给模型看的文本, 引用记录）。"""
    found = _policy_path(policy_ref)
    # 分支：编号不是 P-XX 形态或该编号没有文件 → 明确回报，别让模型凭记忆讲政策
    if found is None:
        return f"没有找到政策 {policy_ref}（政策编号形如 P-01~P-14，见政策库目录）", []
    ref, path = found
    text = load_policy_full(path.name)
    # 分支：文件在但内容为空 → 照实回报，不收进引用
    if not text.strip():
        return f"政策 {ref} 的内容为空", []
    citation = Citation(
        kind="policy", ref=ref, title=policy_title(text), text=clip_text(text), source=path.name
    )
    return f"[{ref}] {text}", [citation.to_dict()]


def _search_policies_tool(retriever: Callable[..., list] | None) -> BaseTool:
    """构造"查政策库"工具：框架的检索工具 + 我们的混合检索适配器，返回条文与编号。"""
    return create_retriever_tool(
        PolicyRetriever(retriever=retriever),
        name="search_policies",
        description=(
            "检索集团采购合同审核制度。输入一个中文问题（如“预付款比例上限是多少”"
            "“数据出境要什么手续”），返回相关条文原文与政策编号（P-XX）。"
        ),
        # 命中带上政策编号与条文标题，模型回答时引得到号，引用芯片也认得出这一条
        document_prompt=PromptTemplate.from_template("[{policy_ref}] {title}\n{page_content}"),
        response_format="content_and_artifact",
    )


def build_tools(context: ContractContext, retriever: Callable[..., list] | None = None) -> list[BaseTool]:
    """按本次提问的合同构造三个工具：查政策库 / 读政策全文 / 在合同里找条款。

    retriever 可注入（离线测试给假检索器）；工具都只读，不改风险清单、评级与审批状态。
    """

    @tool("read_policy", response_format="content_and_artifact")
    def read_policy(policy_ref: str) -> tuple[str, list[dict]]:
        """按政策编号读整份政策全文（检索只给相关条文，要完整依据时用它）。"""
        return _read_policy(policy_ref)

    @tool("find_clauses", response_format="content_and_artifact")
    def find_clauses(keyword: str) -> tuple[str, list[dict]]:
        """在这份合同的条款里按关键词找原句（如"发票"、"验收"、"违约金"），返回条款号与原句。"""
        return _search_clauses(context, keyword)

    return [_search_policies_tool(retriever), read_policy, find_clauses]


def hit_summary(name: str, items: list[dict]) -> str:
    """工具返回摘要：一句话说命中了几条、都是哪条（前端状态行用）。"""
    # 分支：没命中 → 明说，模型也会照实答"没查到"
    if not items:
        return "没有命中"
    refs = list(dict.fromkeys(item["ref"] for item in items))
    shown = "、".join(refs[:3])
    # 分支：读政策全文 → 说的是读了哪份，不是命中几条
    if name == "read_policy":
        return f"已读取 {shown} 全文"
    # 分支：找条款 → 说是哪些条款；其余按政策检索报
    return f"命中条款 {shown}" if name == "find_clauses" else f"命中政策 {shown}"
