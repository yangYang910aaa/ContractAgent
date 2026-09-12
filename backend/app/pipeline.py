"""单份合同审核流水线。

链路：文件 → parser 取全文 → extractor 抽取(LLM) → rules 规则审查 →
policy_rag 为带政策引用的风险检索政策原文 → 汇总成 report dict。

用法：
    python -m backend.app.pipeline data/contracts/sample_01_*.md [更多文件] [--out reports_dir]
不带文件时默认处理 data/contracts/*.md 全部样本。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from backend.app.config import BASE_DIR
from backend.app.extractor import DOUBLE_READ_FIELDS, extract_contract
from backend.app.parser import NO_TEXT_ERROR, extract_text
from backend.app.policy_rag import PolicyHit, load_policy_full, retrieve_policies_many
from backend.app.rules import (
    annotate_open_ended_risks,
    annotate_template_risks,
    evaluate,
    grade_report,
    infer_effective_from_signature,
    text_rules,
)
from backend.app.schemas import ContractModel, RiskItem
from backend.app.usage import track_usage

DEFAULT_SAMPLES = sorted((BASE_DIR / "data" / "contracts").glob("*.md"))


def _safe_retrieve(retriever, query: str) -> list:
    """调一次注入的检索函数；检索不可用（服务未起/超时）返回空列表，不阻断整份报告。"""
    try:
        return retriever(query) or []
    except Exception:
        return []


def enrich_policy_hits(
    risks: list[RiskItem],
    retriever=None,  # (query: str) -> list[PolicyHit]，测试可注入假检索器
) -> list[dict]:
    """拿到规则引擎产出的风险名单后,为每条带policy_ref的风险去向量库检索政策原文,让报告可溯源。
    作用：让报告里的每条政策类风险都有"依据哪条政策"的原文可查
    （防 LLM/规则凭空判断；检索失败不阻断审查，该条留空）。
    """
    hits: list[dict] = []
    # 先收集"要查哪几条政策"（同一条政策只查一次），把 query 备齐再统一检索：
    # 默认路径合并成一次批量向量化请求，省掉逐条往返的等待
    wanted: list[tuple[str, str]] = []  # (政策编号, 检索用文本)
    seen: set[str] = set()
    for risk in risks:
        # 分支：多条风险可能引用同一条政策,只检索一次，避免重复向量检索
        if not risk.policy_ref or risk.policy_ref in seen:
            continue
        seen.add(risk.policy_ref)
        # 优先用建议文本作 query：建议讲的是"这条风险要什么样的条款"（话题），与所引政策对得上；
        # 证据原文是合同自己的话，话题常和所引政策两回事——实测按证据检索会把"数据处理要件"
        # 检索成知识产权条文，报告里就查不到声明的政策原文。没有建议才退回证据
        wanted.append((risk.policy_ref, risk.suggestion or risk.evidence))

    found_lists = (
        # 多取两条候选：语义检索不看政策号，话题相近的政策会互相插队，
        # 多取几个才有机会挑回属于声明政策的那条
        retrieve_policies_many([query for _, query in wanted], k=3)
        if retriever is None
        else [_safe_retrieve(retriever, query) for _, query in wanted]
    )

    for (policy_ref, _), found in zip(wanted, found_lists):
        # 分支：候选里有属于声明的政策号的条文 → 取它（这才是这条风险的依据）
        top = next((hit for hit in found if hit.policy_ref == policy_ref), None)
        # 分支：一条都不属于声明政策 → 退回首条，照实呈现检索到的内容
        if top is None and found:
            top = found[0]
        if top is not None:
            hits.append(
                {
                    "policy_ref": top.policy_ref,
                    "score": round(top.score, 3),
                    "snippet": _policy_snippet(top.text),
                    # text=命中的具体条文（分条后检索到条）；full_text=整份政策，
                    # 前端"查看完整条文"展开整份用（分条前 text 即整份，现两者分开）
                    "text": top.text,
                    "full_text": load_policy_full(top.source),
                }
            )
        else:
            hits.append({"policy_ref": policy_ref, "score": None, "snippet": ""})
    return hits


def _policy_snippet(text: str, limit: int = 200) -> str:
    """智能截断:政策原文 → 报告里的引用片段（保留行结构，每条信息单独一行）。
        规则：去每行 md 标题符 → 把"文件编号:X　　版本:Y"这类同行多信息按全角空格拆成独立行 → 逐行
    累积到 limit, 超长行在句末标点断并加省略号。
    """
    out: list[str] = []
    total = 0
    for raw in text.splitlines():
        # 先去除行首 Markdown 标记；注意不能在拆段前折叠空白（会把"　　"压成单空格，导致 文件编号/版本/生效日期 同段信息拆不开）
        line = re.sub(r"^(?:#{1,6}|>|-|\*)\s*", "", raw.strip())
        # 同行多段信息按 2+ 空白拆成独立段，段内多余空白再折叠
        pieces = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"[\s\u3000]{2,}", line) if p.strip()]
        for piece in pieces:
            # 这种情况是：当前行放得下 → 直接收进片段
            if total + len(piece) <= limit:
                out.append(piece)
                total += len(piece) + 1
                continue
            # 这种情况是：超出上限且前面已有内容 → 尽量在句末断后省略
            room = max(limit - total, 0)
            if room >= 12:
                cut = piece[:room]
                idx = max(cut.rfind("。"), cut.rfind("；"), cut.rfind("，"), cut.rfind("！"), cut.rfind("？"))
                if idx >= room * 0.5:
                    out.append(cut[: idx + 1] + "…")
                else:
                    out.append(cut.rstrip() + "…")
            elif total == 0:
                # 这种情况是：首行就超长（没有可省略的已收内容）→ 直接截首行
                cut = piece[:limit]
                idx = max(cut.rfind("。"), cut.rfind("；"), cut.rfind("，"))
                out.append((cut[: idx + 1] if idx >= limit * 0.5 else cut) + "…")
            return "\n".join(out)
        if total >= limit:
            break
    return "\n".join(out)


def build_report(
    contract_file: str,
    extracted: ContractModel,
    risks: list[RiskItem],
    policy_hits: list[dict],
    review: dict | None = None,
    llm: dict | None = None,
) -> dict:
    """把流水线各环节结果组装成报告 dict(JSON 可直接序列化)。

    review 为双审(review_mode=double)的合并结果段；单审传 None,报告里为 null。
    llm 为本次审查的 LLM 调用计数段(评测二期)；不传为 null,旧调用方(如 graph
    服务端链路)不受影响。
    """
    report = {
        "contract_file": contract_file,  #来源文件路径
        "grade": grade_report(risks).value, #high/medium/low的等级评分
        "risks": [risk.model_dump(mode="json") for risk in risks],  # date/Decimal → JSON 类型
        "policy_hits": policy_hits, #政策引用清单
        "extracted": extracted.model_dump(mode="json"),
    }
    report["review"] = review
    report["llm"] = llm
    return report


def run_review(
    path: str | Path,
    review_mode: str = "single",
    llm=None,
    double_read: bool = True,
    retriever=None,
) -> dict:
    """跑完整链路：取文本 → 抽取 → 规则 → （可选盲审复核）→ 政策检索 → 报告。

    模型与检索函数可注入（离线测试、跑批不连向量库）；审查模式单审/双审；付款期次双读
    可关闭以省调用。抽取失败不中断批处理，报告带错误信息便于定位坏文件。
    """
    path = Path(path)
    #parser 取全文
    text = extract_text(path)
    # 计价区间只覆盖 LLM 环节：parser/规则/检索是纯本地，不进成本口径
    with track_usage() as usage:
        # 这种情况是：文件读不出正文（空文件、加密/损坏 PDF、OCR 没认出字）→ 直接给错误报告。
        # 在空正文上跑规则只会凭空报"缺必填"，让人对着空文件点审批（服务端图链路同一口径）
        if not text.strip():
            return {
                "contract_file": str(path),
                "grade": None,
                "risks": [],
                "policy_hits": [],
                "extracted": ContractModel().model_dump(),
                "review": None,
                "llm": usage.to_dict(),
                "error": NO_TEXT_ERROR,
            }
        try:
            #LLM 结构化抽取（double_read 开时含付款期次第二读）
            extracted = extract_contract(
                llm=llm,
                text=text,
                double_read_fields=DOUBLE_READ_FIELDS if double_read else (),
            )
            # 合同写"自签字盖章之日起生效"时回填 签字日期
            extracted = infer_effective_from_signature(extracted, text)
        except Exception as exc:  # LLM/接口异常（如格式不支持、超时）
            extracted = ContractModel()
            return {
                "contract_file": str(path),
                "grade": None,
                "risks": [],
                "policy_hits": [],
                "extracted": extracted.model_dump(),
                "review": None,
                # 抽取失败也可能已发出调用（限流/超时），成本口径照实带出
                "llm": usage.to_dict(),
                "error": f"抽取失败：{exc}",
            }
        # 先跑规则引擎（字段级 evaluate + 文本级 text_rules），再叠加开放式条款/模板标注；
        # 文本级检查不依赖抽取字段，两个 annotate 只做"降级 + 附提示"，不改判定口径
        risks = annotate_template_risks(
            annotate_open_ended_risks(evaluate(extracted) + text_rules(text, extracted.contract_kind), text),
            text,
        )
        review: dict | None = None
        # 这种情况是：双审模式 → 盲审复核并与主审合并（合并后的新增 high 也参与检索引用）
        if review_mode == "double":
            from backend.app.reviewer import double_review  # 延迟导入：双审才拉 reviewer 链

            risks, review = double_review(risks, text, llm=llm, retriever=retriever)
        # 政策引用基于最终风险清单检索（复核新增项也带政策依据，report 才可溯源）
        policy_hits = enrich_policy_hits(risks, retriever=retriever)
        return build_report(
            str(path), extracted, risks, policy_hits, review=review, llm=usage.to_dict()
        )


def _summary_line(report: dict) -> str:
    """一行摘要：文件名 | 评级 | 命中风险类型 | 政策引用。"""
    risk_types = ",".join(r["risk_type"] for r in report["risks"]) or "-"
    refs = ",".join(h["policy_ref"] for h in report["policy_hits"]) or "-"
    error = f" ERROR: {report.get('error', '')}" if report.get("error") else ""
    return f"{Path(report['contract_file']).name} | {report['grade']} | {risk_types} | 政策:{refs}{error}"


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：逐份处理并输出（--out 目录则写 JSON 文件，否则打印全文）。"""
    parser = argparse.ArgumentParser(description="单份合同审核流水线")
    parser.add_argument("paths", nargs="*", help="合同文件路径；缺省跑 data/contracts/*.md")
    parser.add_argument("--out", default=None, help="报告输出目录（写 JSON 文件）")
    args = parser.parse_args(argv)

    files = [Path(p) for p in args.paths] if args.paths else DEFAULT_SAMPLES
    if not files:
        print("未找到可处理的合同文件", file=sys.stderr)
        return 1

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for path in files:
        report = run_review(path)
        if out_dir:
            target = out_dir / f"{path.stem}.report.json"
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(_summary_line(report))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
