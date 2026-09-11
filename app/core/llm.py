"""LLM 抽象层。

- MockLLM：离线确定性实现，保证示例与测试无需任何 API Key 即可完整跑通。
  它用规则“模拟”真实模型的行为：意图分类（含会话上下文续接判断）与
  function calling 编排（带 tools 的 chat 调用会被当作总控 Agent 的大脑来决策）。
- OpenAICompatLLM：兼容 OpenAI /chat/completions 协议的任意服务（含 function calling），
  通过环境变量 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL 配置。

除 chat 外还定义了三个面向客服场景的高层能力（意图分类 / 摘要 / 答案生成），
默认基于 chat 实现，MockLLM 覆写为规则版本以支持离线演示。
"""
from __future__ import annotations

import abc
import json
import os
import re
import urllib.error
import urllib.request

from .models import LLMResponse, ToolCall


class BaseLLM(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.3) -> LLMResponse:
        """与模型对话；tools 为 OpenAI function calling 格式的工具 schema 列表。"""

    # ---- 以下高层能力默认基于 chat 实现，子类可覆写为确定性版本 ----
    def classify_intent(self, text: str, routes: dict[str, str],
                        context: dict | None = None) -> tuple[str, float]:
        """意图路由；context 携带待办流程/槽位/近期对话，供模型判断“是否为流程续接”。"""
        route_lines = "\n".join(f"- {k}: {v}" for k, v in routes.items())
        context_block = ""
        if context:
            context_block = ("\n会话上下文（JSON，注意 pending 为未完成流程，"
                             "用户简短补充/肯定回复时通常应续接该流程）：\n"
                             + json.dumps(context, ensure_ascii=False))
        prompt = (
            "你是客服系统的意图路由器，根据用户消息与会话上下文选择最合适的路由。\n"
            f"可选路由:\n{route_lines}\n"
            f"{context_block}\n\n"
            "判定提示：\n"
            "- 用户描述商品/订单/物流问题并寻求处理（如商品故障、损坏、退款退货诉求、查询进度）"
            "应归入订单售后类路由；\n"
            "- 咨询政策、规则、条件、流程、资费类问题归入政策问答类路由；\n"
            "- 投诉或明确要求人工服务归入转人工类路由；\n"
            "- 仅当无法归入任何一类时才输出 unknown。\n\n"
            f"用户消息: {text}\n"
            "只输出路由 key，不要输出其他内容；无法判断时输出 unknown。"
        )
        resp = self.chat([{"role": "user", "content": prompt}], temperature=0.0)
        out = (resp.content or "").strip()
        key = out.splitlines()[0].strip() if out else "unknown"
        if key not in routes:  # 模型未严格遵守指令时，从回复中提取唯一的路由 key
            hits = [k for k in routes if k in out.lower()]
            key = hits[0] if len(hits) == 1 else "unknown"
        return (key, 0.8) if key in routes else ("unknown", 0.0)

    def summarize(self, texts: list[str]) -> str:
        prompt = "请将以下客服对话片段浓缩为不超过 120 字的要点摘要：\n" + "\n".join(texts)
        return self.chat([{"role": "user", "content": prompt}], temperature=0.2).content.strip()

    def compose_answer(self, question: str, facts: list[str]) -> str:
        prompt = (
            "你是平台客服。仅依据下列事实回答用户问题，不要编造。\n"
            "事实:\n" + "\n".join(f"- {f}" for f in facts) +
            f"\n\n用户问题: {question}"
        )
        return self.chat([{"role": "user", "content": prompt}]).content.strip()


