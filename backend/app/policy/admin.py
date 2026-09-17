"""政策库命令行：政策库版本、文件 ↔ 索引一致性核对、按份同步。

用法：
    python -m backend.app.policy.admin --check        # 核对一致性（只读，0 次模型调用）
    python -m backend.app.policy.admin --sync         # 只打印同步计划，不改库
    python -m backend.app.policy.admin --sync --yes   # 执行同步（只重写有变化的文件）
    python -m backend.app.policy.admin --fingerprint  # 只打印政策库版本号
    python -m backend.app.policy.admin --drop-legacy --yes      # 删除回滚点集合（默认只报现状）
    python -m backend.app.policy.admin --prune-drafts 10 --yes  # 起稿产物只留最近 10 份

同步是"按份替换"：变化的文件先按文件名删掉旧单元再重插，磁盘上已删的文件清掉库内行，
没变化的文件一个单元都不动——不做清库重建，分条与检索仍走原实现，不动检索口径。
后两条是收尾清理：删集合与删目录都不可恢复，所以一律先列出、加 --yes 才动手。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from backend.app.policy.corpus import (
    compare_corpus_and_index,
    corpus_fingerprint,
    corpus_units,
)
from backend.app.policy.drafts import DRAFTS_DIR, DRAFTS_KEEP
from backend.app.config import settings
from backend.app.policy.rag import MilvusStore, get_store, unit_id

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


def _drop_legacy(store, policy_dir, confirmed: bool, as_json: bool) -> int:
    """删除回滚点集合：默认只报现状，加 --yes 才真删（先摘别名再删集合）。

    回滚点是"改主键时退回去的唯一一条路"，所以删之前要三件事同时成立：检索用的名字
    已经是别名、它指向业务键集合（说明迁移完成而不是回滚状态）、文件与索引核对一致。
    别名还挂在回滚点上时不能直接删集合——Milvus 会拒绝（错误码 1100），必须先摘别名。
    """
    client = store.client
    alias = settings.milvus_collection
    aliases = client.list_aliases().get("aliases", [])
    # 情况：检索名字还不是别名 → 说明没迁移过或已回滚，指向的就是它自己
    is_alias = alias in aliases
    live_target = client.describe_alias(alias)["collection_name"] if is_alias else alias
    has_legacy = client.has_collection(LEGACY_COLLECTION)
    legacy_rows = (
        client.get_collection_stats(LEGACY_COLLECTION).get("row_count", 0) if has_legacy else 0
    )
    # 情况：还有别名挂在回滚点上 → 记下来待摘（删除带别名的集合会被 Milvus 拒绝）
    attached = [
        name
        for name in aliases
        if client.describe_alias(name)["collection_name"] == LEGACY_COLLECTION
    ]
    state = {
        "alias": alias,
        "is_alias": is_alias,
        "live_target": live_target,
        "legacy_collection": LEGACY_COLLECTION,
        "legacy_exists": has_legacy,
        "legacy_rows": legacy_rows,
        "aliases_on_legacy": attached,
    }
    # 情况：回滚点本来就不在 → 没什么可删，按幂等处理
    if not has_legacy:
        if as_json:
            print(json.dumps(state | {"dropped": False, "reason": "回滚点不存在"}, ensure_ascii=False, indent=2))
        else:
            print(f"{LEGACY_COLLECTION} 不存在，无需删除")
        return 0

    check = compare_corpus_and_index(store=store, policy_dir=policy_dir)
    blocked = []
    # 情况：检索名字不是指向业务键集合的别名 → 删了就没有退路，先中止
    if not is_alias or live_target != REBUILD_COLLECTION:
        blocked.append(f"检索名字 {alias} 不指向 {REBUILD_COLLECTION}（当前指向 {live_target}）")
    # 情况：文件与索引对不上 → 先同步，别在库不健康的时候拆掉退路
    if not check["ok"]:
        blocked.append("文件与索引不一致（先跑 --sync）")

    if as_json:
        print(json.dumps(state | {"dropped": False, "blocked": blocked}, ensure_ascii=False, indent=2))
    else:
        print(f"检索名字 {alias} → {live_target}（别名：{'是' if is_alias else '否'}）")
        print(f"回滚点 {LEGACY_COLLECTION}：存在，{legacy_rows} 行")
        if attached:
            print("挂在回滚点上的别名（待摘）：" + "、".join(attached))
        print(f"文件与索引核对：{'一致' if check['ok'] else '不一致'}")
        for problem in blocked:
            print(f"  中止理由：{problem}")
    if blocked:
        return 1
    # 情况：没给 --yes → 只报现状与将要执行的动作
    if not confirmed:
        if not as_json:
            print("（未执行：确认无误后加 --yes；删除不可恢复，政策正文在 data/policies/ 可重建）")
        return 0

    for name in attached:
        client.drop_alias(name)
        print(f"已摘别名 {name}")
    client.drop_collection(LEGACY_COLLECTION)
    gone = not client.has_collection(LEGACY_COLLECTION)
    if as_json:
        print(json.dumps(state | {"dropped": gone}, ensure_ascii=False, indent=2))
    else:
        print(f"已删除 {LEGACY_COLLECTION}（回收 {legacy_rows} 行）：{'确认不存在' if gone else '仍能查到，需人工确认'}")
    return 0 if gone else 1


def plan_draft_prune(drafts_dir: Path, keep: int = DRAFTS_KEEP) -> dict:
    """起稿产物保留计划：按最后修改时间倒序，返回 (保留, 待删)。

    只认目录下的直接子目录（一次起稿一个目录）；目录不存在时返回空计划。
    """
    if not drafts_dir.is_dir():
        return {"keep": [], "remove": []}
    drafts = [path for path in drafts_dir.iterdir() if path.is_dir()]
    drafts.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return {"keep": drafts[:keep], "remove": drafts[keep:]}


def _prune_drafts(drafts_dir: Path, keep: int, confirmed: bool, as_json: bool) -> int:
    """清理起稿产物：默认只列要删的目录，加 --yes 才真删。

    产物是本地历史记录，删了不影响政策库（正文在 data/policies/ 的 md 里）；
    所以这里不做自动清理——列出来给人看一眼再删，避免把还在用的草稿扫掉。
    """
    plan = plan_draft_prune(drafts_dir, keep)
    remove = plan["remove"]
    if as_json:
        print(json.dumps(
            {
                "drafts_dir": str(drafts_dir),
                "keep": keep,
                "kept": [path.name for path in plan["keep"]],
                "removed": [] if not confirmed else [path.name for path in remove],
                "remove_planned": [path.name for path in remove],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(f"起稿产物目录 {drafts_dir}：保留最近 {keep} 份，现有 {len(plan['keep']) + len(remove)} 份")
        for path in remove:
            print(f"  待删 {path.name}")
        if not remove:
            print("没有需要清理的产物")
    if not confirmed or not remove:
        if not as_json and remove:
            print("（未执行：确认无误后加 --yes）")
        return 0
    removed = 0
    for path in remove:
        # 情况：目录里的符号链接 → 跳过（rmtree 不该顺着链接删到别处去）
        if path.is_symlink():
            print(f"跳过符号链接 {path.name}")
            continue
        shutil.rmtree(path)
        removed += 1
    if not as_json:
        print(f"已删除 {removed} 份历史产物")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：--check 核对 / --sync 按份同步 / --fingerprint 打印版本号 /
    --drop-legacy 删回滚点 / --prune-drafts 清理起稿产物。"""
    parser = argparse.ArgumentParser(description="政策库语料指纹、一致性核对与按份同步")
    parser.add_argument("--check", action="store_true", help="核对文件与索引是否一致（默认动作）")
    parser.add_argument("--sync", action="store_true", help="按份同步有变化的语料")
    parser.add_argument("--fingerprint", action="store_true", help="只打印政策库版本号")
    parser.add_argument("--rebuild", action="store_true", help="重建为业务键集合并用别名接管检索名字")
    parser.add_argument("--drop-legacy", action="store_true", help="删除回滚点集合（默认只报现状）")
    parser.add_argument(
        "--prune-drafts",
        type=int,
        nargs="?",
        const=DRAFTS_KEEP,
        default=None,
        metavar="N",
        help=f"清理起稿产物，保留最近 N 份（默认 {DRAFTS_KEEP}；默认只列不删）",
    )
    parser.add_argument("--yes", action="store_true", help="与 --sync / --drop-legacy 同用：确认执行，否则只报计划")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（脚本/评测引用）")
    parser.add_argument("--backend", default=None, choices=["auto", "memory", "milvus"], help="检索后端")
    parser.add_argument("--policy-dir", default=None, help="语料目录（默认 data/policies；演练可指到副本）")
    parser.add_argument("--drafts-dir", default=None, help="起稿产物目录（默认 data/policies/_drafts）")
    args = parser.parse_args(argv)
    policy_dir = Path(args.policy_dir) if args.policy_dir else None

    # 分支：清理起稿产物 → 纯读盘/删目录，不连向量库
    if args.prune_drafts is not None:
        drafts_dir = Path(args.drafts_dir) if args.drafts_dir else DRAFTS_DIR
        return _prune_drafts(drafts_dir, args.prune_drafts, args.yes, args.json)

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
    if args.drop_legacy:
        # 情况：删集合只对 Milvus 有意义（内存后端没有集合可言）
        if not hasattr(store, "client"):
            print("当前检索后端没有物理集合，无需删除回滚点")
            return 0
        return _drop_legacy(store, policy_dir, args.yes, args.json)
    if args.rebuild:
        return _rebuild(args.yes, args.json)
    if args.sync:
        return _sync(store, policy_dir, args.yes, args.json)
    return _check(store, policy_dir, args.json)


if __name__ == "__main__":
    sys.exit(main())
