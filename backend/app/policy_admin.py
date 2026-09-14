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
from backend.app.config import settings
from backend.app.policy_rag import MilvusStore, get_store, unit_id

# 业务键改造后的物理集合与回滚用旧名：检索侧一直认 settings.milvus_collection（别名）
REBUILD_COLLECTION = "contract_policies_biz"
LEGACY_COLLECTION = "contract_policies_legacy"


def plan_unit_sync(disk_units: list, index_rows: list[dict]) -> dict:
    """按业务键算增量：要写的单元（磁盘有、索引没有）与要删的单元（索引有、磁盘没有）。

    有业务键之后同步的最小单位是"单元"：改一条条文只重算那一条的向量，
    同文件里没变的条文一个都不动（旧口径是按整份文件删掉重插）。
    """
    index_keys = {row.get("unit_id") for row in index_rows if row.get("unit_id")}
    disk_keys = {unit_id(unit.source, unit.text) for unit in disk_units}
    return {
        "write": [unit for unit in disk_units if unit_id(unit.source, unit.text) not in index_keys],
        "delete": sorted(index_keys - disk_keys),
        "index_source": {row.get("unit_id"): row.get("source", "") for row in index_rows},
    }


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


def _rebuild(confirmed: bool, as_json: bool) -> int:
    """一次性重建为业务键集合，并用别名接管检索用的名字（可回滚）。

    顺序刻意如此：新集合建好并核对通过之后才动检索名字——中途失败时线上仍指向旧集合；
    旧集合改名保留，回滚就是"删别名 + 把旧名改回来"。别名可读可写（已实测），
    所以检索与同步都能照旧走 settings.milvus_collection。
    """
    alias = settings.milvus_collection
    client = MilvusStore(collection_name=alias).client
    units = corpus_units()
    steps = [
        f"新建集合 {REBUILD_COLLECTION}（业务键主键 unit_id + 标量字段）",
        f"灌入磁盘语料 {len(units)} 个单元并核对一致",
        f"把现有集合 {alias} 改名为 {LEGACY_COLLECTION}（保留作回滚点）",
        f"建别名 {alias} → {REBUILD_COLLECTION}（代码与 .env 都不改）",
        f"回滚办法：drop_alias {alias}，再把 {LEGACY_COLLECTION} 改回 {alias}",
    ]
    # 分支：检索名字已经是别名 → 说明已经迁移过，不再动
    if alias in client.list_aliases().get("aliases", []):
        message = {"alias": alias, "migrated": True, "steps": ["已经是别名形态，无需重建"]}
        print(json.dumps(message, ensure_ascii=False, indent=2) if as_json else f"{alias} 已经是别名形态，无需重建")
        return 0
    if as_json:
        print(json.dumps({"alias": alias, "target": REBUILD_COLLECTION, "units": len(units), "steps": steps},
                         ensure_ascii=False, indent=2))
    else:
        print(f"重建计划（{alias} → {REBUILD_COLLECTION}，{len(units)} 个单元）：")
        for step in steps:
            print(f"  - {step}")
    # 分支：没给 --yes → 只出计划，不动库
    if not confirmed:
        print("（未执行：确认无误后加 --yes）")
        return 0

    # 1) 新集合建好并核对通过之后才动线上名字
    if client.has_collection(REBUILD_COLLECTION):
        print(f"{REBUILD_COLLECTION} 已存在（上次失败残留），先删除重建")
        client.drop_collection(REBUILD_COLLECTION)
    new_store = MilvusStore(collection_name=REBUILD_COLLECTION)
    written = new_store.add_docs(units)
    check = compare_corpus_and_index(store=new_store)
    print(f"新集合写入 {written} 条，核对{'一致' if check['ok'] else '不一致'}")
    # 分支：新集合与磁盘对不上 → 中止，检索名字不动（线上仍是旧集合）
    if not check["ok"]:
        print("新集合核对不通过，已中止；检索名字未动，线上不受影响")
        return 1

    # 2) 改名 + 别名接管
    if client.has_collection(LEGACY_COLLECTION):
        print(f"{LEGACY_COLLECTION} 已存在，需人工确认后再重建（避免覆盖回滚点）")
        return 1
    client.rename_collection(alias, LEGACY_COLLECTION)
    client.create_alias(collection_name=REBUILD_COLLECTION, alias=alias)
    print(f"已切换：{alias} 现在指向 {REBUILD_COLLECTION}（旧集合为 {LEGACY_COLLECTION}）")

    # 3) 经别名再核对一次 + 冒烟检索（都走线上真实入口）
    live = MilvusStore(collection_name=alias)
    live_check = compare_corpus_and_index(store=live)
    hits = live.similarity_search("预付款上限是多少", k=1)
    print(f"经别名核对：{'一致' if live_check['ok'] else '不一致'}；冒烟检索命中 {[(h.policy_ref, h.score) for h in hits]}")
    return 0 if live_check["ok"] else 1