class MockLLM(BaseLLM):
    """离线 mock：用规则模拟真实模型的两类决策，保证全链路无网络可演示、可测试。"""
    name = "mock"

    # 模拟“语义理解”的模式库（真实 LLM 不需要这些，仅用于离线确定性演示）
    _POLICY_Q = re.compile(r"政策|规则|条件|支持.{0,8}吗|可以.{0,8}吗|能.{0,8}吗|无理由|几天|多久|"
                           r"发票|会员|优惠|怎么退|如何退|什么流程")
    _ORDER_KW = ("订单", "退款", "退货", "换货", "物流", "快递", "发货", "签收", "到哪", "包裹", "查单")
    _HUMAN_KW = ("转人工", "人工客服", "投诉", "举报", "态度差", "骗子")
    _AFFIRM = re.compile(r"^(确认|是的?|好的?|好|是|同意|提交|ok)", re.I)
    _NEGATE = re.compile(r"取消|不要|不用|算了")
    _SO_ID = re.compile(r"SO\d{6,}", re.I)

    def chat(self, messages: list[dict], tools=None, temperature=0.3) -> LLMResponse:
        if not tools:
            last = messages[-1]["content"][:40] if messages else ""
            return LLMResponse(content=f"[mock] {last}")
        # 带 tools 的调用视为总控编排 Agent 的大脑决策
        return self._orchestrate(messages, tools)

    # ---------- 意图路由（LLM 全权路由模式的模拟实现） ----------
    def classify_intent(self, text: str, routes: dict[str, str],
                        context: dict | None = None) -> tuple[str, float]:
        low = text.lower()
        pending = (context or {}).get("pending") or {}
        target = pending.get("agent")
        if target in routes and (self._SO_ID.search(text) or self._AFFIRM.search(text)):
            return target, 0.85                     # 明确在续接待办流程
        if self._POLICY_Q.search(text) and "faq" in routes:
            return "faq", 0.8                       # 政策类问句优先于动作意图
        if target in routes and len(text.strip()) <= 12 and not self._NEGATE.search(text):
            return target, 0.8                      # 简短补充信息视作续接
        best_key, best_score = "unknown", 0
        for key, desc in routes.items():            # 关键词加权评分，模拟语义匹配
            score = 0
            if "关键词:" in desc:
                kws = desc.split("关键词:", 1)[1].replace("，", ",").split(",")
                score = sum(1 for k in kws if k.strip().lower() and k.strip().lower() in low)
            if score > best_score:
                best_key, best_score = key, score
        return (best_key, 0.7) if best_score else ("unknown", 0.0)

    def summarize(self, texts: list[str]) -> str:
        head = "；".join(t[:24] for t in texts[:6])
        more = f" 等{len(texts)}条" if len(texts) > 6 else ""
        return f"此前对话要点：{head}{more}。"

    def compose_answer(self, question: str, facts: list[str]) -> str:
        lines = "\n".join(f"· {f}" for f in facts)
        return (f"关于您咨询的「{question}」，为您说明如下：\n{lines}\n"
                "如仍有疑问，可回复“转人工”由人工客服为您解答。")

    # ---------- function calling 编排（模拟总控 Agent 的大脑） ----------
    def _orchestrate(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        names = {t["name"] for t in tools}
        last_user, last_user_idx, last_tool_idx, last_tool_content = "", -1, -1, ""
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                last_user, last_user_idx = m.get("content", ""), i
            elif m.get("role") == "tool":
                last_tool_idx, last_tool_content = i, m.get("content", "")

        def delegate(name: str, query: str) -> LLMResponse:
            return LLMResponse(tool_calls=[ToolCall(
                id=f"mock-{name}-{last_user_idx}", name=name, args={"query": query})])

        # 工具结果尚未答复 -> 生成最终回复（转达专家结论）
        if last_tool_idx > last_user_idx:
            if "需要用户确认" in last_tool_content:
                return LLMResponse(content=f"{last_tool_content}请回复“确认”继续。")
            return LLMResponse(content=last_tool_content or "已为您处理完毕。")

        text = last_user
        if self._NEGATE.search(text):
            return LLMResponse(content="好的，已为您取消本次操作。还有其他可以帮您吗？")
        if any(k in text for k in self._HUMAN_KW) and "transfer_to_human" in names:
            return delegate("transfer_to_human", text)
        # 肯定回复 + 此前有专家在等确认 -> 回到该流程
        if self._AFFIRM.search(text) and any(
                "是否确认" in m.get("content", "") for m in messages if m.get("role") == "tool"):
            if "ask_order_agent" in names:
                return delegate("ask_order_agent", text)
        if self._POLICY_Q.search(text) and "ask_policy_agent" in names:
            return delegate("ask_policy_agent", text)
        if self._SO_ID.search(text) or any(k in text for k in self._ORDER_KW):
            if "ask_order_agent" in names:
                return delegate("ask_order_agent", text)
        if self._AFFIRM.search(text):               # 直连工具被确认门禁拦截后的重试
            blocked = self._find_blocked_call(messages)
            if blocked and blocked["name"] in names:
                return LLMResponse(tool_calls=[ToolCall(
                    id=f"mock-retry-{last_user_idx}", name=blocked["name"], args=blocked["args"])])
        return LLMResponse(content="抱歉，我还没理解您的需求。您可以咨询订单、物流、退款问题，"
                                   "平台政策，或回复“转人工”。")

    @staticmethod
    def _find_blocked_call(messages: list[dict]) -> dict | None:
        """找到最近一次被确认门禁拦截的工具调用（编排层直连工具场景）。"""
        pending = None
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                pending = m["tool_calls"][-1]
            elif m.get("role") == "tool" and pending is not None:
                if "需要用户确认" in m.get("content", ""):
                    fn = pending.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    return {"name": fn.get("name"), "args": args}
                pending = None
        return None


class OpenAICompatLLM(BaseLLM):
    """通过 OpenAI /chat/completions 协议调用真实模型（支持 function calling）。"""
    name = "openai-compat"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: int = 30):
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        self.thinking = os.getenv("LLM_THINKING", "enabled").lower()
        self.reasoning_effort = os.getenv("LLM_REASONING_EFFORT", "high")

    def chat(self, messages: list[dict], tools=None, temperature=0.3) -> LLMResponse:
        payload: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if "deepseek.com" in self.base_url and self.thinking in ("enabled", "disabled"):
            payload["thinking"] = {"type": self.thinking}
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM 请求失败: {e}") from e
        msg = body["choices"][0]["message"]
        calls = [
            ToolCall(id=c["id"], name=c["function"]["name"],
                     args=json.loads(c["function"].get("arguments") or "{}"))
            for c in msg.get("tool_calls") or []
        ]
        return LLMResponse(content=msg.get("content") or "", tool_calls=calls)


