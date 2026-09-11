"""总控编排 Agent：function calling 式的多 Agent 协作。

与“路由器分发”不同，这里只有一个对外的大脑（LLM），它通过 function calling 在
同一轮内自由组合两类能力，循环决策直到产出最终答复：

- 业务工具：query_order / query_logistics / check_refund_eligibility / submit_refund /
  search_knowledge / create_ticket …（可直接调用，敏感工具仍受确认门禁约束）；
- 专家 Agent（Agent-as-Tool）：ask_order_agent / ask_policy_agent / transfer_to_human，
  委派后专家完整执行自己的逻辑（含其内部的工具编排与确认流程），回复作为函数结果
  返回给总控，总控再决定转达、追问或继续调度。

多轮断点续接：编排过程的对话草稿（含工具调用与结果）持久化在 ctx.state 中，
下一轮恢复后 LLM 看得到“上一轮做到哪一步”；若上轮有敏感操作被确认门禁拦截，
用户回复“确认”后本轮自动放行并重试。接入真实 LLM（--llm openai）时即获得
完整的语义级编排能力；MockLLM 则用规则剧本模拟同等决策，离线可演示。
"""
from __future__ import annotations

import json

from ..core.models import AgentResult, ToolResult
from ..tools.base import BaseTool, ToolSpec
from .base import AgentDescriptor, BaseAgent
from .order_agent import AFFIRM

# 专家 Agent 暴露给 LLM 的函数名
DELEGATION_NAMES = {"order": "ask_order_agent", "faq": "ask_policy_agent", "human": "transfer_to_human"}

MAX_TRANSCRIPT = 40  # 编排对话草稿的保留上限（防止无限膨胀）


class AgentDelegationTool(BaseTool):
    """把一个专家 Agent 包装成可被 LLM function calling 调用的工具。

    专家返回 handoff 时（如 FAQ 未命中且用户情绪不佳 -> human）自动接力移交，
    对总控 LLM 透明。
    """

    def __init__(self, key: str, agent: BaseAgent, sub_agents: dict):
        self.key = key
        self.agent = agent
        self.sub_agents = sub_agents
        self.spec = ToolSpec(
            name=DELEGATION_NAMES.get(key, f"ask_{key}_agent"),
            description=f"{agent.descriptor.description}（委派 {key} 专家完整处理后返回其回复）",
            parameters={"type": "object",
                        "properties": {"query": {"type": "string", "description": "用户诉求原话"}},
                        "required": ["query"]})

    def run(self, ctx, query: str = "") -> ToolResult:
        agent, hops, result = self.agent, 0, None
        while True:
            result = agent.handle(ctx, query)
            if (result.handoff and result.handoff in self.sub_agents
                    and result.handoff != agent.name and hops < 2):
                agent = self.sub_agents[result.handoff]
                hops += 1
                ctx.log("delegation_handoff", to=agent.name)
                continue
            break
        reply = result.reply or "（专家暂未给出有效回复）"
        return ToolResult(self.spec.name, True, {"agent": agent.name, "reply": reply}, reply)


class OrchestratorAgent(BaseAgent):
    descriptor = AgentDescriptor(
        "orchestrator", "总控编排 Agent：通过 function calling 统一调度业务工具与专家 Agent", ())

    MAX_STEPS = 8                    # 单轮内“模型决策 -> 工具执行”的循环上限
    TRANSCRIPT_KEY = "orch_messages"  # 编排对话草稿的持久化键（ctx.state）
    AWAITING_KEY = "orch_awaiting"    # 上轮是否有敏感操作在等用户确认

    SYSTEM_PROMPT = """你是客服总控 Agent，负责调度业务工具与专家 Agent 解决用户问题。
可委派的专家（把用户诉求原话作为 query 传入）：
{experts}
工作准则：
1. 领域问题优先委派对应专家；简单查询也可直接调用业务工具。
2. 专家的回复已面向用户，除需要补充信息外应原样转达，不要编造或篡改数据。
3. 退款等敏感操作必须先征得用户明确确认；独立调用可并行发出。
4. 当前会话已知槽位：{slots}
5. 历史摘要：{summary}"""

    def __init__(self, sub_agents: dict):
        self.sub_agents = sub_agents
        self.delegation_tools = [
            AgentDelegationTool(key, agent, sub_agents) for key, agent in sub_agents.items()]

    def handle(self, ctx, user_text: str) -> AgentResult:
        state = ctx.state
        stored = state.get(self.TRANSCRIPT_KEY)
        if stored:  # 断点续接：恢复上一轮的编排对话草稿
            messages = json.loads(json.dumps(stored))
            messages.append({"role": "user", "content": user_text})
            if state.get(self.AWAITING_KEY) and AFFIRM.search(user_text):
                state["user_confirmed"] = True  # 用户已确认，本轮放行敏感工具
        else:
            messages = [{"role": "system", "content": self.SYSTEM_PROMPT.format(
                experts="\n".join(f"- {t.spec.name}: {t.spec.description}"
                                  for t in self.delegation_tools),
                slots=ctx.memory.slots_text(),
                summary=ctx.session.summary or "（无）")}]
            history = ctx.memory.history_text()
            if history:
                messages.append({"role": "system", "content": f"近期对话：\n{history}"})
            messages.append({"role": "user", "content": user_text})

        blocked, reply = False, ""
        try:
            for _ in range(self.MAX_STEPS):
                resp = ctx.llm.chat(messages, tools=ctx.registry.schemas())
                if resp.tool_calls:
                    messages.append(self._assistant_toolcall_msg(resp))
                    for call in resp.tool_calls:
                        result = ctx.executor.call(ctx, call)
                        if result.data.get("error") == "needs_confirmation":
                            blocked = True
                        messages.append({
                            "role": "tool", "tool_call_id": call.id, "name": call.name,
                            "content": result.message or json.dumps(result.data, ensure_ascii=False)})
                    continue
                reply = resp.content.strip()
                break
            if not reply:
                reply = "这个问题我需要进一步核实，已为您记录，稍后为您跟进；您也可以回复“转人工”。"
        finally:
            state.pop("user_confirmed", None)
            if blocked:
                state[self.AWAITING_KEY] = True
            else:
                state.pop(self.AWAITING_KEY, None)
            state[self.TRANSCRIPT_KEY] = self._trim(messages)  # 检查点：断点续接的依据
        return AgentResult(reply=reply)

    @staticmethod
    def _assistant_toolcall_msg(resp) -> dict:
        return {"role": "assistant", "content": resp.content,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name,
                                             "arguments": json.dumps(c.args, ensure_ascii=False)}}
                               for c in resp.tool_calls]}

    @staticmethod
    def _trim(messages: list[dict], limit: int = MAX_TRANSCRIPT) -> list[dict]:
        if len(messages) <= limit:
            return messages
        cut = messages[-limit:]
        while cut and cut[0].get("role") == "tool":  # 不留下“孤儿”工具结果
            cut.pop(0)
        return cut
