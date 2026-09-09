"""Phase 4 评测闭环 runner(本地评测工具, 可入库)。

用途: 在"变体语料(12 份) + 合成 sample(9 份)"上跑离线流水线, 按 ground_truth
算 检出率 / 零误报率 / 风险类型级 macro-F1 / 评级准确率, 并量化 LLM 抽取波动
(每份跑 N 次取均值 + [min,max] 区间)。只走 pipeline.run_review(CLI 离线,
无 TaskManager 队列开销, 评测推荐, 见 交接文档第六节)。

用法:
    python -m backend.eval.run_eval                # 全语料默认 3 次, 单审
    python -m backend.eval.run_eval --runs 5       # 调次数
    python -m backend.eval.run_eval --review-mode double  # 双审盲审模式
    python -m backend.eval.run_eval --compare      # 同语料跑 single+double 并输出对比
    python -m backend.eval.run_eval --only tech_02 # 只跑单份/名字子串
    python -m backend.eval.run_eval --set samples  # 只看 sample md
    python -m backend.eval.run_eval --check        # 离线: 校验 GT + 语料在位
    python -m backend.eval.run_eval --list         # 离线: 列评测语料清单

指标口径(high 级判定, 与 D21/交接文档一致):
- 检出率: 缺陷文件里"期望 high 是否判成 high"的比例(必须命中且 severity=high);
- 零误报率: 无期望 high 的文件(正常/模板/半填)整份没出现任何 high 的比例;
- 风险类型级 macro-F1: 以 (文件, 某次运行) 为样本, 按 risk_type 累加 high 级
  TP/FP/FN 后宏平均(类型未在任何期望/预测出现则跳过);
- 评级准确率: expected_grade == 实测 grade。
每份文件跑 N 次, 头部指标取 N 次(每次=全语料一遍)的均值与 [min,max] 波动区间。
judge=false 的样本(tech_01 真实合同, P-03 口径未定)只记录观察、不判分。
产物(JSON + 人读摘要)默认写 backend/eval/output/(gitignore, 不入库)。

双审对比口径(D26): --compare 在同一语料上分别以 review_mode=single/double 各跑
N 次; double 只比 single 多 1 次盲审 LLM 调用。除头部指标并列对比外, 逐文件给
"double 相对 single 的漏检/误报差异"明细——这是 README「为什么多 agent」的量化行。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.config import BASE_DIR
from backend.app.pipeline import run_review
from backend.app.schemas import Grade, Severity
from backend.app.rules import RISK_LABELS

# 语料根目录与 GT 默认位置(均为本地数据, 不入库; D21 口径只服务回归/评测)
VARIANTS_DIR = BASE_DIR / "data/合同模板/合同变体/out"
SAMPLES_DIR = BASE_DIR / "data/contracts"
DEFAULT_GT = VARIANTS_DIR / "ground_truth.json"
DEFAULT_OUT = BASE_DIR / "backend/eval/output"

_GRADES = {g.value for g in Grade}
_SEVERITIES = {s.value for s in Severity}
_REVIEW_MODES = ("single", "double")


@dataclass
class GtEntry:
    """一条 ground truth: 期望评级 + 期望风险类型(risk_type -> 最低期望严重级)。"""

    file: str  # 语料文件名(相对所属 set 目录)
    set_: str  # variants | samples
    kind: str  # 合同品类(信息用, 不参与判分)
    expected_grade: str | None  # pass/conditional_pass/fail; None=不判评级
    expected_types: dict[str, str] = field(default_factory=dict)  # {risk_type: severity}
    judge: bool = True  # False=只观察不判分(如真实合同 tech_01)
    why: str = ""  # 期望依据(人工说明, 供走查)
    path: Path | None = None  # 解析后的文件绝对路径(load_gt 后填充)

    @property
    def expected_highs(self) -> set[str]:
        """期望为 high 的风险类型集合(检出/F1 只统计这一档)。"""
        return {t for t, sev in self.expected_types.items() if sev == Severity.high.value}

    @property
    def expects_no_high(self) -> bool:
        """是否属于"零误报"分母: 判分且不期望任何 high。"""
        return self.judge and not self.expected_highs


def _load_json(path: Path) -> list[GtEntry]:
    """读 GT JSON → GtEntry 列表(宽松容错: judge 缺省 True, 未知键忽略)。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[GtEntry] = []
    for item in raw.get("files", []):
        entry = GtEntry(
            file=item["file"],
            set_=item.get("set", "variants"),
            kind=item.get("kind", ""),
            expected_grade=item.get("expected_grade"),
            expected_types=dict(item.get("expected_types") or {}),
            judge=bool(item.get("judge", True)),
            why=item.get("why", ""),
        )
        out.append(entry)
    return out


