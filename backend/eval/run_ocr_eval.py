"""扫描件 OCR → 抽取 的离线评分（第 5 步，本地工具，可入库）。

用途：拿人工核过的扫描件真值（data/素材/ocr/ocr_gt.json）对比一次跑批产物里
的抽取字段，量化"OCR 出来的文本能不能被正确抽取"——这样"支持扫描件"才是有数字的
结论，而不是口头承诺。

特点：本脚本**不调用任何 API、也不跑 OCR**，只读已有产物（run_generalization 的 JSON）；
所以可以随时复算，不花钱、可复现。GT 只对"写了的字段"评分（没写的字段跳过，
例如原文没有独立签署日期栏就不设日期真值）。

用法：
    python -m backend.eval.run_ocr_eval                 # 用最新一份 generalization 产物
    python -m backend.eval.run_ocr_eval --run <产物.json>
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backend.app.config import BASE_DIR

DEFAULT_GT = BASE_DIR / "data/素材/ocr/ocr_gt.json"
DEFAULT_OUT = BASE_DIR / "backend/eval/output"
# 跑批产物默认目录（取最新的 generalization_*.json 当输入）
RUN_DIR = DEFAULT_OUT


def _norm_name(value) -> str:
    """机构名归一：去空白与常见全角/半角标点，便于跨 OCR 空格差异比较。"""
    if not value:
        return ""
    text = str(value)
    for ch in " \u3000\t\n()（）·,，。":
        text = text.replace(ch, "")
    return text


def _norm_amount(value) -> Decimal | None:
    """金额归一为 Decimal：容忍千分位、全角逗号与"元/万元"字样；解析失败返回 None。"""
    if value is None or value == "":
        return None
    text = str(value).replace(",", "").replace("，", "").replace("¥", "").replace("￥", "").strip()
    text = text.replace("元", "").strip()
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _norm_date(value) -> str:
    """日期归一为 ISO 串：支持 date/datetime 与 2025-11-18 / 2025年11月18日 两种文本。"""
    if value is None or value == "":
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    for sep in ("年", "月", "/", "."):
        text = text.replace(sep, "-")
    text = text.replace("日", "")
    parts = [p for p in text.split("-") if p != ""]
    if len(parts) == 3:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return text


def _outcome(field: str, expected, actual) -> str:
    """单字段判定：ok / wrong / missing（口径与 run_eval 的字段尺子一致，便于互相对照）。"""
    # 分支 1：抽取为空 → 缺失（OCR 全丢或模型没抄到）
    if actual in (None, "", []):
        return "missing"
    # 分支：机构名按归一后比较（OCR 会在名称里插空格/换行）
    if field in ("buyer", "supplier"):
        return "ok" if _norm_name(expected) == _norm_name(actual) else "wrong"
    # 分支：金额按数值比较（千分位/全角逗号/￥ 都容忍）
    if field == "total_amount":
        exp, act = _norm_amount(expected), _norm_amount(actual)
        return "ok" if exp is not None and exp == act else "wrong"
    # 分支：日期按 ISO 比较
    if field == "signature_date":
        return "ok" if _norm_date(expected) == _norm_date(actual) else "wrong"
    return "ok" if str(expected) == str(actual) else "wrong"


def _latest_run() -> Path | None:
    """取输出目录里最新的 generalization 产物（没有则返回 None）。"""
    files = sorted(RUN_DIR.glob("generalization_*.json"))
    return files[-1] if files else None


def evaluate(gt_path: Path, run_path: Path) -> dict:
    """读 GT 与跑批产物 → 逐份逐字段判定，返回结果 dict（含总体准确率）。"""
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    run = json.loads(run_path.read_text(encoding="utf-8"))
    # 产物按文件名索引：GT/产物都按文件名字符串对齐
    items = run.get("files", [])
    rows: list[dict] = []
    counters = {"ok": 0, "wrong": 0, "missing": 0}
    for entry in gt.get("files", []):
        observed = _find_run_item(entry["file"], items)
        if observed is None:
            rows.append({"file": entry["file"], "error": "产物里没有该文件（未跑或文件名不符）", "fields": {}})
            continue
        extracted = observed.get("extracted") or {}
        fields: dict[str, dict] = {}
        for field in ("buyer", "supplier", "total_amount", "signature_date"):
            # 分支：GT 没写该字段 → 跳过（原文没有该内容，不该算错）
            if field not in entry:
                continue
            outcome = _outcome(field, entry[field], extracted.get(field))
            counters[outcome] += 1
            fields[field] = {
                "outcome": outcome,
                "expected": entry[field],
                "actual": extracted.get(field),
            }
        rows.append({
            "file": entry["file"],
            "grade": observed.get("grade"),
            "high_types": observed.get("high_types"),
            "fields": fields,
        })
    total = sum(counters.values())
    return {
        "gt": str(gt_path),
        "run": str(run_path),
        "counters": counters,
        "field_accuracy": round(counters["ok"] / total, 4) if total else None,
        "files": rows,
    }


def _find_run_item(gt_file: str, items: list[dict]) -> dict | None:
    """在产物里找 GT 对应的那份：先精确比文件名，找不到再按关键词包含比。

    为什么要兜底（2026-09-11 素材重命名）：尺子按文件名对齐，改名后旧产物就对不上，
    报"产物里没有该文件"而不是给分数。这里把 "扫描件_04_医用设备.pdf" 这类名字
    去掉前缀与序号后取关键词（"医用设备"）做包含匹配，改名/换批次都不再影响复算。
    """
    exact = next((i for i in items if i.get("file") == gt_file), None)
    if exact is not None:
        return exact
    core = re.sub(r"^(?:扫描件_)?\d+_", "", Path(gt_file).stem)
    if not core:
        return None
    return next((i for i in items if core in (i.get("file") or "")), None)


def main(argv: list[str] | None = None) -> int:
    """CLI：读 GT + 产物 → 打印逐字段结果 → 写评分产物（全部本地，不花钱）。"""
    parser = argparse.ArgumentParser(description="扫描件 OCR→抽取 离线评分")
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT, help="扫描件真值 JSON")
    parser.add_argument("--run", type=Path, default=None, help="跑批产物 JSON（默认取最新）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="评分产物输出目录")
    args = parser.parse_args(argv)

    run_path = args.run or _latest_run()
    if run_path is None or not run_path.is_file():
        print("找不到跑批产物，请先跑 run_generalization --include-scans", flush=True)
        return 1
    if not args.gt.is_file():
        print(f"找不到真值文件: {args.gt}", flush=True)
        return 1

    result = evaluate(args.gt, run_path)
    print(f"扫描件字段尺子：{result['counters']}  整体准确率={result['field_accuracy']}")
    for row in result["files"]:
        if row.get("error"):
            print(f" - {row['file']}: {row['error']}")
            continue
        bits = [f"{k}={v['outcome']}(期望 {v['expected']} / 实得 {v['actual']})"
                for k, v in row["fields"].items()]
        print(f" - {row['file'][:34]:36s} 评级={row['grade']} | " + "; ".join(bits))

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_json = args.out / f"ocr_eval_{stamp}.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
