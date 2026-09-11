"""Agent 抽象基类。

每个业务 Agent 有一个 AgentDescriptor（名字/职责描述/可用工具白名单），
职责描述同时供路由器的 LLM 意图分类使用（约定“关键词: a,b,c”段落供 MockLLM 匹配）。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

from ..core.models import AgentResult
from ..core.context import RunContext


@dataclass
class AgentDescriptor:
    name: str
    description: str                       # 供路由使用，如“负责…。关键词: 订单,退款”
    allowed_tools: tuple = ()              # 该 agent 可调用的工具白名单


class BaseAgent(abc.ABC):
    descriptor: AgentDescriptor

    @abc.abstractmethod
    def handle(self, ctx: RunContext, user_text: str) -> AgentResult:
        """处理一轮用户消息；需要移交给其他 agent 时返回 AgentResult(handoff=...)。"""

    @property
    def name(self) -> str:
        return self.descriptor.name