def _resolve(entry: GtEntry) -> Path:
    """按 set 把文件名解析成绝对路径(variants 在 out/, samples 在 data/contracts)。"""
    base = VARIANTS_DIR if entry.set_ == "variants" else SAMPLES_DIR
    return base / entry.file


def _validate(entries: list[GtEntry], quiet: bool = False) -> list[str]:
    """离线校验 GT: 文件在位、risk_type/severity/grade 取值合法、无重复。"""
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        # 这种情况是: 文件名重复(同一语料两行 GT) → 判分口径会打架
        if (entry.set_, entry.file) in seen:
            problems.append(f"重复条目: {entry.set_}/{entry.file}")
        seen.add((entry.set_, entry.file))
        if entry.expected_grade not in _GRADES | {None}:
            problems.append(f"{entry.file}: expected_grade 非法 {entry.expected_grade!r}")
        # 这种情况是: GT 用了 rules 里没登记的类型 → 评测永远对不上(易错点)
        for risk_type, sev in entry.expected_types.items():
            if risk_type not in RISK_LABELS:
                problems.append(f"{entry.file}: risk_type 未登记 {risk_type!r}")
            if sev not in _SEVERITIES:
                problems.append(f"{entry.file}: severity 非法 {sev!r}")
        if not entry.path or not entry.path.is_file():
            problems.append(f"{entry.file}: 文件不在位({entry.path})")
    if not quiet:
        print(f"GT 校验: {len(entries)} 条, 问题 {len(problems)} 个")
        for p in problems:
            print("  -", p)
    return problems


def _observed(entry: GtEntry, report: dict) -> dict:
    """单次 run_review 报告 → 判分所需观测(grade/high 类型集合/错误)。"""
    risks = report.get("risks") or []
    return {
        "grade": report.get("grade"),
        "high_types": {r["risk_type"] for r in risks if r.get("severity") == Severity.high.value},
        "error": report.get("error"),
    }


def _run_level_metrics(entries: list[GtEntry], observed: list[dict]) -> dict:
    """对"一次全语料运行"的观测算四个头部指标(纯函数, 便于单测)。

    observed 与 entries 一一对应(只含 judge=True 的条目, 调用方先过滤)。
    """
    # ---- 检出率: 缺陷文件期望 high 命中(且判为 high)的比例 ----
    exp_hits = sum(len(e.expected_highs) for e in entries)
    det_hits = sum(
        len(e.expected_highs & obs["high_types"]) for e, obs in zip(entries, observed)
    )
    detection = det_hits / exp_hits if exp_hits else None

    # ---- 零误报率: 无期望 high 文件里, 没出现任何 high 的比例 ----
    clean = [(e, obs) for e, obs in zip(entries, observed) if e.expects_no_high]
    clean_ok = sum(1 for _, obs in clean if not obs["high_types"])
    zero_fp = clean_ok / len(clean) if clean else None

    # ---- 风险类型级 macro-F1(high 级; 类型在期望或预测出现过才算) ----
    universe: set[str] = set()
    for e, obs in zip(entries, observed):
        universe |= e.expected_highs | obs["high_types"]
    per_type: dict[str, dict] = {}
    for risk_type in sorted(universe):
        tp = fp = fn = 0
        for e, obs in zip(entries, observed):
            expected = risk_type in e.expected_highs
            predicted = risk_type in obs["high_types"]
            # 分支: 期望且命中 → TP; 期望未命中 → FN; 未期望却命中 → FP
            if expected and predicted:
                tp += 1
            elif expected:
                fn += 1
            elif predicted:
                fp += 1
        if tp + fp + fn == 0:
            continue
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_type[risk_type] = {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        }
    f1s = [v["f1"] for v in per_type.values()]
    macro_f1 = sum(f1s) / len(f1s) if f1s else None

    # ---- 评级准确率(期望评级非空才判) ----
    graded = [(e, obs) for e, obs in zip(entries, observed) if e.expected_grade]
    grade_ok = sum(1 for e, obs in graded if obs["grade"] == e.expected_grade)
    grade_acc = grade_ok / len(graded) if graded else None

    return {
        "detection_rate": round(detection, 4) if detection is not None else None,
        "zero_fp_rate": round(zero_fp, 4) if zero_fp is not None else None,
        "macro_f1": round(macro_f1, 4) if macro_f1 is not None else None,
        "grade_accuracy": round(grade_acc, 4) if grade_acc is not None else None,
        "per_type": per_type,
    }


