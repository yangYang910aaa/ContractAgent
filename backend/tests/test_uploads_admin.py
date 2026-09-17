"""上传原件目录的巡检与孤儿清理测试：临时目录 + 假登记簿，全部离线。"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.api import routes_tasks
from backend.app.tasks import uploads_admin
from backend.app.config import settings


class _FakeStore:
    """只提供 list_records 的假登记簿：巡检只用到记录里的 source。"""

    def __init__(self, sources: list[str]) -> None:
        self._records = [type("Record", (), {"source": source})() for source in sources]

    def list_records(self):
        """返回全部任务记录（与真实登记簿同名接口）。"""
        return self._records


@pytest.fixture()
def upload_dir(tmp_path: Path, monkeypatch) -> Path:
    """临时上传目录：两份「在用」+ 一份没人认领。"""
    monkeypatch.setattr(routes_tasks, "UPLOAD_DIR", tmp_path)
    (tmp_path / "t-1_合同甲.md").write_text("甲", encoding="utf-8")
    (tmp_path / "t-2_合同乙.md").write_text("乙", encoding="utf-8")
    (tmp_path / "t-9_没人认领.md").write_text("孤儿", encoding="utf-8")
    return tmp_path


def test_scan_finds_only_unclaimed_files(upload_dir: Path) -> None:
    """只把登记簿里没人指向的文件当孤儿。"""
    orphans = uploads_admin.scan_orphans(
        [str(upload_dir / "t-1_合同甲.md"), str(upload_dir / "t-2_合同乙.md")]
    )
    assert [path.name for path in orphans] == ["t-9_没人认领.md"]


def test_clean_removes_orphans(upload_dir: Path) -> None:
    """清理只删孤儿，在用的原件一个不动。"""
    orphans = uploads_admin.scan_orphans([str(upload_dir / "t-1_合同甲.md")])
    assert uploads_admin.clean_orphans(orphans) == 2
    assert sorted(path.name for path in upload_dir.iterdir()) == ["t-1_合同甲.md"]


def test_cli_refuses_without_records_source(upload_dir: Path, monkeypatch, capsys) -> None:
    """读不到登记簿时只报告、绝不删文件（宁可不清理，也不能误删在用的原件）。"""
    monkeypatch.setattr(settings, "database_url", "")
    assert uploads_admin.main([]) == 1
    assert "未配置" in capsys.readouterr().out
    assert len(list(upload_dir.iterdir())) == 3


def test_cli_clean_needs_yes(upload_dir: Path, monkeypatch, capsys) -> None:
    """默认与只加 --clean 都只列；--clean --yes 才真删，且只删孤儿。"""
    monkeypatch.setattr(
        uploads_admin,
        "_records_store",
        lambda: _FakeStore(
            [str(upload_dir / "t-1_合同甲.md"), str(upload_dir / "t-2_合同乙.md")]
        ),
    )
    assert uploads_admin.main([]) == 0
    assert "未执行" in capsys.readouterr().out
    assert len(list(upload_dir.iterdir())) == 3

    assert uploads_admin.main(["--clean"]) == 0
    assert len(list(upload_dir.iterdir())) == 3

    assert uploads_admin.main(["--clean", "--yes"]) == 0
    assert sorted(path.name for path in upload_dir.iterdir()) == ["t-1_合同甲.md", "t-2_合同乙.md"]
