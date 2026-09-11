"""订单售后 Agent：订单查询 / 物流跟踪 / 退款流程。

演示要点：
- 槽位复用：order_id 一经提供即入记忆，后续查询物流/退款不再重复询问；
- 断点续接：流程中断（如等用户提供订单号/等确认）会写入 ctx.state.pending，
  下轮由路由器续接回本 Agent 的对应阶段；
- 多工具编排：退款核查用 DAG 计划并行拉取订单+物流+资格三路数据；
- 确认门禁：submit_refund 是敏感操作，用户明确确认后才放行。
"""
from __future__ import annotations

import re

from ..core.context import RunContext
from ..core.models import AgentResult, ToolCall
from ..tools.executor import Plan, PlanStep
from .base import AgentDescriptor, BaseAgent

AFFIRM = re.compile(r"确认|同意|提交|好的?|是的?|^是|ok", re.I)
NEGATE = re.compile(r"取消|不要|不用|算了|先不|不退")


def _fmt_order(order: dict) -> str:
    return f"{order['item']}，金额 ¥{order['amount']}，状态：{order['status']}"


class OrderAgent(BaseAgent):
    descriptor = AgentDescriptor(
        "order",
        "处理订单查询、物流跟踪、退款退货。关键词: 订单,退款,退货,物流,快递,发货,签收",
        ("query_order", "query_logistics", "check_refund_eligibility", "submit_refund"),
    )

    def handle(self, ctx: RunContext, user_text: str) -> AgentResult:
        pending = ctx.state.get("pending")
        if pending and pending.get("agent") == self.name:
            return self._resume(ctx, pending, user_text)
        if any(w in user_text for w in ("退款", "退货", "换货")):
            return self._refund_entry(ctx)
        if any(w in user_text for w in ("物流", "快递", "到哪", "包裹", "发货", "催单")):
            return self._logistics(ctx)
        return self._order_status(ctx)

    # ---------- 退款流程 ----------
    def _refund_entry(self, ctx: RunContext) -> AgentResult:
        order_id = ctx.memory.get_slot("order_id")
        if not order_id:
            ctx.state["pending"] = {"agent": self.name, "action": "refund", "stage": "await_order_id"}
            return AgentResult("好的，为您办理退款。请提供订单号（如 SO20260905001），我先为您核查退款资格。")
        return self._check_refund(ctx, order_id)

    def _check_refund(self, ctx: RunContext, order_id: str) -> AgentResult:
        # DAG 编排：三路查询互不依赖，同波次并行执行
        plan = Plan("退款资格核查", [
            PlanStep("order", "query_order", {"order_id": order_id}),
            PlanStep("logistics", "query_logistics", {"order_id": order_id}),
            PlanStep("eligibility", "check_refund_eligibility", {"order_id": order_id}),
        ])
        r = ctx.executor.run_plan(ctx, plan)
        order_r, elig_r, logi_r = r["order"], r["eligibility"], r["logistics"]
        if not order_r.success or not elig_r.success:
            ctx.state["pending"] = {"agent": self.name, "action": "refund", "stage": "await_order_id"}
            return AgentResult(f"没有查到订单 {order_id}，请核对后重新提供订单号。")
        order, elig = order_r.data, elig_r.data
        if not elig.get("eligible"):
            ctx.state.pop("pending", None)
            latest = logi_r.data.get("latest") if logi_r.success else ""
            extra = f"最新物流：{latest}。" if latest else ""
            return AgentResult(
                f"您的订单{_fmt_order(order)}。经核查：{elig.get('reason', '不符合退款条件')}，"
                f"暂时无法提交退款。{extra}如有疑问可回复“转人工”。",
                data={"order": order, "eligibility": elig})
        ctx.state["pending"] = {"agent": self.name, "action": "refund",
                                "stage": "await_confirm", "order_id": order_id}
        return AgentResult(
            f"已查到您的订单：{_fmt_order(order)}。该订单{elig.get('reason', '')}，符合退款条件，"
            f"退款金额 ¥{order['amount']}。是否确认提交退款申请？回复“确认”提交，回复“取消”放弃。",
            data={"order": order, "eligibility": elig})

    def _await_confirm(self, ctx: RunContext, pending: dict, text: str) -> AgentResult:
        if NEGATE.search(text):
            ctx.state.pop("pending", None)
            return AgentResult("好的，已为您取消本次退款申请。如需再次办理随时告诉我。")
        if AFFIRM.search(text):
            order_id = pending.get("order_id") or ctx.memory.get_slot("order_id")
            ctx.state["user_confirmed"] = True   # 打开确认门禁，执行器才会放行敏感工具
            result = ctx.executor.call(ctx, ToolCall(
                id="submit_refund", name="submit_refund",
                args={"order_id": order_id, "reason": "用户确认申请退款"}))
            ctx.state.pop("user_confirmed", None)
            ctx.state.pop("pending", None)
            if result.success:
                return AgentResult(f"{result.message}。请留意账户到账通知。", data=result.data)
            if result.data.get("error") == "needs_confirmation":
                return AgentResult("该操作需要您明确确认，请回复“确认”以提交退款。")
            return AgentResult(f"退款提交失败：{result.data.get('error', '未知错误')}，请稍后重试或回复“转人工”。")
        return AgentResult("请问是否确认提交退款申请？回复“确认”提交，回复“取消”放弃。")

    # ---------- 物流 / 订单状态 ----------
    def _logistics(self, ctx: RunContext) -> AgentResult:
        order_id = ctx.memory.get_slot("order_id")
        if not order_id:
            ctx.state["pending"] = {"agent": self.name, "action": "logistics", "stage": "await_order_id"}
            return AgentResult("请提供需要查询物流的订单号（如 SO20260905001）。")
        plan = Plan("订单与物流查询", [
            PlanStep("order", "query_order", {"order_id": order_id}),
            PlanStep("logistics", "query_logistics", {"order_id": order_id}),
        ])
        r = ctx.executor.run_plan(ctx, plan)
        if not r["order"].success:
            ctx.state.pop("pending", None)
            return AgentResult(f"没有查到订单 {order_id}，请核对后重新提供。")
        order = r["order"].data
        trace = r["logistics"].data.get("trace", []) if r["logistics"].success else []
        lines = "\n".join(f"· {t}" for t in trace) or "· 暂无物流轨迹"
        return AgentResult(f"订单{_fmt_order(order)}的物流轨迹如下：\n{lines}",
                           data={"order": order, "trace": trace})

    def _order_status(self, ctx: RunContext) -> AgentResult:
        order_id = ctx.memory.get_slot("order_id")
        if not order_id:
            ctx.state["pending"] = {"agent": self.name, "action": "order_status", "stage": "await_order_id"}
            return AgentResult("请提供需要查询的订单号（如 SO20260905001）。")
        result = ctx.executor.call(
            ctx, ToolCall(id="q1", name="query_order", args={"order_id": order_id}))
        ctx.state.pop("pending", None)
        if not result.success:
            return AgentResult(f"没有查到订单 {order_id}，请核对后重新提供。")
        return AgentResult(f"您的订单信息：{_fmt_order(result.data)}。还有什么可以帮您？",
                           data=result.data)

    # ---------- 断点续接 ----------
    def _resume(self, ctx: RunContext, pending: dict, text: str) -> AgentResult:
        stage = pending.get("stage")
        if stage == "await_confirm":
            return self._await_confirm(ctx, pending, text)
        if stage == "await_order_id":
            if NEGATE.search(text):
                ctx.state.pop("pending", None)
                return AgentResult("好的，已取消办理。还有其他可以帮您吗？")
            order_id = ctx.memory.get_slot("order_id")
            if not order_id:
                return AgentResult("暂时没有识别到订单号，请提供订单号（如 SO20260905001）。")
            ctx.state.pop("pending", None)
            if pending.get("action") == "logistics":
                return self._logistics(ctx)
            if pending.get("action") == "order_status":
                return self._order_status(ctx)
            return self._check_refund(ctx, order_id)
        ctx.state.pop("pending", None)
        return self.handle(ctx, text)  # 未知阶段：重新走入口
