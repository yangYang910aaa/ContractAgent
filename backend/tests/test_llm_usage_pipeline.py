"""run_review 调用计数单测（假抽取器/假模型，离线 0 API）。

验证评测二期的成本口径：single+双读=2 次、关双读=1 次、双审=3 次；
第二读失败仍计 2 次（请求已发出）；抽取整体失败时报告带 error 且计数照实带出。
"""

from __future__ import annotations

from pathlib import Path

from backend.app.pipeline import build_report, run_review
from backend.app.schemas import ContractModel

# 迷你合同文本：只为走通 parser → 抽取 → 规则 → 报告链路，内容不参与断言
_MINI_CONTRACT = (
    "采购合同\n"
    "甲方：某采购方　乙方：某供应商\n"
    "第一条 合同总价 100000 元。\n"
    "第二条 乙方应于签订后交付。\n"
)


class _FakeStructured:
    """假结构化输出：记录调用次数与阶段，按需在第 N 次调用抛异常。"""

    def __init__(self, model: "_FakeModel", stage: str, payload, fail_at: int | None) -> None:
        self._model = model
        self._stage = stage
        self._payload = payload
        self._fail_at = fail_at

    def invoke(self, messages):
        """模拟一次 LLM 往返：先计数（真实成本发生在请求发出时），再返回假 payload。"""
        self._model.calls += 1
        self._model.seen.append(self._stage)
        if self._fail_at == self._model.calls:
            raise RuntimeError("stub invoke failure")
        return self._payload


class _FakeModel:
    """假 chat 模型：按 with_structured_output 的 schema 分派抽取/复核两种假输出。"""

    def __init__(
        self,
        extract_payload: dict | None = None,
        review_payload: dict | None = None,
        fail_at: int | None = None,
    ) -> None:
        self.extract_payload = extract_payload if extract_payload is not None else {}
        self.review_payload = review_payload if review_payload is not None else {"findings": []}
        self.fail_at = fail_at  # 第 N 次调用（1-based）抛异常；None=不失败
        self.calls = 0  # 模型收到的调用总次数（抽取与复核共用一个计数）
        self.seen: list[str] = []  # 调用顺序的阶段名，便于断言"第几次是什么"

    def with_structured_output(self, schema, method=None):
        """抽取 schema / 复核 schema → 各自的假 structured（共享模型的计数）。"""
        stage = "review" if schema.__name__ == "BlindReviewSchema" else "extract"
        payload = self.review_payload if stage == "review" else self.extract_payload
        return _FakeStructured(self, stage, payload, self.fail_at)


def _write(tmp_path: Path) -> Path:
    """写一份迷你合同 md，返回路径（run_review 需要真实文件走 parser）。"""
    path = tmp_path / "mini.md"
    path.write_text(_MINI_CONTRACT, encoding="utf-8")
    return path


def _run(tmp_path: Path, model: _FakeModel, **kwargs) -> dict:
    """跑一份迷你合同；检索器注入空实现，避免离线测试去连 Milvus/embedding。"""
    return run_review(_write(tmp_path), llm=model, retriever=lambda query: [], **kwargs)


def test_single_with_double_read_counts_two(tmp_path: Path) -> None:
    """默认 single + 付款期次双读 → 抽取两次，报告 llm 段如实带出。"""
    model = _FakeModel()
    report = _run(tmp_path, model)
    assert model.calls == 2
    assert report["llm"]["calls"] == 2
    assert report["llm"]["stages"] == {"extract": 2}
    assert report["llm"]["seconds"] >= 0


def test_double_read_off_counts_one(tmp_path: Path) -> None:
    """关掉双读 → 只抽取一次（省钱跑批口径）。"""
    report = _run(tmp_path, _FakeModel(), double_read=False)
    assert report["llm"]["calls"] == 1
    assert report["llm"]["stages"] == {"extract": 1}


def test_double_mode_adds_review_call(tmp_path: Path) -> None:
    """双审 → 抽取两读 + 盲审一次 = 3 次，且阶段顺序是 extract、extract、review。"""
    model = _FakeModel()
    report = _run(tmp_path, model, review_mode="double")
    assert model.seen == ["extract", "extract", "review"]
    assert report["llm"]["calls"] == 3
    assert report["llm"]["stages"] == {"extract": 2, "review": 1}


def test_second_read_failure_still_counts_two(tmp_path: Path) -> None:
    """第二读失败：请求已发出 → 仍计 2 次；首读结果照常出报告，不中断审查。"""
    model = _FakeModel(fail_at=2)
    report = _run(tmp_path, model)
    assert report.get("error") in (None, "")
    assert report["llm"]["calls"] == 2


def test_extract_failure_reports_error_with_count(tmp_path: Path) -> None:
    """首读就失败 → 报告带 error、评级 None，但调用次数照实带出（成本可见）。"""
    model = _FakeModel(fail_at=1)
    report = _run(tmp_path, model)
    assert "抽取失败" in report["error"]
    assert report["grade"] is None
    assert report["llm"]["calls"] == 1
    assert report["llm"]["stages"] == {"extract": 1}


def test_build_report_llm_key_defaults_none() -> None:
    """旧调用方（如 graph 服务端链路）不传 llm 时键仍存在且为 None，前端不必判空。"""
    report = build_report("demo.md", ContractModel(), [], [])
    assert report["llm"] is None
