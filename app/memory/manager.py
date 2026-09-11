"""对话记忆管理器：组合三层记忆，支撑连续多轮交互。

- 短期记忆：滑动窗口内的逐条消息（session.messages，上限 WINDOW_SIZE）。
- 长期记忆：被窗口淘汰的消息先进入 overflow，积攒到阈值后由 LLM 压缩为摘要（session.summary）。
- 槽位记忆：从用户消息中抽取的结构化实体（订单号/手机号等），跨轮、跨 agent 共享。

三层内容最终通过 prompt_blocks() 渲染进提示词；整个状态随 SessionData 持久化，
进程重启后对话上下文不丢。
"""
from __future__ import annotations

import time

from ..core.models import Message, SessionData
from .extractor import SlotExtractor

WINDOW_SIZE = 10      # 短期记忆保留的最大消息条数（约 5 轮对话）
SUMMARY_TRIGGER = 3   # 被淘汰消息积攒到该数量时，触发一次摘要压缩


class MemoryManager:
    def __init__(self, session: SessionData, llm):
        self.session = session
        self.llm = llm
        self.extractor = SlotExtractor()

    # ---------- 槽位记忆 ----------
    def set_slot(self, ctx, key: str, value, source: str = "system") -> None:
        if value is None:
            return
        self.session.slots[key] = str(value)
        self.session.slot_turns[key] = self.session.turn_count
        if ctx is not None:
            ctx.log("memory_slot", key=key, value=value, source=source)

    def get_slot(self, key: str, default=None):
        return self.session.slots.get(key, default)

    # ---------- 每轮更新 ----------
    def on_user_turn(self, ctx, text: str) -> None:
        """用户消息入库 + 槽位抽取（在路由/Agent 执行之前，保证本轮即可用）。"""
        self.session.turn_count += 1
        self.session.messages.append(Message(role="user", content=text))
        for key, value in self.extractor.extract(text).items():
            if value != self.session.slots.get(key):
                self.set_slot(ctx, key, value, source="extraction")

    def on_assistant_turn(self, ctx, reply: str, tools_used: list[str]) -> None:
        """回复入库，随后做窗口淘汰与摘要压缩。"""
        self.session.messages.append(
            Message(role="assistant", content=reply, meta={"tools": tools_used}))
        self._evict_and_summarize(ctx)
        self.session.updated_at = time.time()

    def _evict_and_summarize(self, ctx) -> None:
        msgs = self.session.messages
        if len(msgs) <= WINDOW_SIZE:
            return
        evicted = msgs[: len(msgs) - WINDOW_SIZE]
        msgs[:] = msgs[len(msgs) - WINDOW_SIZE:]
        self.session.overflow.extend(m.render() for m in evicted)
        if ctx is not None:
            ctx.log("memory_evict", evicted=len(evicted), overflow=len(self.session.overflow))
        if len(self.session.overflow) >= SUMMARY_TRIGGER:
            self._summarize(ctx)

    def _summarize(self, ctx) -> None:
        parts = ([self.session.summary] if self.session.summary else []) + self.session.overflow
        self.session.summary = self.llm.summarize(parts)
        self.session.overflow.clear()
        if ctx is not None:
            ctx.log("memory_summary", summary=self.session.summary)

    # ---------- 供提示词 / 路由 / 工单使用 ----------
    def history_text(self) -> str:
        return "\n".join(m.render() for m in self.session.messages)

    def slots_text(self) -> str:
        if not self.session.slots:
            return "（无）"
        return "；".join(f"{k}={v}" for k, v in self.session.slots.items())

    def prompt_blocks(self) -> dict:
        return {
            "summary": self.session.summary or "（无）",
            "slots": self.slots_text(),
            "history": self.history_text(),
        }
