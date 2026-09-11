"""内置演示工具集：基于内存 Mock 数据，离线可跑、结果确定。

接入真实业务时，只需用真实实现替换各工具的 run()（查库、调内部 API 等），
spec（名字/描述/参数 schema）与执行层完全不用动。
"""
from __future__ import annotations

import itertools

from ..core.models import ToolResult
from ..support.store import ticket_store
from .base import BaseTool, ToolRegistry, ToolSpec

_ORDER_SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string", "description": "订单号，如 SO20260905001"}},
    "required": ["order_id"],
}

# ---------------------- Mock 数据（模拟业务系统） ----------------------
ORDERS = {
    "SO20260905001": {"order_id": "SO20260905001", "item": "无线蓝牙耳机", "amount": 299.0,
                      "status": "已签收", "signed_days_ago": 2, "created_at": "2026-09-04"},
    "SO20260820002": {"order_id": "SO20260820002", "item": "机械键盘", "amount": 459.0,
                      "status": "已签收", "signed_days_ago": 19, "created_at": "2026-08-16"},
    "SO20260907003": {"order_id": "SO20260907003", "item": "USB-C 数据线", "amount": 39.0,
                      "status": "运输中", "signed_days_ago": None, "created_at": "2026-09-06"},
}

LOGISTICS = {
    "SO20260905001": ["09-05 18:20 订单已发货", "09-06 10:02 到达杭州转运中心",
                      "09-06 21:40 派送中", "09-06 22:15 已签收"],
    "SO20260820002": ["08-20 09:00 订单已发货", "08-21 14:30 已签收"],
    "SO20260907003": ["09-06 20:11 订单已发货", "09-07 16:45 到达上海转运中心", "09-08 08:30 派送中"],
}

REFUND_WINDOW_DAYS = 7

KB = [
    {"q": "7天无理由退货政策",
     "keywords": ["退货", "无理由", "7天", "七天"],
     "a": "商品自签收之日起 7 日内，在不影响二次销售的前提下支持 7 天无理由退货；定制类商品、贴身衣物及已激活的电子数码产品除外。"},
    {"q": "退款时效",
     "keywords": ["退款", "到账", "多久退", "时效"],
     "a": "退款申请审核通过后，1-3 个工作日原路退回您的支付账户。"},
    {"q": "退货运费规则",
     "keywords": ["运费", "邮费", "包邮", "承担"],
     "a": "质量问题退货由商家承担往返运费；无理由退货的退货运费由买家承担。"},
    {"q": "发票政策",
     "keywords": ["发票", "开票", "专票", "普票"],
     "a": "支持电子普票与增值税专票，可在订单详情页申请，开票后 24 小时内发送至您的邮箱。"},
    {"q": "会员权益",
     "keywords": ["会员", "积分", "优惠", "会员日"],
     "a": "每月 8 日为会员日，全场满 300 减 40；会员积分可在结算时抵扣现金。"},
    {"q": "物流时效",
     "keywords": ["物流", "快递", "到哪", "几天到"],
     "a": "可在订单详情页实时查询物流；包裹超过 72 小时未更新轨迹，可在页面申请催单。"},
]

# ---------------------- 工具实现 ----------------------
class QueryOrderTool(BaseTool):
    spec = ToolSpec("query_order", "按订单号查询订单详情（商品、金额、状态、签收时间）", _ORDER_SCHEMA)

    def run(self, ctx, order_id: str) -> ToolResult:
        order = ORDERS.get(str(order_id).upper())
        if not order:
            return ToolResult.fail(self.spec.name, "order_not_found", message=f"未找到订单 {order_id}")
        return ToolResult(self.spec.name, True, dict(order),
                          f"订单 {order['order_id']}：{order['item']}，金额 ¥{order['amount']}，状态 {order['status']}")


class QueryLogisticsTool(BaseTool):
    spec = ToolSpec("query_logistics", "按订单号查询物流轨迹", _ORDER_SCHEMA)

    def run(self, ctx, order_id: str) -> ToolResult:
        trace = LOGISTICS.get(str(order_id).upper())
        if trace is None:
            return ToolResult.fail(self.spec.name, "no_logistics", message=f"订单 {order_id} 暂无物流信息")
        return ToolResult(self.spec.name, True, {"order_id": order_id, "trace": trace, "latest": trace[-1]},
                          f"最新物流：{trace[-1]}")


