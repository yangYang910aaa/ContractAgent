"""大模型调用计数与耗时统计。

用途：回答"审一份合同到底问了几次大模型"——审查报告带 `llm` 段
（calls / stages / seconds），run_eval 再按"每次全语料跑一遍"汇总成本，
供 PRD/README 的成本口径使用。这也是"钱花在哪"的可核对凭证。

实现：用 contextvar 存当前追踪器，`llm_call(stage)` 上下文管理器在 LLM 调用点
包住 invoke——计数 + 计时；没有追踪上下文时是 no-op（服务端链路与旧调用方零影响）。

易错点：不能用模块级全局计数器——服务端有多个 worker 线程并发审合同，
全局计数会把不同合同的调用混在一起；contextvar 按"每个 run_review 一份上下文"
隔离（含线程池里的各 worker 各自持有自己的上下文）。
口径：只计 chat 调用（抽取首读/付款期次二读/双审盲审），不含本地规则与
embedding 检索调用（检索是另一条成本线，量级小且不在本指标口径内）。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

# 当前追踪器：None = 未开启追踪（此时计数什么都不做，不产生任何副作用）
_ACTIVE: ContextVar["Usage | None"] = ContextVar("llm_usage", default=None)

# 阶段名常量：与计数处一致，报告与输出文件里按这两个键分组
STAGE_EXTRACT = "extract"  # 结构化抽取（双读时同一阶段计两次）
STAGE_REVIEW = "review"  # 双审盲审复核


@dataclass
class Usage:
    """一次审查的 LLM 用量：总次数 + 分阶段次数 + 调用累计耗时。"""

    calls: int = 0  # LLM 调用总次数（含双读第二读、双审盲审）
    stages: dict[str, int] = field(default_factory=dict)  # 阶段名 → 次数，如 {"extract": 2}
    seconds: float = 0.0  # 调用累计耗时（秒，只算 invoke 往返，不含本地规则/检索）

    def to_dict(self) -> dict:
        """报告用 dict：三个键固定存在（前端与评测都不必判空/兜底）。"""
        return {
            "calls": self.calls,
            "stages": dict(self.stages),
            "seconds": round(self.seconds, 2),
        }


@contextmanager
def track_usage():
    """开启一段用量追踪：`with track_usage() as usage:`，usage 实时累计到退出为止。

    由 run_review 在抽取/复核整段外面套一层；嵌套使用时内层会覆盖外层，
    本调用方无嵌套需求（如需嵌套应先取回外层 usage 再合并）。
    """
    usage = Usage()
    token = _ACTIVE.set(usage)
    try:
        yield usage
    finally:
        _ACTIVE.reset(token)


@contextmanager
def llm_call(stage: str):
    """包裹一次 LLM 调用：计数 + 计时（stage 取 STAGE_EXTRACT / STAGE_REVIEW）。

    没有追踪上下文（服务端直调、旧单测）时不计数，也不抛异常。
    易错点：放在 finally 里累加——请求抛异常也要计，因为请求已经发出、
    服务端可能已计费；按"实际发起次数"记比按"成功次数"记更贴近账单。
    """
    usage = _ACTIVE.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if usage is not None:
            usage.seconds += time.perf_counter() - started
            usage.calls += 1
            usage.stages[stage] = usage.stages.get(stage, 0) + 1


def current_usage() -> dict | None:
    """当前追踪上下文里的用量快照（未开启追踪返回 None）。

    图节点用它把"本任务到目前发生的调用"写进 state：报告在最后一个节点才拼出来，
    而审批恢复会重新跑一遍 rules/policy 节点——用量要按"任务累计"而不是
    "本次调用"来报，所以存在 state 里并逐次合并。
    """
    usage = _ACTIVE.get()
    return usage.to_dict() if usage is not None else None


def merge_usage(prev: dict | None, cur: dict | None) -> dict:
    """合并两段用量（各字段相加，阶段计数按 key 相加）。"""
    if not prev:
        return cur or {"calls": 0, "stages": {}, "seconds": 0.0}
    if not cur:
        return prev
    stages = dict(prev.get("stages") or {})
    for name, count in (cur.get("stages") or {}).items():
        stages[name] = stages.get(name, 0) + count
    return {
        "calls": (prev.get("calls") or 0) + (cur.get("calls") or 0),
        "stages": stages,
        "seconds": round((prev.get("seconds") or 0.0) + (cur.get("seconds") or 0.0), 2),
    }
