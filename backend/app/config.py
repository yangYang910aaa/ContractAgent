"""
应用配置。
"""

from pathlib import Path
from dotenv import load_dotenv

# 仓库根目录 = backend/app/config.py 向上 2 级（app -> backend -> 仓库根）
BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")

from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402


class Settings(BaseSettings):
    """集中管理 .env 配置。未设置的键留空，由 /api/health 报告，不在启动时崩溃。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 对话模型：硅基流动（OpenAI 兼容）----
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    chat_model: str = "deepseek-ai/DeepSeek-V4-flash"
    chat_temperature: float = 0.1
    chat_enable_thinking: bool = False  # DeepSeek-V4 系列默认 thinking，抽取消掉

    # ---- 向量模型：阿里 DashScope（OpenAI 兼容 embedding 端点）----
    # 键名兼容：新项目用 DASHSCOPE_API_KEY，旧笔记里叫 QWEN_EMBEDDING_API_KEY
    dashscope_api_key: str = ""
    qwen_embedding_api_key: str = ""
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_dim: int = 1024
    embedding_batch_size: int = 10  # DashScope 单次上限，分批绕开

    # ---- 向量库 / 数据库 ----
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_collection: str = "contract_policies"
    database_url: str = ""  # 可选 Postgres（checkpointer）

    # ---- 检索后端：milvus 不可用时自动退回内存，别卡死（计划风险预案）----
    retrieval_backend: str = "auto"  # auto | milvus | memory

    @property
    def embedding_api_key(self) -> str:
        return self.dashscope_api_key or self.qwen_embedding_api_key

    @property
    def env_ready(self) -> dict:
        """供健康检查报告：哪些关键配置缺失。"""
        return {
            "chat_api_key_set": bool(self.siliconflow_api_key),
            "embedding_api_key_set": bool(self.embedding_api_key),
            "milvus_uri": self.milvus_uri,
            "retrieval_backend": self.retrieval_backend,
        }


settings = Settings()
