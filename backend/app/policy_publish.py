"""政策入库：把一份政策正文落成语料文件并同步进检索库（写操作，幂等、可回滚）。

和命令行 `policy_admin --sync` 是同一套按单元增量（只重算变化的条文），区别只在入口面向
"刚起稿的一份政策"：先校验（文件名、编号撞号、元信息缺项）→ 落盘 → 同步 → 核对，
核对不过就把文件与库一起退回去。核对与预览都只读，不花模型调用。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from backend.app import policy_assistant
from backend.app.policy_admin import plan_unit_sync
from backend.app.policy_corpus import (
    compare_corpus_and_index,
    corpus_fingerprint,
    corpus_units,
    policy_documents,
)
from backend.app.policy_rag import POLICY_DIR, _split_doc_articles, get_store

# 语料文件名形态：入库靠文件名前缀认编号，不符合的文件会被 load_policies 直接跳过
_FILE_RE = re.compile(r"^P-\d+_[^\\/:*?\"<>|]+\.md$")


class PublishBlocked(Exception):
    """入库被拦下：文件名不合法、编号撞号、元信息缺项且没有显式放行。"""

    def __init__(self, reasons: list[dict]) -> None:
        self.reasons = reasons
        # 带上类型：接口把它当 400 的 detail 直接回给页面，只有句子看不出是哪类问题
        super().__init__("；".join(f"{item['kind']}：{item['detail']}" for item in reasons))


def _ref_of(file_name: str) -> str:
    """文件名前缀里的政策编号（入库口径：认不出编号的文件不入库）。"""
    matched = re.match(r"(P-\d+)", file_name)
    return matched.group(1) if matched else ""


def _file_reasons(file_name: str) -> list[dict]:
    """文件名的硬性校验：形态、路径穿越。"""
    reasons: list[dict] = []
    # 这种情况是：带了目录或多级路径 → 不许（落盘位置固定是语料目录）
    if not file_name or file_name != Path(file_name).name:
        reasons.append({"kind": "文件名不合法", "detail": "文件名不能带目录，只写 `P-XX_短名.md` 这一层"})
    # 这种情况是：不符合语料命名形态 → 入库会被跳过，等于白入
    elif not _FILE_RE.match(file_name):
        reasons.append({
            "kind": "文件名不合法",
            "detail": "文件名要写成 `P-XX_短名.md`（编号 + 下划线 + 短名），否则入库时认不出编号",
        })
    return reasons


def _collision_reasons(ref: str, file_name: str, policy_dir: Path) -> list[dict]:
    """编号撞号：同名文件是"更新这份政策"，不同文件用同一个编号才是硬冲突。"""
    others = [
        doc["source"]
        for doc in policy_documents(policy_dir=policy_dir)
        if doc["ref"] == ref and doc["source"] != file_name
    ]
    # 这种情况是：库里已有另一份文件用着这个编号 → 挡住（否则检索引用分不清是哪份）
    if others:
        return [{
            "kind": "编号重复",
            "detail": f"{ref} 已被 {'、'.join(others)} 占用；要么换号，要么直接更新那份政策",
        }]
    return []


def _units_after(content: str, file_name: str, policy_dir: Path) -> list:
    """落盘后磁盘上的语料单元：这份文件的旧单元换成按新内容分条的结果。"""
    fresh = _split_doc_articles(
        full_text=content, source=file_name, policy_ref=_ref_of(file_name)
    )
    return [unit for unit in corpus_units(policy_dir=policy_dir) if unit.source != file_name] + fresh


def _next_version(content: str, file_name: str, policy_dir: Path) -> str:
    """落盘后的政策库版本（纯计算）：按指纹同口径，把这份文件换掉再算一遍。"""
    digests = {doc["source"]: doc["sha256"] for doc in policy_documents(policy_dir=policy_dir)}
    digests[file_name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    joined = "\n".join(f"{name}:{digest}" for name, digest in sorted(digests.items()))
    return "PL-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def plan_publish(
    content: str, file_name: str, policy_dir: Path | None = None, store=None
) -> dict:
    """算一份政策的入库计划（只读：不落盘、不改库）。

    给出要写/要删的单元数、是否覆盖已有文件、版本号前后对照，以及拦下入库的硬冲突。
    """
    directory = policy_dir or POLICY_DIR
    store = store if store is not None else get_store()
    file_name = (file_name or "").strip()
    blockers = _file_reasons(file_name)
    # 分支：文件名就不合法 → 后面的比对都不成立，直接给拦截项
    if blockers:
        return {
            "file_name": file_name,
            "ref": "",
            "exists": False,
            "write_units": 0,
            "delete_units": 0,
            "delete_sources": [],
            "missing": [],
            "version": corpus_fingerprint(policy_dir=directory)["version"],
            "next_version": "",
            "blockers": blockers,
        }

    ref = _ref_of(file_name)
    plan = plan_unit_sync(_units_after(content, file_name, directory), store.iter_rows())
    return {
        "file_name": file_name,
        "ref": ref,
        "exists": (directory / file_name).is_file(),
        "write_units": len(plan["write"]),
        "delete_units": len(plan["delete"]),
        "delete_sources": sorted({plan["index_source"].get(key, "") for key in plan["delete"]}),
        "missing": policy_assistant.parse_policy(content, source=file_name)["missing"],
        "version": corpus_fingerprint(policy_dir=directory)["version"],
        "next_version": _next_version(content, file_name, directory),
        "blockers": _collision_reasons(ref, file_name, directory),
    }


def publish(
    content: str,
    file_name: str,
    policy_dir: Path | None = None,
    store=None,
    allow_missing_meta: bool = False,
) -> dict:
    """落盘 + 按单元增量同步 + 执行后核对；核对不过就把文件和库一起退回。

    allow_missing_meta=False（默认）时缺元信息会被拦下——缺项的政策入库后，报告里"依据哪一版政策"
    就无从标注。撞号无论如何都拦：那是检索引用分不清是哪份的问题，不是体例问题。
    """
    directory = policy_dir or POLICY_DIR
    store = store if store is not None else get_store()
    plan = plan_publish(content, file_name, policy_dir=directory, store=store)
    reasons = list(plan["blockers"])
    # 分支：缺元信息且没有显式放行 → 与硬冲突一并拦下
    if plan["missing"] and not allow_missing_meta:
        reasons.append({
            "kind": "元信息缺失",
            "detail": "缺：" + "、".join(plan["missing"]) + "（补齐后再入库，或显式选择按现状入库）",
        })
    if reasons:
        raise PublishBlocked(reasons)

    target = directory / plan["file_name"]
    previous = target.read_bytes() if target.is_file() else None
    target.write_bytes(content.encode("utf-8"))
    try:
        # 落盘后按真实磁盘状态再算一次：计划与实际之间的差异（磁盘上别的变化）也一并同步，
        # 口径与命令行 --sync 相同
        unit_plan = plan_unit_sync(corpus_units(policy_dir=directory), store.iter_rows())
        written = store.add_docs(unit_plan["write"])
        removed = store.delete_units(unit_plan["delete"])
        after = compare_corpus_and_index(store=store, policy_dir=directory)
    except Exception as exc:  # noqa: BLE001
        # 这种情况是：同步中途报错 → 文件与库都退回原状，再把原因抛出去
        _restore(target, previous, policy_dir=directory, store=store)
        raise RuntimeError(f"同步失败，已退回改动：{exc}") from exc
    # 分支：写进去了但核对不上（库与磁盘对不齐）→ 同样退回
    if not after["ok"]:
        _restore(target, previous, policy_dir=directory, store=store)
        raise RuntimeError("同步后核对不一致，已退回改动；政策库仍是原来的样子")

    return {
        "applied": True,
        "file_name": plan["file_name"],
        "ref": plan["ref"],
        "updated": plan["exists"],
        "written": written,
        "removed": removed,
        "check_ok": after["ok"],
        "previous_version": plan["version"],
        "version": after["version"],
        "units": after["disk_units"],
    }


def _restore(target: Path, previous: bytes | None, policy_dir: Path, store) -> None:
    """退回改动：文件恢复成原内容（本来没有就删掉），再把库按退回后的磁盘同步一次。"""
    # 分支：这份文件是本次新建的 → 直接删掉
    if previous is None:
        target.unlink(missing_ok=True)
    else:
        target.write_bytes(previous)
    unit_plan = plan_unit_sync(corpus_units(policy_dir=policy_dir), store.iter_rows())
    store.add_docs(unit_plan["write"])
    store.delete_units(unit_plan["delete"])
