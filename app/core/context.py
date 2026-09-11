"""运行时上下文：一次请求处理过程中，各组件（记忆/工具/Agent/LLM）共享的执行环境。"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅为类型标注，避免循环导入
    from .llm import BaseLLM
    from .models import SessionData
    from ..memory.manager import MemoryManager
    from ..tools.base import ToolRegistry
    from ..tools.executor import ToolExecutor


class RunContext:
    """每个用户请求创建一个实例，贯穿“记忆 → 路由 → Agent → 工具”全流程。"""

    def __init__(self, session: "SessionData", llm: "BaseLLM", memory: "MemoryManager",
                 registry: "ToolRegistry", executor: "ToolExecutor"):
        self.session = session
        self.llm = llm
        self.memory = memory
        self.registry = registry
        self.executor = executor
        self.trace: list[dict] = []      # 本轮执行轨迹（观测/调试用，不持久化）
        self.started_at = time.time()

    @property
    def state(self) -> dict:
        """跨轮工作状态（持久化在会话里），如待办流程 pending、确认标记。"""
        return self.session.state

    def log(self, event: str, **fields) -> None:
        self.trace.append({"event": event, "t": round(time.time() - self.started_at, 3), **fields})
