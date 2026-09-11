"""调度引擎（Scheduler）：整个系统的中枢。

一次请求的管线：
  1. 取会话 + 建记忆管理器          —— FileSessionStore / MemoryManager
  2. 记忆写前更新                   —— 用户消息入短期记忆，槽位抽取（本轮立即可用）
  3. 上下文路由                     —— Router 决定目标 Agent（规则/待办续接/LLM 分类）
  4. Agent 执行                     —— 支持有限次数的 Agent 间移交（handoff 循环）
  5. 记忆写回                       —— 回复入库，触发窗口淘汰与长期摘要
  6. 持久化                         —— 会话整体落盘，多轮/重启不丢上下文

设计要点：
- Agent 单实例复用，无请求内状态；全部可变状态集中在 SessionData（可持久化）与
  RunContext.trace（本轮观测轨迹），因此天然支持并发会话与水平扩展。
- 任何 Agent / 工具异常都被引擎兜底，最多降级为一句道歉回复，不会击穿服务。
"""
from __future__ import annotations

import copy

from ..agents.base import BaseAgent
from ..agents.faq_agent import FAQAgent
from ..agents.human_agent import HumanAgent
from ..agents.orchestrator import OrchestratorAgent
from ..agents.order_agent import OrderAgent
from ..agents.router import Router
from ..agents.support_agent import SupportFallbackAgent
from ..core.context import RunContext
from ..core.llm import BaseLLM, MockLLM
from ..core.models import AgentReply, AgentResult, RouteDecision
from ..memory.manager import MemoryManager
from ..memory.store import FileSessionStore
from ..tools.base import ToolRegistry
from ..tools.builtin import register_builtin_tools
from ..tools.executor import ToolExecutor

MAX_HANDOFFS = 3  # 单轮内 Agent 间移交的上限，防止循环移交

# 草稿/预回答预览时禁止产生真实写副作用的工具：不得新建工单、不得提交退款。
DRAFT_BLOCKED_TOOLS = frozenset({"create_ticket", "submit_refund"})

# - pipeline     规则优先路由 + 专家 Agent（默认：高频意图零延迟，LLM 仅兜底）
# - llm          LLM 全权语义路由 + 专家 Agent（每轮由模型决策，规则退化为安全网）
# - orchestrator function calling 多 Agent 协作：总控 Agent 统一调度业务工具与专家 Agent
MODES = ("pipeline", "llm", "orchestrator")


