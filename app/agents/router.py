"""上下文路由：决定这轮消息交给哪个 Agent。

两种模式（构造时通过 mode 指定）：

- rules（默认，pipeline 模式使用）—— 三级策略按序生效：
  1. 高优先级规则：显式诉求（转人工/投诉）直接命中，保证用户随时能逃离机器人；
  2. 待办续接：会话存在未完成流程（ctx.state.pending）时续接到对应 Agent，
     用户补一句订单号/“确认”即可从断点继续，无需重新表达意图；
  3. 规则关键词 → LLM 意图分类兜底 → 都失败返回 None，由调度引擎澄清反问。

- llm（LLM 全权路由模式）—— 每轮都由 LLM 依据“路由职责描述 + 会话上下文
  （待办流程/槽位/近期对话）”做语义决策，规则退化为 LLM 失误时的安全网。
  待办续接不再是硬编码逻辑，而是作为上下文交给 LLM 判断。
"""
from __future__ import annotations

import re

from ..core.context import RunContext
from ..core.models import RouteDecision

# 顺序即优先级（rules 模式）
RULES: list[tuple[str, re.Pattern]] = [
    ("human", re.compile(r"转人工|人工客服|投诉|举报|客服专员|态度差")),
    ("faq", re.compile(r"政策|规则|条件|支持.{0,8}吗|可以.{0,8}吗|能.{0,8}吗|无理由|几天|多久|"
                       r"流程|范围|需要什么|发票|会员|优惠")),
    ("order", re.compile(r"订单|退款|退货|换货|物流|快递|发货|签收|收货|包裹|到哪|催单")),
]
ORDER_ID_RE = re.compile(r"(?<![A-Za-z0-9])(SO\d{6,})(?!\d)", re.I)


class Router:
    def __init__(self, agents: dict, mode: str = "rules"):
        if mode not in ("rules", "llm"):
            raise ValueError(f"未知路由模式: {mode}")
        self.mode = mode
        self.agents = agents  # name -> BaseAgent

    def routes_for_llm(self) -> dict[str, str]:
        return {a.descriptor.name: a.descriptor.description for a in self.agents.values()}

    def route(self, ctx: RunContext, text: str) -> RouteDecision:
        if self.mode == "llm":
            return self._route_by_llm(ctx, text)
        return self._route_by_rules(ctx, text)

    # ---------- LLM 全权路由 ----------
    def _route_by_llm(self, ctx: RunContext, text: str) -> RouteDecision:
        context = {
            "pending": ctx.state.get("pending"),
            "slots": ctx.session.slots,
            "recent_dialogue": [m.render() for m in ctx.session.messages[-4:]],
        }
        key, conf = ctx.llm.classify_intent(text, self.routes_for_llm(), context)
        if key in self.agents:
            return RouteDecision(key, f"LLM 语义路由（置信度 {conf}）", conf)
        decision = self._route_by_rules(ctx, text)  # LLM 失误时的安全网
        if decision.route:
            return RouteDecision(decision.route, f"{decision.reason}（LLM 未命中，规则兜底）",
                                 min(decision.confidence, 0.6))
        return RouteDecision(None, "LLM 与规则均无法判定，需要澄清", 0.0)

    # ---------- 规则路由（pipeline 模式 / 安全网） ----------
    def _route_by_rules(self, ctx: RunContext, text: str) -> RouteDecision:
        for name, pattern in RULES:
            m = pattern.search(text)
            if m and name in self.agents:
                return RouteDecision(name, f"规则命中「{m.group(0)}」", 0.9)

        if ORDER_ID_RE.search(text) and "order" in self.agents:
            return RouteDecision("order", "消息中包含订单号", 0.95)

        pending = ctx.state.get("pending")
        if pending and pending.get("agent") in self.agents:
            return RouteDecision(
                pending["agent"],
                f"续接未完成流程：{pending.get('action', '')}/{pending.get('stage', '')}",
                0.8, resumed=True)

        key, conf = ctx.llm.classify_intent(text, self.routes_for_llm())
        if key in self.agents:
            return RouteDecision(key, f"LLM 意图分类（置信度 {conf}）", conf)
        return RouteDecision(None, "规则与 LLM 均无法判定，需要澄清", 0.0)