def _check(store, policy_dir, as_json: bool) -> int:
    """核对入口：输出结果，不一致时返回非 0 退出码（便于脚本拦截）。"""
    state = compare_corpus_and_index(store=store, policy_dir=policy_dir)
    if as_json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        _print_check(state, corpus_fingerprint(policy_dir=policy_dir)["documents"])
    return 0 if state["ok"] else 1


def _sync(store, policy_dir, confirmed: bool, as_json: bool) -> int:
    """同步入口：默认只打印计划（dry-run），加 --yes 才真正改库，改完自动再核对一次。

    动作按业务键精确到单元——只写新增/变化的那几条、只删消失的那几条；
    同文件里没变的条文不重算向量（旧口径是整份文件删掉重插）。
    """
    state = compare_corpus_and_index(store=store, policy_dir=policy_dir)
    units = corpus_units(policy_dir=policy_dir)
    plan = plan_unit_sync(units, store.iter_rows())
    write_sources = sorted({unit.source for unit in plan["write"]})
    delete_sources = sorted({plan["index_source"].get(key, "") for key in plan["delete"]})
    brief = {
        "write_units": len(plan["write"]),
        "delete_units": len(plan["delete"]),
        "write_sources": write_sources,
        "delete_sources": delete_sources,
    }
    # 分支：没有给 --yes 或本来就没有差异 → 只出计划，不动库
    if not plan["write"] and not plan["delete"]:
        if as_json:
            print(json.dumps({"version": state["version"], "applied": False, "plan": brief}, ensure_ascii=False, indent=2))
        else:
            print("无需同步：文件与索引逐单元一致")
        return 0
    if not confirmed:
        if as_json:
            print(json.dumps(
                {"version": state["version"], "applied": False, "plan": brief},
                ensure_ascii=False, indent=2,
            ))
        else:
            _print_unit_plan(plan)
        return 0
    written = store.add_docs(plan["write"])
    removed = store.delete_units(plan["delete"])
    after = compare_corpus_and_index(store=store, policy_dir=policy_dir)
    if as_json:
        print(json.dumps(
            {
                "version": state["version"],
                "applied": True,
                "plan": brief,
                "written": written,
                "removed": removed,
                "check": after["ok"],
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_unit_plan(plan)
        print(f"已写入 {written} 条 / 删除 {removed} 条")
        print(f"同步后核对：{'一致' if after['ok'] else '仍不一致'}")
    return 0 if after["ok"] else 1


def _print_unit_plan(plan: dict) -> None:
    """打印按单元的同步计划：新增/变化的文件与消失的文件各一行。"""
    print(f"同步计划（按单元）：写 {len(plan['write'])} 条 / 删 {len(plan['delete'])} 条")
    for source in sorted({unit.source for unit in plan["write"]}):
        count = sum(1 for unit in plan["write"] if unit.source == source)
        print(f"  写入 {source}：{count} 条单元")
    for source in sorted({plan["index_source"].get(key, "") for key in plan["delete"]}):
        count = sum(1 for key in plan["delete"] if plan["index_source"].get(key) == source)
        print(f"  清除 {source}：{count} 条单元（磁盘上已无对应正文）")
    print("（未执行：确认无误后加 --yes）")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：--check 核对 / --sync 按份同步 / --fingerprint 打印版本号。"""
    parser = argparse.ArgumentParser(description="政策库语料指纹、一致性核对与按份同步")
    parser.add_argument("--check", action="store_true", help="核对文件与索引是否一致（默认动作）")
    parser.add_argument("--sync", action="store_true", help="按份同步有变化的语料")
    parser.add_argument("--fingerprint", action="store_true", help="只打印政策库版本号")
    parser.add_argument("--rebuild", action="store_true", help="重建为业务键集合并用别名接管检索名字")
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
    if args.rebuild:
        return _rebuild(args.yes, args.json)
    if args.sync:
        return _sync(store, policy_dir, args.yes, args.json)
    return _check(store, policy_dir, args.json)


if __name__ == "__main__":
    sys.exit(main())
