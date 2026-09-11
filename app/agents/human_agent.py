"""转人工 Agent：创建工单（附带会话摘要与已知槽位），并清空待办流程。"""
from __future__ import annotations

from ..core.context import RunContext
from ..core.models import AgentResult, ToolCall
from .base import AgentDescriptor, BaseAgent


class HumanAgent(BaseAgent):
    descriptor = AgentDescriptor(
        "human",
        "处理投诉与复杂问题，转接人工客服。关键词: 转人工,人工,投诉,举报",
        ("create_ticket",),
    )

    def handle(self, ctx: RunContext, user_text: str) -> AgentResult:
        priority = "high" if any(w in user_text for w in ("投诉", "举报", "骗子", "曝光", "12315")) else "normal"
        ctx.state.pop("pending", None)  # 转人工后清空机器人侧的待办流程
        result = ctx.executor.call(ctx, ToolCall(
            id="create_ticket", name="create_ticket",
            args={
                "title": f"用户反馈：{user_text[:20]}",
                "description": (
                    f"会话摘要：{ctx.session.summary or '（无）'}\n"
                    f"已知信息：{ctx.memory.slots_text()}\n"
                    f"用户诉求：{user_text}"),
                "priority": priority,
            }))
        if result.success:
            ctx.state["support"] = {
                "ticket_id": result.data["ticket_id"],
                "status": "waiting",
                "assigned_to": None,
            }
            return AgentResult(
                f"已为您进入人工服务队列。{result.message}。等待期间由智能客服临时响应，"
                "人工客服接入后会自动切换，您也可以继续补充说明。",
                data=result.data)
        return AgentResult("工单创建失败，请稍后重试，或拨打客服热线 400-000-0000。")
