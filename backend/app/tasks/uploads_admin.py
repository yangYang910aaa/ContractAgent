"""上传原件目录的巡检与孤儿清理。

任务删除时会顺手删掉原件，但删除失败、登记簿里任务被清而文件留下、演练留下的文件
都会让 `data/uploads/` 越攒越多。这里只处理"登记簿里没有任何任务指向"的文件：
默认只列出（dry-run），`--clean --yes` 才真删；读不到登记簿时一律不判断、不删除。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from backend.app.api import routes_tasks
from backend.app.config import settings


def scan_orphans(known_sources: Iterable[str], upload_dir: Path | None = None) -> list[Path]:
    """没人认领的上传原件：在目录里、但登记簿的来源路径里没有它。"""
    directory = upload_dir or routes_tasks.UPLOAD_DIR
    if not directory.is_dir():
        return []
    known = {Path(item).resolve() for item in known_sources if item}
    return sorted(
        path for path in directory.iterdir() if path.is_file() and path.resolve() not in known
    )


def clean_orphans(paths: Iterable[Path]) -> int:
    """删掉给定的孤儿文件，返回实际删掉的个数（不存在就当已清理）。"""
    removed = 0
    for path in paths:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass  # 删不掉不中断：留到下次巡检还能看到
    return removed


def _records_store():
    """任务登记簿：配了 DATABASE_URL 就读 Postgres（与线上同源），否则返回 None。"""
    # 分支：没配数据库 → 读不到登记簿，调用方必须放弃判断（否则空登记簿会把在用的原件全当孤儿）
    if not settings.database_url:
        return None
    from backend.app.tasks.store_pg import PgPersistence

    return PgPersistence().store


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：默认只列孤儿；`--clean --yes` 才真删。"""
    parser = argparse.ArgumentParser(description="上传原件目录巡检与孤儿清理")
    parser.add_argument("--clean", action="store_true", help="删除孤儿文件（默认只列出）")
    parser.add_argument("--yes", action="store_true", help="与 --clean 同用：确认执行")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    store = _records_store()
    # 分支：读不到登记簿 → 只报告、绝不判断孤儿（宁可不清理，也不能误删在用的原件）
    if store is None:
        message = "未配置 DATABASE_URL：读不到任务登记簿，无法判断谁是孤儿，本次什么也没做"
        print(json.dumps({"error": message}, ensure_ascii=False) if args.json else message)
        return 1

    records = store.list_records()
    orphans = scan_orphans(record.source for record in records)
    payload = {
        "records": len(records),
        "orphans": [path.name for path in orphans],
        "bytes": sum(path.stat().st_size for path in orphans),
        "deleted": 0,
    }
    # 分支：没加 --clean → 只列出，不动文件
    if not args.clean:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"任务 {payload['records']} 条 · 孤儿 {len(orphans)} 个（{payload['bytes']} 字节）")
            for name in payload["orphans"]:
                print(f"  {name}")
            print("（未执行：确认无误后加 --clean --yes）")
        return 0
    # 分支：加了 --clean 但没加 --yes → 仍然只列
    if not args.yes:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"将删除 {len(orphans)} 个孤儿文件（{payload['bytes']} 字节）：")
            for name in payload["orphans"]:
                print(f"  {name}")
            print("（未执行：确认无误后加 --yes）")
        return 0

    payload["deleted"] = clean_orphans(orphans)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"已删除 {payload['deleted']} 个孤儿文件（{payload['bytes']} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
