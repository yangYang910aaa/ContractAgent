"""政策库命令行：政策库版本、文件 ↔ 索引一致性核对、按份同步。

用法：
    python -m backend.app.policy_admin --check        # 核对一致性（只读，0 次模型调用）
    python -m backend.app.policy_admin --sync         # 只打印同步计划，不改库
    python -m backend.app.policy_admin --sync --yes   # 执行同步（只重写有变化的文件）
    python -m backend.app.policy_admin --fingerprint  # 只打印政策库版本号

同步是"按份替换"：变化的文件先按文件名删掉旧单元再重插，磁盘上已删的文件清掉库内行，
没变化的文件一个单元都不动——不做清库重建，分条与检索仍走原实现，不动检索口径。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.app.policy_corpus import (
    compare_corpus_and_index,
    corpus_fingerprint,
    corpus_units,
)
from backend.app.policy_rag import get_store


def build_sync_plan(state: dict, units_by_source: dict[str, list]) -> list[dict]:
    """按核对结果列同步动作：变化的文件整份替换，磁盘上已无的文件清库内行。

    输入是 compare_corpus_and_index 的结果与按文件分组的磁盘单元；返回动作清单，
    未变化的文件不进清单——这是"不覆盖"的落点。
    """
    steps: list[dict] = []
    for item in state["files_detail"]:
        # 分支：这份文件磁盘与索引逐单元一致 → 一个单元都不动
        if item["same"]:
            continue
        steps.append(
            {
                "action": "replace" if item["disk_units"] else "drop",
                "source": item["source"],
                "disk_units": item["disk_units"],
                "index_units": item["index_units"],
                "units": units_by_source.get(item["source"], []),
            }
        )
    return steps


def apply_sync(store, steps: list[dict]) -> list[dict]:
    """执行同步动作：先删该文件旧单元再重插新单元，逐份报告删除与写入条数。"""
    results: list[dict] = []
    for step in steps:
        removed = store.delete_source(step["source"])
        written = 0
        # 分支：文件还在磁盘上 → 删完接着把新版单元写回去；已被删除的文件就到此为止
        if step["action"] == "replace":
            written = store.add_docs(step["units"])
        results.append(
            {"source": step["source"], "removed": removed, "written": written}
        )
    return results


def _print_check(state: dict, documents: list[dict]) -> None:
    """打印核对结果：先逐份列出编号/版本/条数，再给一致性结论与差异清单。"""
    print(f"政策库版本 {state['version']} · {state['files']} 份 / {state['disk_units']} 个检索单元")
    print("文件\t编号\t版本\t生效日期\t单元\t索引")
    units_by_source = {doc["source"]: doc for doc in documents}
    for item in state["files_detail"]:
        doc = units_by_source.get(item["source"], {})
        mark = "" if item["same"] else f"  ← 缺 {item['missing']} / 多 {item['extra']}"
        print(
            f"{item['source']}\t{doc.get('ref', '-')}\t{doc.get('version') or '-'}"
            f"\t{doc.get('effective_date') or '-'}\t{item['disk_units']}\t{item['index_units']}{mark}"
        )
    # 分支：库里存在磁盘上已没有的文件 → 单独列出（清库前遗留的孤儿行）
    if state["orphan_sources"]:
        print("磁盘上已无、索引里还在的文件：" + "、".join(state["orphan_sources"]))
    verdict = "一致" if state["ok"] else "不一致：需要跑 --sync"
    print(f"索引核对：{verdict}（磁盘 {state['disk_units']} 条 / 索引 {state['index_units']} 条）")


def _print_plan(steps: list[dict]) -> None:
    """打印同步计划：哪些文件会被替换、哪些会被清行。"""
    if not steps:
        print("无需同步：文件与索引逐单元一致")
        return
    print(f"同步计划（{len(steps)} 份文件）：")
    for step in steps:
        # 分支：文件还在磁盘上 → 整份替换；磁盘上已删 → 只清库内行
        if step["action"] == "replace":
            print(f"  替换 {step['source']}：删旧 {step['index_units']} 条 → 写新 {step['disk_units']} 条")
        else:
            print(f"  清除 {step['source']}：磁盘已无此文件，删库内 {step['index_units']} 条")


def _check(store, policy_dir, as_json: bool) -> int:
    """核对入口：输出结果，不一致时返回非 0 退出码（便于脚本拦截）。"""
    state = compare_corpus_and_index(store=store, policy_dir=policy_dir)
    if as_json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        _print_check(state, corpus_fingerprint(policy_dir=policy_dir)["documents"])
    return 0 if state["ok"] else 1


def _sync(store, policy_dir, confirmed: bool, as_json: bool) -> int:
    """同步入口：默认只打印计划（dry-run），加 --yes 才真正改库，改完自动再核对一次。"""
    state = compare_corpus_and_index(store=store, policy_dir=policy_dir)
    units_by_source: dict[str, list] = {}
    for unit in corpus_units(policy_dir=policy_dir):
        units_by_source.setdefault(unit.source, []).append(unit)
    steps = build_sync_plan(state, units_by_source)
    # JSON 里不带单元列表（那是完整正文，没必要输出）
    brief = [{k: v for k, v in step.items() if k != "units"} for step in steps]
    # 分支：没有给 --yes 或本来就没有差异 → 只出计划，不动库
    if not steps or not confirmed:
        if as_json:
            print(json.dumps(
                {"version": state["version"], "applied": False, "steps": brief},
                ensure_ascii=False, indent=2,
            ))
        else:
            _print_plan(steps)
            if steps:
                print("（未执行：确认无误后加 --yes）")
        return 0
    results = apply_sync(store, steps)
    after = compare_corpus_and_index(store=store, policy_dir=policy_dir)
    if as_json:
        print(json.dumps(
            {
                "version": state["version"],
                "applied": True,
                "steps": brief,
                "results": results,
                "check": after["ok"],
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_plan(steps)
        for result in results:
            print(f"已处理 {result['source']}：删除 {result['removed']} 条 / 写入 {result['written']} 条")
        print(f"同步后核对：{'一致' if after['ok'] else '仍不一致'}")
    return 0 if after["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：--check 核对 / --sync 按份同步 / --fingerprint 打印版本号。"""
    parser = argparse.ArgumentParser(description="政策库语料指纹、一致性核对与按份同步")
    parser.add_argument("--check", action="store_true", help="核对文件与索引是否一致（默认动作）")
    parser.add_argument("--sync", action="store_true", help="按份同步有变化的语料")
    parser.add_argument("--fingerprint", action="store_true", help="只打印政策库版本号")
    parser.add_argument("--yes", action="store_true", help="与 --sync 同用：确认执行，否则只打印计划")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（脚本/评测引用）")
    parser.add_argument("--backend", default=None, choices=["auto", "memory", "milvus"], help="检索后端")
    parser.add_argument("--policy-dir", default=None, help="语料目录（默认 data/policies；演练可指到副本）")
    args = parser.parse_args(argv)
    policy_dir = Path(args.policy_dir) if args.policy_dir else None

    # 分支：只要版本号 → 不连向量库，纯读盘
    if args.fingerprint and not args.sync and not args.check:
        fingerprint = corpus_fingerprint(policy_dir=policy_dir)
        # 分支：--json → 连清单一起给脚本；否则只给版本号
        if args.json:
            print(json.dumps(fingerprint, ensure_ascii=False, indent=2))
        else:
            print(fingerprint["version"])
        return 0

    store = get_store(backend=args.backend)
    if args.sync:
        return _sync(store, policy_dir, args.yes, args.json)
    return _check(store, policy_dir, args.json)


if __name__ == "__main__":
    sys.exit(main())
