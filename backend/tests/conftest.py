"""pytest 会话级配置。

作用：离线测试环境禁止 LangSmith tracing——.env 里 LANGCHAIN_TRACING_V2 有意开启
（联网验收/服务运行时用），但沙箱内无外网，追踪上传会重试报错刷屏并拖慢测试。
在 import langchain 之前设 false, backend.app.config 的 load_dotenv 默认
override=False 不会覆盖已有环境变量。
"""

from __future__ import annotations

import os

os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")
