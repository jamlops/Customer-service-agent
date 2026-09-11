"""核心数据模型：消息、工具调用、路由决策、会话状态等。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Message:
    """一条对话消息（记忆的基本单元）。"""
    role: str                                  # user / assistant / system
    content: str
    name: Optional[str] = None                 # 产生该消息的主体（如 agent 名）
    ts: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)   # 附加信息，如 {"tools": [...]}

    def render(self) -> str:
        who = "用户" if self.role == "user" else "客服"
        return f"{who}: {self.content}"


@dataclass
class ToolCall:
    """一次工具调用请求。"""
    id: str
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """工具执行结果；message 是面向用户的可读摘要。"""
    name: str = ""
    success: bool = True
    data: dict = field(default_factory=dict)
    message: str = ""

    @classmethod
    def fail(cls, name: str, error: str, data: dict | None = None, message: str = "") -> "ToolResult":
        return cls(name=name, success=False, data={"error": error, **(data or {})}, message=message)


@dataclass
class LLMResponse:
    """LLM 一次响应：文本内容 +（可选）工具调用请求。"""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class RouteDecision:
    """路由决策：交给哪个 agent、为什么。"""
    route: Optional[str]          # 目标 agent 名；None 表示无法判定
    reason: str = ""
    confidence: float = 0.0
    resumed: bool = False         # 是否由“待办上下文”续接（多轮断点恢复）


@dataclass
class AgentResult:
    """单个 agent 的处理结果。"""
    reply: str = ""
    handoff: Optional[str] = None  # 需要移交给的其他 agent 名
    data: dict = field(default_factory=dict)


@dataclass
class AgentReply:
    """调度引擎对外的最终回复（含过程信息，便于观测与调试）。"""
    session_id: str = ""
    text: str = ""
    agent: str = ""
    route_reason: str = ""
    tools_used: list[str] = field(default_factory=list)
    slots: dict = field(default_factory=dict)
    turns: int = 0
    has_summary: bool = False
    trace: list[dict] = field(default_factory=list)


@dataclass
class SessionData:
    """会话的完整可持久化状态（记忆机制的数据载体）。"""
    session_id: str
    user_id: str = "anonymous"
    messages: list[Message] = field(default_factory=list)   # 短期记忆：受窗口约束的近期消息
    overflow: list[str] = field(default_factory=list)       # 被窗口淘汰的消息（待摘要）
    summary: str = ""                                       # 长期记忆：历史对话摘要
    slots: dict[str, str] = field(default_factory=dict)     # 槽位记忆：跨轮结构化实体
    slot_turns: dict[str, int] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)     # 跨轮工作状态（待办流程等）
    last_agent: Optional[str] = None
    turn_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SessionData":
        raw_messages = d.pop("messages", [])
        session = cls(**d)
        session.messages = [Message(**m) for m in raw_messages]
        return session
