"""人工排队期间的 DeepSeek 临时托管 Agent。"""
from __future__ import annotations

from ..core.context import RunContext
from ..core.models import AgentResult
from .base import AgentDescriptor, BaseAgent


class SupportFallbackAgent(BaseAgent):
    descriptor = AgentDescriptor(
        "support",
        "人工客服尚未接单时临时响应，结合会话上下文继续提供基础协助。",
    )

    def handle(self, ctx: RunContext, user_text: str) -> AgentResult:
        support = ctx.state.get("support") or {}
        if support.get("status") == "waiting":
            role_line = "你是电商平台的临时客服 Agent。用户已经申请人工服务，但当前仍在排队。"
            system_line = "你是正在为排队用户提供临时支持的客服 Agent。"
            fallback_line = "人工接入前你会继续协助。"
        else:
            # 人工已接入或后台预回答：同一套 Agent 链路为坐席草拟一条可直接发送的回复
            role_line = "你是电商平台的客服 Agent，正在结合会话上下文生成一条发给客户的回复。"
            system_line = "你是辅助人工坐席生成回复的客服 Agent。"
            fallback_line = "若信息不足，说明已记录补充信息并会继续跟进。"
        prompt = (
            f"{role_line}\n"
            "请结合下方上下文直接回答当前问题，语气简洁自然。不得声称自己是人工客服，"
            "不得承诺具体接通时间，不得编造订单或政策事实。若信息不足，说明已记录补充信息，"
            f"{fallback_line}\n\n"
            f"已知槽位：{ctx.memory.slots_text()}\n"
            f"历史摘要：{ctx.session.summary or '（无）'}\n"
            f"近期对话：\n{ctx.memory.history_text()}\n\n"
            f"用户当前消息：{user_text}"
        )
        response = ctx.llm.chat([
            {"role": "system", "content": system_line},
            {"role": "user", "content": prompt},
        ], temperature=0.3)
        answer = (response.content or "").strip()
        if not answer or answer.startswith("[mock]"):
            answer = "您的补充信息已记录。人工客服接入前，我会继续为您提供基础协助。"
        ticket_id = support.get("ticket_id")
        return AgentResult(answer, data={"ticket_id": ticket_id, "support_status": "waiting"})