class ResilientLLM(BaseLLM):
    """主 LLM + 备用 LLM：主模型调用失败（网络/限流/鉴权）时自动降级到备用实现。

    用于“规则优先、LLM 兜底”的部署形态：兜底分类 / 摘要 / 答案生成挂真实模型，
    模型不可用时退回 Mock 规则版本，外部依赖故障不击穿服务。
    """
    name = "resilient"

    def __init__(self, primary: BaseLLM, fallback: BaseLLM | None = None):
        self.primary = primary
        self.fallback = fallback or MockLLM()
        self.primary_enabled = True
        self.name = f"{primary.name}+{self.fallback.name}"

    def set_primary_enabled(self, enabled: bool) -> None:
        """运行时启停外部主模型；关闭后所有能力统一走离线备用实现。"""
        self.primary_enabled = bool(enabled)

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.3) -> LLMResponse:
        if not self.primary_enabled:
            return self.fallback.chat(messages, tools=tools, temperature=temperature)
        try:
            return self.primary.chat(messages, tools=tools, temperature=temperature)
        except Exception:
            return self.fallback.chat(messages, tools=tools, temperature=temperature)

    # 高层能力同样走降级：真实 LLM 基于 chat 实现即可由 chat 兜住，
    # 但子类可能覆写，这里显式转发保证任一实现抛异常都能退回备用。
    def classify_intent(self, text: str, routes: dict[str, str],
                        context: dict | None = None) -> tuple[str, float]:
        if not self.primary_enabled:
            return self.fallback.classify_intent(text, routes, context)
        try:
            return self.primary.classify_intent(text, routes, context)
        except Exception:
            return self.fallback.classify_intent(text, routes, context)

    def summarize(self, texts: list[str]) -> str:
        if not self.primary_enabled:
            return self.fallback.summarize(texts)
        try:
            return self.primary.summarize(texts)
        except Exception:
            return self.fallback.summarize(texts)

    def compose_answer(self, question: str, facts: list[str]) -> str:
        if not self.primary_enabled:
            return self.fallback.compose_answer(question, facts)
        try:
            return self.primary.compose_answer(question, facts)
        except Exception:
            return self.fallback.compose_answer(question, facts)
