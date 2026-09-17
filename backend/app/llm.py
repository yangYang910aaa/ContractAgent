"""
模型工厂：统一对外暴露 get_chat_model / get_embedding_model。

另放一件跨模块共用的模型输出工具：recover_completion——从结构化输出的解析报错里
还原模型原始 JSON（抽取与盲审都要用，不各留一份副本）。
"""

from __future__ import annotations
import json
from typing import Any, List

import httpx
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from backend.app.config import settings


def get_chat_model(
    model: str | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    timeout: float | None = None,
) -> ChatOpenAI:
    """硅基流动 DeepSeek chat 模型。抽取消掉 thinking 以控制时延/成本。

    timeout=None 沿用 SDK 默认（不设上限）——长调用（如政策起草）要显式传超时，
    否则一次卡住的连接会把调用方一起拖住。
    """
    return ChatOpenAI(
        model=model or settings.chat_model,
        temperature=settings.chat_temperature if temperature is None else temperature,
        api_key=SecretStr(settings.siliconflow_api_key),
        base_url=settings.siliconflow_base_url,
        timeout=timeout,
        extra_body={
            "enable_thinking": (
                settings.chat_enable_thinking
                if enable_thinking is None
                else enable_thinking
            )
        },
    )


class DashScopeCompatEmbeddings(Embeddings):
    """DashScope OpenAI 兼容 embedding 端点封装。

    模型默认 qwen3.7-text-embedding（1024 维）。embed_documents 内部按
    batch_size 分批，规避单次请求条数上限。
    """

    def __init__(
        self,
        model: str = "qwen3.7-text-embedding",
        api_key: str = "",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        batch_size: int = 10,
    ) -> None:
        """初始化向量客户端参数；base_url 去尾斜杠，防止拼 URL 时出现双斜杠。"""
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.batch_size = batch_size

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """调一次 DashScope 兼容 /embeddings 接口（单批），按 index 排序保证与输入同序。"""
        resp = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # 返回顺序与输入一致（DashScope 兼容模式保证）
        ordered = sorted(data, key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：按 batch_size 分批调 _embed，规避 DashScope 单次条数上限。"""
        results: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            results.extend(self._embed(texts[i : i + self.batch_size]))
        return results

    def embed_query(self, text: str) -> list[float]:
        """单条文本向量化（检索 query / 维度探测用）。"""
        return self._embed([text])[0]


def get_embedding_model() -> Embeddings:
    """DashScope 向量模型(OpenAI 兼容端点)"""
    return DashScopeCompatEmbeddings(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        batch_size=settings.embedding_batch_size,
    )


def check_env_ready() -> dict[str, Any]:
    """供健康检查/冒烟脚本：返回关键配置就绪情况，不抛异常。"""
    return {
        "chat_model": settings.chat_model,
        "chat_api_key_set": bool(settings.siliconflow_api_key),
        "embedding_model": settings.embedding_model,
        "embedding_api_key_set": bool(settings.embedding_api_key),
    }


def recover_completion(exc: Exception) -> dict | None:
    """从 with_structured_output 的解析报错里还原模型原始 JSON。

    解析失败时报错文本通常带着原始 completion，能捞出 dict 就返回它，捞不到返回 None
    （接口、超时这类异常根本没有 completion）。抽取与盲审共用这一份。
    """
    text = str(exc)
    marker = text.find("completion ")
    # 这种情况是：报错里没有 completion 字样 → 不是解析问题，无法还原
    if marker < 0:
        return None
    start = text.find("{", marker)
    if start < 0:
        return None
    try:
        raw, _ = json.JSONDecoder().raw_decode(text, start)
    except Exception:  # noqa: BLE001 — 报错文本形态不可控，捞不出来就当没得还原
        return None
    return raw if isinstance(raw, dict) else None
