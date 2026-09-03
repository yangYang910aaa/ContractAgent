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
import sys
from pathlib import Path

from backend.app.config import BASE_DIR
from backend.app.extractor import extract_contract
from backend.app.parser import extract_text
from backend.app.policy_rag import PolicyHit, retrieve_policies
from backend.app.rules import evaluate, grade_report
from backend.app.schemas import ContractModel, RiskItem

DEFAULT_SAMPLES = sorted((BASE_DIR / "data" / "contracts").glob("*.md"))


def enrich_policy_hits(
    risks: list[RiskItem],
    retriever=None,  # (query: str) -> list[PolicyHit]，测试可注入假检索器
) -> list[dict]:
    """为带 policy_ref 的风险检索政策原文，去重后返回引用清单。

    作用：让报告里的每条政策类风险都有"依据哪条政策"的原文可查
    （防 LLM/规则凭空判断；检索失败不阻断审查，该条留空）。
    """
    retriever = retriever or (lambda query: retrieve_policies(query, k=1))
    hits: list[dict] = []
    seen: set[str] = set()
    for risk in risks:
        # 分支：规则没给政策编号（纯逻辑风险如金额不一致）→ 无需检索
        if not risk.policy_ref or risk.policy_ref in seen:
            continue
        seen.add(risk.policy_ref)
        query = risk.evidence or risk.suggestion
        try:
            top = retriever(query)[0] if retriever(query) else None
        except Exception:
            top = None  # 检索服务不可用时不拖垮整份报告
        if top is not None:
            hits.append(
                {"policy_ref": top.policy_ref, "score": round(top.score, 3), "snippet": top.text[:200]}
            )
        else:
            hits.append({"policy_ref": risk.policy_ref, "score": None, "snippet": ""})
    return hits


def build_report(
    contract_file: str,
    extracted: ContractModel,
    risks: list[RiskItem],
    policy_hits: list[dict],
) -> dict:
    """把流水线各环节结果组装成报告 dict（JSON 可直接序列化）。"""
    return {
        "contract_file": contract_file,
        "grade": grade_report(risks).value,
        "risks": [risk.model_dump(mode="json") for risk in risks],  # date/Decimal → JSON 类型
        "policy_hits": policy_hits,
        "extracted": extracted.model_dump(mode="json"),
    }


def run_review(path: str | Path) -> dict:
    """完整跑一份合同：取文本 → 抽取 → 规则 → 政策检索 → 报告。

    抽取环节异常不中断批处理：报告带 error 字段，便于 CLI 批量跑时定位坏文件。
    """
    path = Path(path)
    text = extract_text(path)
    try:
        extracted = extract_contract(text=text)
    except Exception as exc:  # LLM/接口异常（如格式不支持、超时）
        extracted = ContractModel()
        risks: list[RiskItem] = []
        return {
            "contract_file": str(path),
            "grade": None,
            "risks": [],
            "policy_hits": [],
            "extracted": extracted.model_dump(),
            "error": f"抽取失败：{exc}",
        }
    risks = evaluate(extracted)
    policy_hits = enrich_policy_hits(risks)
    return build_report(str(path), extracted, risks, policy_hits)


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
