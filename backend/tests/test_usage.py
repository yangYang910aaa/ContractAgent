"""LLM 调用计数底座单测（离线，无网络/无 API）。

只测 usage.py 的计数语义：次数/分阶段/失败也计/并发上下文隔离。
"""

from __future__ import annotations

import threading

from backend.app.usage import llm_call, track_usage


def test_counts_calls_and_stages() -> None:
    """正常调用：总次数累加、分阶段分开记、报告 dict 三个键都在。"""
    with track_usage() as usage:
        with llm_call("extract"):
            pass
        with llm_call("extract"):
            pass
        with llm_call("review"):
            pass
    assert usage.calls == 3
    assert usage.stages == {"extract": 2, "review": 1}
    assert usage.seconds >= 0
    assert usage.to_dict()["calls"] == 3


def test_no_tracker_is_noop() -> None:
    """没有追踪上下文（服务端直调/旧代码）时埋点是 no-op，不能抛异常。"""
    with llm_call("extract"):
        pass


def test_failed_call_still_counted() -> None:
    """调用抛异常也要计数：请求已发出、服务端可能已计费（成本口径按发起次数）。"""
    with track_usage() as usage:
        try:
            with llm_call("extract"):
                raise RuntimeError("限流")
        except RuntimeError:
            pass
    assert usage.calls == 1
    assert usage.stages == {"extract": 1}


def test_contexts_are_isolated_across_threads() -> None:
    """并发 worker 各持一份上下文：两个线程的计数不能互相串（全局计数器会串号）。"""
    results: dict[str, int] = {}

    def worker(name: str, times: int) -> None:
        with track_usage() as usage:
            for _ in range(times):
                with llm_call("extract"):
                    pass
            results[name] = usage.calls

    threads = [
        threading.Thread(target=worker, args=("a", 2)),
        threading.Thread(target=worker, args=("b", 5)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == {"a": 2, "b": 5}
