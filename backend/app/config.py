"""
从 `.env` 读密钥和端点；`Settings` 类集中管理 chat 模型 (硅基流动 DeepSeek)、
embedding (DashScope Qwen3.7-text-embedding)、Milvus、检索后端 ;`env_ready` 供健康检查
"""

from pathlib import Path
from dotenv import load_dotenv

# 仓库根目录 = backend/app/config.py 向上 2 级（app -> backend -> 仓库根）
BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")

from pydantic_settings import BaseSettings, SettingsConfigDict  

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
    chat_enable_thinking: bool = False  # False=关闭 DeepSeek thinking，控制抽取时延与成本
    # 服务端审核并发度（同时送审的合同份数）：有界并发避免打爆 LLM 配额/限流。
    # 按实测限流调整：默认 2 起步，稳妥后可试 3~5（env: REVIEW_WORKERS）
    review_workers: int = 2

    # ---- 向量模型：阿里 DashScope（OpenAI 兼容 embedding 端点）----
    dashscope_api_key: str = ""
    qwen_embedding_api_key: str = ""
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_dim: int = 1024
    embedding_batch_size: int = 10  # DashScope 单次上限，分批绕开

    # ---- 向量库 / 数据库 ----
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_collection: str = "contract_policies"
    database_url: str = ""  # Postgres

    # ---- 检索后端：milvus 不可用时自动退回内存----
    retrieval_backend: str = "auto"  # auto | milvus | memory

    @property
    def embedding_api_key(self) -> str:
        """取 embedding 密钥：新键名 DASHSCOPE_API_KEY 优先，旧键名 QWEN_EMBEDDING 兜底。"""
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