def _collapse(values: list[float | None]) -> dict | None:
    """N 次运行的同名指标 → {mean, min, max, runs}; 全 None 时返回 None。"""
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return {
        "mean": round(sum(nums) / len(nums), 4),
        "min": round(min(nums), 4),
        "max": round(max(nums), 4),
        "runs": [v for v in values],
    }


def _to_jsonable(value):
    """递归把结果里的 set 转排序列表(JSON 不认 set; 排序保证输出稳定)。

    判分过程内部用 set 做集合运算(high_types/expected_highs), 落盘前统一转换。
    """
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def _summary_line(entry: GtEntry, states: list[dict]) -> str:
    """单份文件的人读摘要: 评级/检出/误报, 供控制台与 PRD 回填核对。"""
    grades = [s["grade"] for s in states]
    parts = [entry.file, f"期望={entry.expected_grade}"]
    if entry.judge:
        ok = sum(1 for g in grades if g == entry.expected_grade)
        parts.append(f"评级{ok}/{len(states)}")
    # 每个期望 high 类型都标注"几次判为 high"(波动看这里最直观)
    for risk_type in sorted(entry.expected_highs):
        hits = sum(1 for s in states if risk_type in s["high_types"])
        parts.append(f"{risk_type}={hits}/{len(states)}")
    # 无期望 high 却出现的 high(误报)一并列出
    if entry.expects_no_high:
        fps = sorted({t for s in states for t in s["high_types"]})
        if fps:
            parts.append(f"误报high={fps}")
    errors = [s["error"] for s in states if s["error"]]
    if errors:
        parts.append(f"错误x{len(errors)}:{errors[0][:60]}")
    return " | ".join(parts)


def _run_mode(
    entries: list[GtEntry],
    judged: list[GtEntry],
    runs: int,
    mode: str,
    label: str = "",
) -> tuple[list[dict], list[dict]]:
    """逐份逐次跑某 review_mode 的离线流水线, 返回 (per_file, run_metrics)。

    label 用于 --compare 时区分 single/double 的进度行(单跑传空串保持旧输出)。
    抽取异常由 run_review 转 error 报告, 不中断批处理。
    """
    per_file: list[dict] = []
    run_metrics: list[dict] = []
    for idx, entry in enumerate(entries, start=1):
        states: list[dict] = []
        for run_no in range(1, runs + 1):
            started = time.perf_counter()
            report = run_review(entry.path, review_mode=mode)
            state = _observed(entry, report)
            state["seconds"] = round(time.perf_counter() - started, 1)
            state["run"] = run_no
            states.append(state)
            print(f"{label}  [{idx}/{len(entries)}] {entry.file} run{run_no} "
                  f"grade={state['grade']} high={sorted(state['high_types']) or '-'} "
                  f"{state['seconds']}s", flush=True)
        per_file.append({
            "file": entry.file,
            "set": entry.set_,
            "kind": entry.kind,
            "judge": entry.judge,
            "expected_grade": entry.expected_grade,
            "expected_types": entry.expected_types,
            "why": entry.why,
            "states": states,
            "summary": _summary_line(entry, states),
        })
        # 判分文件参与每次全语料指标
        if entry.judge:
            run_metrics.append({s["run"]: s for s in states})
    return per_file, run_metrics


def _aggregate(judged: list[GtEntry], run_metrics: list[dict], mode: str) -> tuple[dict, dict]:
    """汇总某模式 N 次运行: 打印头部指标/类型级/单份明细, 返回 (metrics, per_type_merged)。"""
    # 每次运行(全语料一遍)是一个独立的指标样本 → N 个样本取均值与波动区间
    headline: dict = {}
    for run_no in range(1, len(run_metrics[0]) + 1):
        observed = [run_metrics[i][run_no] for i in range(len(judged))]
        headline[run_no] = _run_level_metrics(judged, observed)
    metrics = {
        "detection_rate": _collapse([m["detection_rate"] for m in headline.values()]),
        "zero_fp_rate": _collapse([m["zero_fp_rate"] for m in headline.values()]),
        "macro_f1": _collapse([m["macro_f1"] for m in headline.values()]),
        "grade_accuracy": _collapse([m["grade_accuracy"] for m in headline.values()]),
    }

    print(f"\n===== 汇总 {mode}(每次=全语料跑一遍) =====")
    for name, col in metrics.items():
        if col:
            print(f"{name}: mean={col['mean']} 波动=[{col['min']},{col['max']}] "
                  f"runs={col['runs']}")
    print("\n===== 类型级明细(合并 N 次运行) =====")
    merged_per_type: dict[str, dict] = {}
    for run_metric in headline.values():
        for risk_type, row in run_metric["per_type"].items():
            acc = merged_per_type.setdefault(risk_type, {"tp": 0, "fp": 0, "fn": 0})
            for key in ("tp", "fp", "fn"):
                acc[key] += row[key]
    for risk_type, acc in sorted(merged_per_type.items()):
        tp, fp, fn = acc["tp"], acc["fp"], acc["fn"]
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        print(f"  {risk_type}: tp={tp} fp={fp} fn={fn} "
              f"precision={p:.3f} recall={r:.3f} f1={f1:.3f}")
    return metrics, merged_per_type


