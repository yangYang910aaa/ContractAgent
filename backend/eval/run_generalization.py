"""真实合同泛化集观察跑批(评测二期, 决策 D36)。

用途：拿用户收集的真实合同（非生成器产出）整份跑一遍流水线，只看表现、不打分——
这批没有 ground truth（判分口径留给后续批3/走查再定）。观察四件事：
1) 抽取是否失败（报告 error）；2) 有没有误停闸（出现 high，逐条人工核证据）；
3) medium 噪音分布（按 risk_type 计数，用于收紧规则口径）；4) parser 章节切分是否
正常（0 条/极少条要记入问题与踩坑记录）。

成本口径：默认 single × runs=1，每份 1~2 次 chat 调用（付款期次双读各 +1）。
扫描件默认整批跳过（纯图片 PDF 留给第 5 步 OCR，用 --include-scans 才纳入）。

合规：素材目录 data/素材/ 已 gitignore，产物 JSON 写 backend/eval/output/
（同样不入库）；脚本里不写任何真实合同文件名，选子集只在命令行给 --only。

用法：
    python -m backend.eval.run_generalization --dry-run          # 离线：只列文件与预算
    python -m backend.eval.run_generalization --only 电煤 --runs 1
    python -m backend.eval.run_generalization --no-double-read   # 省一半调用
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from backend.app.config import BASE_DIR
from backend.app.parser import extract_text, split_clauses
from backend.app.pipeline import run_review
from backend.eval.run_eval import _to_jsonable

# 真实合同目录（本地、已 gitignore；含 电子版/ 与 扫描件/ 两个子目录，递归扫描）
DEFAULT_DIR = BASE_DIR / "data/素材/真实合同"
DEFAULT_OUT = BASE_DIR / "backend/eval/output"

# 默认排除的素材：扫描件是纯图片 PDF（文本层近 0 字），走第 5 步 OCR 单独评测
DEFAULT_EXCLUDE = ("扫描件",)

# 章节切分异常判据：正文有字却切出极少条款 → 章节式/定义式结构，evidence 回指会丢
_CLAUSE_ANOMALY_MAX = 2


def discover(base: Path, only: list[str], exclude: list[str], include_scans: bool) -> list[Path]:
    """列出待跑的合同文件：默认排除扫描件；only 非空时只留文件名含任一子串的。

    排序保证跑批顺序稳定（踩坑复现时便于对比两次跑批的同一批文件）。
    """
    ex = [e for e in exclude if e]
    # 这种情况是：显式要求纳入扫描件 → 不再按"扫描件"前缀排除
    if include_scans:
        ex = [e for e in ex if e not in DEFAULT_EXCLUDE]
    # 递归扫描：真实合同放在"电子版/扫描件"两个子目录里（2026-09-11 素材目录合并后）
    files = [
        p
        for p in sorted(base.rglob("*"))
        if p.is_file() and not any(e in p.name for e in ex)
    ]
    # 这种情况是：给了 --only → 只保留命中任一子串的文件（不做空手道匹配）
    if only:
        files = [p for p in files if any(sub in p.name for sub in only)]
    return files


def _text_stats(path: Path) -> tuple[int, int]:
    """离线取全文统计 → (字符数, 条款块数)；解析失败返回 (-1, -1) 由调用方标注。"""
    try:
        text = extract_text(path)
        return len(text), len(split_clauses(text))
    except Exception:
        return -1, -1


def _row(path: Path, review_mode: str, double_read: bool, llm=None, retriever=None) -> dict:
    """跑一份真实合同 → 观察记录行（评级/类型分布/错误/调用成本/文本统计）。"""
    chars, clauses = _text_stats(path)
    started = time.perf_counter()
    report = run_review(
        path, review_mode=review_mode, llm=llm, double_read=double_read, retriever=retriever
    )
    seconds = round(time.perf_counter() - started, 1)
    risks = report.get("risks") or []
    llm_info = report.get("llm") or {}
    extracted = report.get("extracted") or {}
    hits = report.get("policy_hits") or []
    return {
        "file": path.name,
        "grade": report.get("grade"),
        "error": report.get("error"),
        # 全部命中按"类型/严重级"落成字符串列表，人读与产物都好对
        "types": [f"{r['risk_type']}/{r['severity']}" for r in risks],
        "high_types": [r["risk_type"] for r in risks if r.get("severity") == "high"],
        "medium_types": [r["risk_type"] for r in risks if r.get("severity") == "medium"],
        # 完整风险条目（含 evidence/clause_ref/suggestion）：误报判定必须落到原文句子上，
        # 只存类型名的话第二轮得再花钱跑一遍才能核（评测二期的教训）
        "risks": risks,
        # 完整抽取结果：缺必填/金额不一致这类命中要回看模型到底抽到了什么才算核得清
        "extracted": extracted,
        "kind": extracted.get("contract_kind"),
        "buyer": extracted.get("buyer"),
        "supplier": extracted.get("supplier"),
        "total_amount": extracted.get("total_amount"),
        "chars": chars,
        "clauses": clauses,
        "policy_hits_with_text": sum(1 for h in hits if (h or {}).get("text")),
        "seconds": seconds,
        "llm_calls": int(llm_info.get("calls") or 0),
        "llm_seconds": float(llm_info.get("seconds") or 0.0),
    }


def _summarize(rows: list[dict]) -> dict:
    """把观察行汇总成"跑批结论"：误停闸候选 / 噪音频率 / 抽取失败 / 切分异常。"""
    medium_freq: Counter[str] = Counter()
    for row in rows:
        medium_freq.update(row["medium_types"])
    return {
        "files": len(rows),
        "grade_counts": dict(Counter(r["grade"] or "error" for r in rows)),
        "errors": [r["file"] for r in rows if r["error"]],
        # 误停闸候选：真实件若出现 high 要逐条人工核证据（本轮观察口径，不自动判误报）
        "high_files": {r["file"]: r["high_types"] for r in rows if r["high_types"]},
        # high 的证据明细：控制台直接给原文句，省得再去 JSON 里翻
        "high_details": {
            r["file"]: [
                {
                    "type": risk.get("risk_type"),
                    "severity": risk.get("severity"),
                    "clause_ref": risk.get("clause_ref"),
                    "evidence": (risk.get("evidence") or "")[:120],
                }
                for risk in r.get("risks") or []
                if risk.get("severity") == "high"
            ]
            for r in rows
            if r["high_types"]
        },
        "medium_freq": dict(medium_freq.most_common()),
        "total_calls": sum(r["llm_calls"] for r in rows),
        "total_seconds": round(sum(r["seconds"] for r in rows), 1),
        # 章节切分异常：有正文却几乎切不出条款（如定义式/目录式合同）
        "clause_anomalies": {
            r["file"]: {"chars": r["chars"], "clauses": r["clauses"]}
            for r in rows
            if r["chars"] > 0 and r["clauses"] <= _CLAUSE_ANOMALY_MAX
        },
    }


def _print_rows(rows: list[dict]) -> None:
    """逐份打印观察行：评级 | 命中数 | 调用 | 耗时 | 文件名（+错误）。"""
    print("\n===== 逐份观察 =====")
    for row in rows:
        flags = f"high={','.join(row['high_types'])}" if row["high_types"] else f"类型={len(row['types'])}"
        err = f" ERROR: {row['error'][:60]}" if row["error"] else ""
        print(
            f" - {row['file']} | {row['grade']} | {flags} | "
            f"调用{row['llm_calls']}次 {row['seconds']}s | 条款{row['clauses']}{err}"
        )


def _print_summary(summary: dict) -> None:
    """打印跑批结论（本轮只观察不打分，故只列计数与清单）。"""
    print("\n===== 跑批结论 =====")
    print(f"文件 {summary['files']} 份 | 评级分布 {summary['grade_counts']} | "
          f"调用合计 {summary['total_calls']} 次 | 总耗时 {summary['total_seconds']}s")
    if summary["errors"]:
        print(f"抽取失败 {len(summary['errors'])} 份: {summary['errors']}")
    if summary["high_files"]:
        print("出现 high（逐条人工核证据，确认误报先修规则再提交）:")
        for name, details in summary["high_details"].items():
            print(f"  - {name}")
            for item in details:
                ref = f"[{item['clause_ref']}]" if item.get("clause_ref") else ""
                print(f"      {item['type']}{ref}: {item['evidence']}")
    if summary["medium_freq"]:
        print("medium 噪音频率（按 risk_type 计数，口径收紧的候选）:")
        for risk_type, n in summary["medium_freq"].items():
            print(f"  - {risk_type}: {n}")
    if summary["clause_anomalies"]:
        print("章节切分异常（有正文但切不出条款 → 记入问题与踩坑记录）:")
        for name, info in summary["clause_anomalies"].items():
            print(f"  - {name}: chars={info['chars']} clauses={info['clauses']}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：列文件/预算 → 逐份观察跑 → 打印结论并落盘产物。"""
    parser = argparse.ArgumentParser(description="真实合同泛化集观察跑批")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="真实合同素材目录")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="产物输出目录")
    parser.add_argument("--runs", type=int, default=1, help="每份跑几次（默认 1，省钱）")
    parser.add_argument("--only", default="", help="只跑文件名含该子串的文件，逗号分隔")
    parser.add_argument(
        "--review-mode",
        choices=("single", "double"),
        default="single",
        help="single=主审（默认，省钱）/ double=主审+盲审复核（每份多 1 次调用）",
    )
    parser.add_argument(
        "--no-double-read",
        action="store_true",
        help="关掉付款期次双读，每份省 1 次调用（成本减半，波动观察会略降）",
    )
    parser.add_argument("--include-scans", action="store_true", help="纳入扫描件（无文本层的会报错）")
    parser.add_argument("--dry-run", action="store_true", help="离线：只列文件与预估调用数后退出")
    args = parser.parse_args(argv)

    if not args.dir.is_dir():
        print(f"素材目录不存在: {args.dir}", file=sys.stderr)
        return 1
    only = [s for s in args.only.split(",") if s]
    files = discover(args.dir, only, list(DEFAULT_EXCLUDE), args.include_scans)
    if not files:
        print("没有匹配的合同文件（检查 --only/--include-scans）", file=sys.stderr)
        return 1
    double_read = not args.no_double_read
    # 预估调用数：每份 1 次抽取 + 双读 1 次 + 双审 1 次，再乘份数与次数
    per_file = 1 + (1 if double_read else 0) + (1 if args.review_mode == "double" else 0)
    print(f"文件 {len(files)} 份 × 每份跑 {args.runs} 次 × 每份约 {per_file} 次调用 "
          f"≈ {len(files) * args.runs * per_file} 次 chat 调用 "
          f"(review_mode={args.review_mode}, 双读={'开' if double_read else '关'})")
    for path in files:
        print(f"  - {path.name}")
    # 这种情况是：--dry-run → 只做预算与清单，不发起任何调用
    if args.dry_run:
        return 0

    rows: list[dict] = []
    for idx, path in enumerate(files, start=1):
        for run_no in range(1, args.runs + 1):
            row = _row(path, args.review_mode, double_read)
            row["run"] = run_no
            rows.append(row)
            print(
                f"[{idx}/{len(files)}] run{run_no} {row['file']} "
                f"grade={row['grade']} high={row['high_types'] or '-'} "
                f"调用{row['llm_calls']}次 {row['seconds']}s",
                flush=True,
            )

    summary = _summarize(rows)
    _print_rows(rows)
    _print_summary(summary)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    result = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dir": str(args.dir),
        "runs": args.runs,
        "review_mode": args.review_mode,
        "double_read": double_read,
        "summary": summary,
        "files": rows,
    }
    out_json = args.out / f"generalization_{stamp}.json"
    out_json.write_text(
        json.dumps(_to_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n产物: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
