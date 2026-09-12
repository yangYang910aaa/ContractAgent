"""政策检索质量离线评测：向量 vs 混合（向量+BM25+RRF）。

用途：语料纵向分条 + 横向扩类后，用"查询→期望政策编号"标准答案集
量化检索质量，验证混合检索是否真的更好，并留下可复现数字（PRD/README 引用）。

指标：Recall@1、Recall@3、MRR（同一查询在两种模式下的对照）。
用法：
    python -m backend.eval.run_retrieval_eval --k 3
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from backend.app.config import BASE_DIR
from backend.app.policy_rag import retrieve_policies

# 标准答案集：(查询, 期望政策编号)。查询取自各政策/风险类型的典型表述，覆盖 P-01~P-12。
GOLD: list[tuple[str, str]] = [
    ("预付款比例超过合同总额的百分之三十", "P-01"),
    ("预付款资金占用与供应商履约风险", "P-01"),
    ("质保期不足十二个月", "P-02"),
    ("质量保证期约定少于一年", "P-02"),
    ("责任上限低于合同总额的百分之五十", "P-03"),
    ("逾期违约金每日超过百分之一", "P-03"),
    ("保密期限超过三十六个月", "P-04"),
    ("未约定保密义务条款", "P-04"),
    ("知识产权归属没有约定", "P-05"),
    ("缺少适用法律与争议解决约定", "P-05"),
    ("未明确验收标准与验收期限", "P-06"),
    ("交付验收安排怎么写", "P-06"),
    ("付款安排没有约定发票开具义务", "P-07"),
    ("先票后款结算要求", "P-07"),
    ("大额合同缺少履约担保", "P-08"),
    ("预付款需要银行保函或保证金", "P-08"),
    ("未限制转包分包", "P-09"),
    ("允许任意转包且甲方无法追责", "P-09"),
    ("委托处理个人信息的要件不完整", "P-10"),
    ("未约定个人信息保护义务", "P-10"),
    ("数据出境没有做安全评估", "P-11"),
    ("境外服务器处理用户数据是否合规", "P-11"),
    ("未约定数据删除返还与泄露告知", "P-12"),
    ("数据安全事件的告知与补救义务", "P-12"),
    # ---- 难例：口语化/数字型表述（考察 BM25 关键词与向量语义的互补）----
    ("先付三成货款安不安全", "P-01"),
    ("质保只给半年够不够", "P-02"),
    ("赔偿最多赔多少合适", "P-03"),
    ("保密要保三年以上吗", "P-04"),
    ("成果归谁所有没写清楚", "P-05"),
    ("货到了怎么验收", "P-06"),
    ("报销要发票吗", "P-07"),
    ("一百万的合同要不要保函", "P-08"),
    ("活儿能不能交给别人做", "P-09"),
    ("对方能不能把我的用户资料拿去做别的", "P-10"),
    ("数据传到国外服务器行不行", "P-11"),
    ("服务结束以后数据怎么处理", "P-12"),
]


def _metrics(rows: list[tuple[str, str, list[str]]]) -> dict:
    """按标准答案逐行算 Recall@1/@3 与 MRR（hits 为按序命中的 policy_ref 列表）。"""
    n = len(rows)
    r1 = sum(1 for _, want, hits in rows if hits[:1] == [want]) / n
    r3 = sum(1 for _, want, hits in rows if want in hits[:3]) / n
    mrr = sum(
        (1.0 / (hits.index(want) + 1)) if want in hits[:3] else 0.0 for _, want, hits in rows
    ) / n
    return {"recall@1": round(r1, 4), "recall@3": round(r3, 4), "mrr": round(mrr, 4)}


def _run(mode: str, k: int) -> tuple[dict, list[tuple[str, str, list[str]]]]:
    """跑一遍标准答案集，返回 (指标, 明细行)。"""
    rows: list[tuple[str, str, list[str]]] = []
    for query, want in GOLD:
        hits = [h.policy_ref for h in retrieve_policies(query, k=k, mode=mode)]
        rows.append((query, want, hits))
    return _metrics(rows), rows


def main(argv: list[str] | None = None) -> int:
    """CLI：跑 vector / hybrid 两种模式并打印对照表，结果落 output/。"""
    parser = argparse.ArgumentParser(description="政策检索质量评测（vector vs hybrid）")
    parser.add_argument("--k", type=int, default=3, help="每次检索返回条数（默认 3）")
    parser.add_argument("--out", type=Path, default=BASE_DIR / "backend/eval/output")
    args = parser.parse_args(argv)

    results: dict[str, dict] = {}
    details: dict[str, list] = {}
    for mode in ("vector", "hybrid"):
        metrics, rows = _run(mode, args.k)
        results[mode] = metrics
        details[mode] = [
            {"query": q, "expected": w, "hits": h, "hit@1": h[:1] == [w], "hit@3": w in h[:3]}
            for q, w, h in rows
        ]
        print(f"[{mode}] recall@1={metrics['recall@1']} recall@3={metrics['recall@3']} mrr={metrics['mrr']}")

    print("\n===== 逐条对照（仅列未命中或模式差异）=====")
    for vrow, hrow in zip(details["vector"], details["hybrid"]):
        q, w, vh, hh = vrow["query"], vrow["expected"], vrow["hits"], hrow["hits"]
        v_ok, h_ok = vh[:1] == [w], hh[:1] == [w]
        # 只打印有差异或都未命中的行，方便定位
        if v_ok != h_ok or (not v_ok and not h_ok):
            print(f"  {q[:24]:<26} 期望={w} vector={vh[:3]} hybrid={hh[:3]}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.out / f"retrieval_eval_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"k": args.k, "gold_size": len(GOLD), "metrics": results, "details": details},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n输出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