class CheckRefundEligibilityTool(BaseTool):
    spec = ToolSpec("check_refund_eligibility", "判断订单是否满足 7 天无理由退款条件", _ORDER_SCHEMA)

    def run(self, ctx, order_id: str) -> ToolResult:
        order = ORDERS.get(str(order_id).upper())
        if not order:
            return ToolResult.fail(self.spec.name, "order_not_found", message=f"未找到订单 {order_id}")
        days = order.get("signed_days_ago")
        if days is None:
            return ToolResult(self.spec.name, True,
                              {"eligible": False, "reason": f"订单尚未签收（当前状态：{order['status']}）"},
                              f"订单 {order_id} 未签收，暂不能退款")
        if days <= REFUND_WINDOW_DAYS:
            reason = f"签收至今 {days} 天，在 {REFUND_WINDOW_DAYS} 天无理由退款期内"
            return ToolResult(self.spec.name, True, {"eligible": True, "reason": reason},
                              f"订单 {order_id} 符合退款条件")
        reason = f"签收至今 {days} 天，已超过 {REFUND_WINDOW_DAYS} 天无理由退款期"
        return ToolResult(self.spec.name, True, {"eligible": False, "reason": reason},
                          f"订单 {order_id} 已超出退款期")


class SubmitRefundTool(BaseTool):
    spec = ToolSpec("submit_refund", "提交退款申请（敏感操作，需用户明确确认后才会真正执行）",
                    {"type": "object",
                     "properties": {"order_id": {"type": "string"},
                                    "reason": {"type": "string", "description": "退款原因"}},
                     "required": ["order_id"]},
                    requires_confirm=True)
    _seq = itertools.count(1)

    def run(self, ctx, order_id: str, reason: str = "用户申请退款") -> ToolResult:
        refund_id = f"RF{next(self._seq):06d}"
        return ToolResult(self.spec.name, True,
                          {"refund_id": refund_id, "order_id": order_id, "reason": reason,
                           "eta": "1-3 个工作日原路退回"},
                          f"退款申请已提交，退款单号 {refund_id}，预计 1-3 个工作日原路退回")


class SearchKnowledgeTool(BaseTool):
    spec = ToolSpec("search_knowledge", "在帮助中心知识库中检索与问题最相关的条目",
                    {"type": "object",
                     "properties": {"query": {"type": "string", "description": "用户问题或关键词"}},
                     "required": ["query"]})

    def run(self, ctx, query: str) -> ToolResult:
        q = str(query)
        scored = [(sum(1 for k in e["keywords"] if k in q), e) for e in KB]
        scored = [(s, e) for s, e in scored if s > 0]
        scored.sort(key=lambda x: -x[0])
        hits = [{"title": e["q"], "answer": e["a"]} for _, e in scored[:2]]
        return ToolResult(self.spec.name, True, {"hits": hits},
                          "；".join(h["answer"] for h in hits) if hits else "未命中知识库")


class CreateTicketTool(BaseTool):
    spec = ToolSpec("create_ticket", "创建人工客服工单",
                    {"type": "object",
                     "properties": {"title": {"type": "string"},
                                    "description": {"type": "string"},
                                    "priority": {"type": "string", "enum": ["normal", "high"]}},
                     "required": ["title", "description"]})
    def run(self, ctx, title: str, description: str, priority: str = "normal") -> ToolResult:
        ticket = ticket_store.create(
            session_id=ctx.session.session_id,
            user_id=ctx.session.user_id,
            title=title,
            description=description,
            priority=priority,
        )
        return ToolResult(self.spec.name, True, ticket,
                          f"工单 {ticket['ticket_id']} 已创建（优先级 {priority}），人工客服将尽快与您联系")


def register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    for tool in (QueryOrderTool(), QueryLogisticsTool(), CheckRefundEligibilityTool(),
                 SubmitRefundTool(), SearchKnowledgeTool(), CreateTicketTool()):
        registry.register(tool)
    return registry