def _print_single_summaries(per_file: list[dict], mode: str | None = None) -> None:
    """打印单份明细行；mode 给定时标题标注所属模式（--compare 两段并存用）。"""
    label = f" [{mode}]" if mode else ""
    print(f"\n===== 单份明细{label} =====")
    for row in per_file:
        print(" -", row["summary"])


def _mode_label(mode: str) -> str:
    """头部指标行/产物里的人读模式名(与 CLI 取值一致)。"""
    return {"single": "单审 single", "double": "双审 double"}.get(mode, mode)


def _compare_modes(judged: list[GtEntry], run_metrics_a: list[dict], run_metrics_b: list[dict]) -> dict:
    """single/double 逐文件逐次 diff: double 相对 single 的漏检/误报变化。

    以期望 high 为准算"漏检"：single 漏而 double 命中 → double 改善；
    double 出现 single 没有的 high → 记录差异供人工看(可能复核补漏也可能误报)。
    """
    deltas: list[dict] = []
    totals = {"improved_runs": 0, "regressed_runs": 0, "extra_high_runs": 0}
    for idx, entry in enumerate(judged):
        exp = entry.expected_highs
        row: dict = {"file": entry.file, "runs": []}
        for run_no in range(1, len(run_metrics_a[idx]) + 1):
            sa = run_metrics_a[idx][run_no]
            sb = run_metrics_b[idx][run_no]
            missed_a = exp - sa["high_types"]
            missed_b = exp - sb["high_types"]
            gained = missed_a - missed_b  # double 多抓的期望漏检
            lost = missed_b - missed_a  # double 反而漏的
            extras = sb["high_types"] - sa["high_types"]  # double 新增的 high(任意来源)
            if gained or lost or extras:
                totals["improved_runs"] += bool(gained)
                totals["regressed_runs"] += bool(lost)
                totals["extra_high_runs"] += bool(extras)
                row["runs"].append({
                    "run": run_no,
                    "gained": sorted(gained),
                    "lost": sorted(lost),
                    "extra_highs": sorted(extras),
                })
        if row["runs"]:
            deltas.append(row)
    return {"totals": totals, "files": deltas}


