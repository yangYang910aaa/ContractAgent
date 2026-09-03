"""
模型工厂：统一对外暴露 get_chat_model / get_embedding_model。
"""

from __future__ import annotations
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
) -> ChatOpenAI:
    """硅基流动 DeepSeek chat 模型。抽取消掉 thinking 以控制时延/成本。"""
    return ChatOpenAI(
        model=model or settings.chat_model,
        temperature=settings.chat_temperature if temperature is None else temperature,
        api_key=SecretStr(settings.siliconflow_api_key),
        base_url=settings.siliconflow_base_url,
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
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.batch_size = batch_size

    def _embed(self, texts: list[str]) -> list[list[float]]:
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
        results: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            results.extend(self._embed(texts[i : i + self.batch_size]))
        return results

    def embed_query(self, text: str) -> list[float]:
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