class Scheduler:
    def __init__(self, llm: BaseLLM | None = None, store: FileSessionStore | None = None,
                 registry: ToolRegistry | None = None,
                 agents: dict[str, BaseAgent] | None = None, mode: str = "pipeline"):
        if mode not in MODES:
            raise ValueError(f"未知运行模式: {mode}，可选 {MODES}")
        self.mode = mode
        self.llm = llm or MockLLM()
        self.store = store or FileSessionStore()
        self.registry = registry or register_builtin_tools(ToolRegistry())
        self.agents = agents or {"order": OrderAgent(), "faq": FAQAgent(), "human": HumanAgent()}
        self.support_agent = SupportFallbackAgent()
        if mode == "orchestrator":
            self.orchestrator = OrchestratorAgent(self.agents)
            for tool in self.orchestrator.delegation_tools:  # 专家 Agent 以函数形式进入注册表
                self.registry.register(tool)
            self.agents = {**self.agents, "orchestrator": self.orchestrator}
            self.router = None
        else:
            self.router = Router(self.agents, mode="llm" if mode == "llm" else "rules")
        self.executor = ToolExecutor(self.registry)
        self._draft_executor: ToolExecutor | None = None

    def process(self, session_id: str, text: str, user_id: str = "anonymous",
                preferred_agent: str | None = None) -> AgentReply:
        """客户端正式回复链路：写记忆、走 Agent，并把整段会话持久化。"""
        session = self.store.get_or_create(session_id, user_id)
        return self._run_pipeline(session, text, preferred_agent, persist=True)

    def preview_reply(self, session_id: str, text: str, user_id: str = "anonymous",
                      preferred_agent: str | None = None) -> AgentReply:
        """以与客户端完全相同的 Agent 回复链路生成“预回答草稿”。

        与 process 的区别仅在于安全隔离，输出内容一致：
        - 在会话的深拷贝上运行，不向客户真实会话写入消息/槽位/轮次，也不落盘；
        - 工具层屏蔽建单、退款等写操作，预回答不会产生任何业务副作用；
        - 路由落到“转人工/无法判定”时改由排队托管 Agent 直接生成草稿，
          保证后台总能拿到一条结合上下文的 DeepSeek 预回答。
        """
        session = self.store.get_or_create(session_id, user_id)
        sandbox = copy.deepcopy(session)
        return self._run_pipeline(sandbox, text, preferred_agent, persist=False, draft=True)

    def _draft_tool_executor(self) -> "ToolExecutor":
        """草稿模式专用执行器：复用无状态工具实例，但剔除有写副作用的工具。"""
        if self._draft_executor is None:
            registry = ToolRegistry()
            for name in self.registry.names():
                if name not in DRAFT_BLOCKED_TOOLS:
                    registry.register(self.registry.get(name))
            self._draft_executor = ToolExecutor(registry)
        return self._draft_executor

    def _run_pipeline(self, session, text: str, preferred_agent: str | None,
                      *, persist: bool, draft: bool = False) -> AgentReply:
        # 1. 会话与记忆（草稿模式使用只读工具执行器，杜绝写副作用）
        memory = MemoryManager(session, self.llm)
        executor = self._draft_tool_executor() if draft else self.executor
        ctx = RunContext(session, self.llm, memory, self.registry, executor)

        # 2. 记忆写前更新：消息入短期记忆 + 槽位抽取
        memory.on_user_turn(ctx, text)

        # 3. 上下文路由（orchestrator 模式下由总控 Agent 全权接管，不走路由器）
        support = session.state.get("support") or {}
        if support.get("status") == "waiting":
            decision = RouteDecision("support", "人工排队中，由智能 Agent 临时托管", 1.0)
        elif preferred_agent and preferred_agent in self.agents and preferred_agent != "orchestrator":
            decision = RouteDecision(
                preferred_agent, f"用户选择预设 Agent：{preferred_agent}", 1.0)
        elif self.router is None:
            decision = RouteDecision("orchestrator", "function-calling 编排（LLM 全权调度）", 1.0)
        else:
            decision = self.router.route(ctx, text)

        # 草稿模式：转人工/无法判定时不建单、不抛澄清，统一由托管 Agent 生成预回答
        if draft and (decision.route is None or decision.route == "human"):
            decision = RouteDecision("support", "预回答草稿：复用客户端托管 Agent 生成", 1.0)

        # 4. Agent 执行（含移交循环 / 澄清兜底）
        tools_used: list[str] = []
        if decision.route is None:
            agent_name = "clarify"
            reply_text = ("抱歉，我还没理解您的需求。您可以：\n"
                          "· 咨询订单、物流、退款问题\n"
                          "· 咨询平台政策（如退货规则、发票、会员）\n"
                          "· 回复“转人工”接入人工客服")
            data: dict = {}
        else:
            agent = self.support_agent if decision.route == "support" else self.agents[decision.route]
            hops = 0
            while True:
                ctx.log("agent_start", agent=agent.name, reason=decision.reason)
                try:
                    result = agent.handle(ctx, text)
                except Exception as e:
                    ctx.log("agent_error", agent=agent.name, error=str(e))
                    result = AgentResult(reply="系统开小差了，请稍后再试，或回复“转人工”由人工为您服务。")
                ctx.log("agent_end", agent=agent.name)
                if result.handoff and result.handoff in self.agents and hops < MAX_HANDOFFS:
                    ctx.log("handoff", from_agent=agent.name, to=result.handoff)
                    # 草稿模式下 handoff 到 human 同样改走托管 Agent，避免触发建单
                    if draft and result.handoff == "human":
                        agent = self.support_agent
                    else:
                        agent = self.agents[result.handoff]
                    hops += 1
                    continue
                agent_name, reply_text, data = agent.name, result.reply, result.data
                break

        # 5. 记忆写回：回复入库 + 窗口淘汰 + 摘要压缩（草稿模式只作用于副本）
        tools_used = list(dict.fromkeys(
            e["tool"] for e in ctx.trace if e["event"] == "tool_end"))
        memory.on_assistant_turn(ctx, reply_text, tools_used)

        # 6. 持久化（草稿预览不落盘，保持客户真实会话不变）
        if agent_name in self.agents or agent_name == "support":
            session.last_agent = agent_name
        if persist:
            self.store.save(session)

        return AgentReply(
            session_id=session.session_id, text=reply_text, agent=agent_name,
            route_reason=decision.reason, tools_used=tools_used,
            slots=dict(session.slots), turns=session.turn_count,
            has_summary=bool(session.summary), trace=list(ctx.trace))