def _main_compare(entries: list[GtEntry], judged: list[GtEntry], runs: int, out_dir: Path) -> int:
    """--compare 主流程: single/double 各跑 N 次 → 并列指标 + 逐文件差异 → 落盘。"""
    results: dict[str, dict] = {}
    for mode in _REVIEW_MODES:
        print(f"\n########## review_mode={mode} ##########")
        per_file, run_metrics = _run_mode(entries, judged, runs, mode, label=f"[{mode}]")
        metrics, merged_per_type = _aggregate(judged, run_metrics, mode)
        results[mode] = {
            "per_file": per_file,
            "run_metrics": run_metrics,
            "metrics": metrics,
            "per_type_merged": merged_per_type,
        }

    # ---- 并列对比表(mean 列; 波动区间同单跑) ----
    print("\n===== single vs double 头部指标对比(mean) =====")
    names = ("detection_rate", "zero_fp_rate", "macro_f1", "grade_accuracy")
    labels = ("检出率", "零误报率", "macro-F1", "评级准确率")
    for name, label in zip(names, labels):
        cols = []
        for mode in _REVIEW_MODES:
            col = results[mode]["metrics"][name]
            cols.append(f"{col['mean']:.4f}" if col else "-")
        delta = ""
        a, b = results["single"]["metrics"][name], results["double"]["metrics"][name]
        if a and b and a["mean"] != b["mean"]:
            delta = f"  (double {'+' if b['mean'] > a['mean'] else ''}{b['mean'] - a['mean']:.4f})"
        print(f"{label:8s}: single={cols[0]} double={cols[1]}{delta}")

    # ---- 漏检率对比行(检出率互补口径, README 展示用) ----
    det_a = results["single"]["metrics"]["detection_rate"]
    det_b = results["double"]["metrics"]["detection_rate"]
    if det_a and det_b:
        print(f"\n漏检率对比(1-检出率): single={1 - det_a['mean']:.4f} "
              f"double={1 - det_b['mean']:.4f}")

    print("\n===== double 相对 single 逐文件差异 =====")
    for mode in _REVIEW_MODES:
        _print_single_summaries(results[mode]["per_file"], mode=mode)
    comparison = _compare_modes(
        judged, results["single"]["run_metrics"], results["double"]["run_metrics"]
    )
    print(f"\n差异统计: 改善(漏检补抓){comparison['totals']['improved_runs']} 次 / "
          f"回退(新漏){comparison['totals']['regressed_runs']} 次 / "
          f"新增 high{comparison['totals']['extra_high_runs']} 次")
    for row in comparison["files"]:
        print(f" - {row['file']}")
        for run_delta in row["runs"]:
            bits = []
            if run_delta["gained"]:
                bits.append("补抓漏检=" + ",".join(run_delta["gained"]))
            if run_delta["lost"]:
                bits.append("新漏=" + ",".join(run_delta["lost"]))
            if run_delta["extra_highs"]:
                bits.append("新增high=" + ",".join(run_delta["extra_highs"]))
            print(f"    run{run_delta['run']}: {'; '.join(bits)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runs": runs,
        "modes": {mode: results[mode]["metrics"] for mode in _REVIEW_MODES},
        "per_type_merged": {
            mode: results[mode]["per_type_merged"] for mode in _REVIEW_MODES
        },
        "comparison": comparison,
        "files": {mode: results[mode]["per_file"] for mode in _REVIEW_MODES},
    }
    out_json = out_dir / f"run_eval_compare_{stamp}.json"
    out_json.write_text(
        json.dumps(_to_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n对比产物: {out_json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口: 校验 → 逐份跑 N 次 → 汇总指标 → 写产物。"""
    parser = argparse.ArgumentParser(description="Phase 4 评测闭环(run_eval)")
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT, help="ground_truth.json 路径")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="产物输出目录")
    parser.add_argument("--runs", type=int, default=3, help="每份文件跑几次(默认 3)")
    parser.add_argument("--set", choices=("variants", "samples", "all"), default="all")
    parser.add_argument("--only", default="", help="只跑文件名含该子串的语料(调试用)")
    parser.add_argument(
        "--review-mode",
        choices=_REVIEW_MODES,
        default="single",
        help="review_mode: single=主审(默认) / double=主审+独立盲审复核",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="同语料跑 single+double 各 N 次, 输出漏检率对比(README 亮点行)",
    )
    parser.add_argument("--check", action="store_true", help="离线校验 GT 后退出")
    parser.add_argument("--list", action="store_true", help="离线列出评测语料后退出")
    args = parser.parse_args(argv)

    gt_path = Path(args.gt)
    if not gt_path.is_file():
        print(f"找不到 GT: {gt_path}", file=sys.stderr)
        return 1
    entries = [e for e in _load_json(gt_path) if args.set in ("all", e.set_)]
    for entry in entries:
        entry.path = _resolve(entry)
    problems = _validate(entries, quiet=args.list)
    if args.check:
        return 0 if not problems else 2
    if args.list:
        for e in entries:
            print(f"[{e.set_}] {e.file} -> 期望 {e.expected_grade}, judge={e.judge}")
        return 0
    if problems:
        print("GT 有问题, 先修再跑(--check 看明细)", file=sys.stderr)
        return 2

    if args.only:
        entries = [e for e in entries if args.only in e.file]
    judged = [e for e in entries if e.judge]
    if not judged:
        print("没有可判分的语料, 退出", file=sys.stderr)
        return 1
    print(f"评测语料 {len(entries)} 份(判分 {len(judged)}), 每份跑 {args.runs} 次, "
          f"review_mode={args.review_mode if not args.compare else 'single+double'}")

    # 这种情况是: --compare → 双模式各跑一遍并输出对比, 落盘单个对比产物
    if args.compare:
        return _main_compare(entries, judged, args.runs, args.out)

    per_file, run_metrics = _run_mode(entries, judged, args.runs, args.review_mode)
    metrics, merged_per_type = _aggregate(judged, run_metrics, _mode_label(args.review_mode))
    _print_single_summaries(per_file)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    result = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gt": str(gt_path),
        "runs": args.runs,
        "review_mode": args.review_mode,
        "metrics": metrics,
        "per_type_merged": merged_per_type,
        "files": per_file,
    }
    out_json = args.out / f"run_eval_{args.review_mode}_{stamp}.json"
    out_json.write_text(
        json.dumps(_to_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n产物: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
