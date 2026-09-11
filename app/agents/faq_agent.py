"""FAQ Agent：基于知识库检索 + LLM 组织答案；未命中且用户情绪不佳时移交人工。"""
from __future__ import annotations

import re

from ..core.context import RunContext
from ..core.models import AgentResult, ToolCall
from .base import AgentDescriptor, BaseAgent


class FAQAgent(BaseAgent):
    descriptor = AgentDescriptor(
        "faq",
        "解答平台政策与常见问题（退货规则、退款时效、发票、会员等）。关键词: 政策,规则,怎么,如何,是什么,条件",
        ("search_knowledge",),
    )
    IRRITATED = re.compile(r"投诉|骗子|差评|曝光|12315|垃圾")

    def handle(self, ctx: RunContext, user_text: str) -> AgentResult:
        result = ctx.executor.call(
            ctx, ToolCall(id="faq_kb", name="search_knowledge", args={"query": user_text}))
        hits = result.data.get("hits", []) if result.success else []
        if hits:
            facts = [h["answer"] for h in hits]
            reply = ctx.llm.compose_answer(user_text, facts)
            return AgentResult(reply=reply, data={"kb_hits": [h["title"] for h in hits]})
        if self.IRRITATED.search(user_text):
            return AgentResult(reply="", handoff="human", data={"reason": "FAQ 未命中且用户情绪不佳"})
        return AgentResult(
            "抱歉，暂时没有找到与您问题相关的内容。您可以说“转人工”，由人工客服为您解答。")
