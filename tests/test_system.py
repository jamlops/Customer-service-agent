"""全链路单元测试：python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import threading
import unittest

from app.core.context import RunContext
from app.core.llm import MockLLM, ResilientLLM
from app.core.models import LLMResponse, SessionData, ToolCall, ToolResult
from app.engine.scheduler import Scheduler
from app.memory.manager import MemoryManager
from app.memory.store import FileSessionStore
from app.tools.base import BaseTool, ToolRegistry, ToolSpec
from app.tools.builtin import register_builtin_tools
from app.tools.executor import Plan, PlanStep, ToolExecutor


class ExplodingLLM(MockLLM):
    def chat(self, messages, tools=None, temperature=0.3):
        raise AssertionError("primary model should not be called")


class TestLLMSwitch(unittest.TestCase):
    def test_disabled_primary_uses_fallback_without_calling_primary(self):
        model = ResilientLLM(ExplodingLLM(), MockLLM())
        model.set_primary_enabled(False)
        response = model.chat([{"role": "user", "content": "你好"}])
        self.assertTrue(response.content.startswith("[mock]"))


def make_ctx(session: SessionData | None = None):
    session = session or SessionData("t")
    registry = register_builtin_tools(ToolRegistry())
    executor = ToolExecutor(registry)
    memory = MemoryManager(session, MockLLM())
    return RunContext(session, MockLLM(), memory, registry, executor)


# ---------------- 记忆机制 ----------------
class TestMemory(unittest.TestCase):
    def test_slot_extraction(self):
        ctx = make_ctx()
        ctx.memory.on_user_turn(ctx, "我的手机号 13812345678，订单 SO20260905001 出问题了")
        self.assertEqual(ctx.memory.get_slot("order_id"), "SO20260905001")
        self.assertEqual(ctx.memory.get_slot("phone"), "13812345678")

    def test_slot_persists_across_turns(self):
        ctx = make_ctx()
        ctx.memory.on_user_turn(ctx, "订单号是 SO20260905001")
        ctx.memory.on_user_turn(ctx, "帮我查下物流")
        self.assertEqual(ctx.memory.get_slot("order_id"), "SO20260905001")

    def test_summary_compression_on_overflow(self):
        session = SessionData("t")
        ctx = make_ctx(session)
        for i in range(12):  # 24 条消息 > 窗口 10，触发淘汰 + 摘要
            ctx.memory.on_user_turn(ctx, f"问题{i}")
            ctx.memory.on_assistant_turn(ctx, f"回答{i}", [])
        self.assertTrue(session.summary)
        self.assertLessEqual(len(session.messages), 10)
        self.assertLess(len(session.overflow), 3)  # 仅允许留存未达摘要阈值的少量积压


# ---------------- 工具编排 ----------------
class EchoTool(BaseTool):
    spec = ToolSpec("echo", "回显", {"type": "object", "properties": {"v": {}}})

    def __init__(self):
        self.seen_threads = set()

    def run(self, ctx, v=None):
        self.seen_threads.add(threading.current_thread().name)
        return ToolResult(self.spec.name, True, {"out": v})


class BarrierTool(BaseTool):
    """两个实例必须同时在跑才能通过 Barrier——用于证明并行执行。"""
    spec = ToolSpec("barrier", "barrier", {"type": "object", "properties": {}})

    def __init__(self, barrier: threading.Barrier):
        self.barrier = barrier

    def run(self, ctx):
        self.barrier.wait(timeout=3)  # 串行执行会超时抛错 -> 结果失败
        return ToolResult(self.spec.name, True, {"ok": True})


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_plan_resolves_slot_and_step_refs(self):
        echo = EchoTool()
        registry = ToolRegistry().register(echo)
        executor = ToolExecutor(registry)
        self.ctx.memory.set_slot(self.ctx, "order_id", "SO999")
        plan = Plan("t", [
            PlanStep("a", "echo", {"v": "1"}),
            PlanStep("b", "echo", {"v": "step:a.out"}, depends_on=["a"]),
            PlanStep("c", "echo", {"v": "slot:order_id"}),
        ])
        r = executor.run_plan(self.ctx, plan)
        self.assertEqual(r["a"].data["out"], "1")
        self.assertEqual(r["b"].data["out"], "1")      # 引用了上游 a 的输出
        self.assertEqual(r["c"].data["out"], "SO999")  # 引用了槽位记忆

    def test_plan_runs_independent_steps_in_parallel(self):
        barrier = threading.Barrier(2)
        registry = ToolRegistry().register(BarrierTool(barrier)).register(BarrierTool(barrier))
        executor = ToolExecutor(registry, max_workers=2)
        plan = Plan("t", [PlanStep("x", "barrier"), PlanStep("y", "barrier")])
        r = executor.run_plan(self.ctx, plan)
        self.assertTrue(r["x"].success and r["y"].success)  # 串行则必有一方超时失败

    def test_failed_dependency_skips_downstream(self):
        registry = ToolRegistry().register(EchoTool())
        executor = ToolExecutor(registry)
        plan = Plan("t", [
            PlanStep("boom", "no_such_tool"),
            PlanStep("child", "echo", {"v": "1"}, depends_on=["boom"]),
        ])
        r = executor.run_plan(self.ctx, plan)
        self.assertFalse(r["boom"].success)
        self.assertFalse(r["child"].success)

    def test_confirmation_gate_blocks_sensitive_tool(self):
        r1 = self.ctx.executor.call(self.ctx, ToolCall("1", "submit_refund", {"order_id": "SO20260905001"}))
        self.assertFalse(r1.success)
        self.assertEqual(r1.data["error"], "needs_confirmation")
        self.ctx.state["user_confirmed"] = True
        r2 = self.ctx.executor.call(self.ctx, ToolCall("2", "submit_refund", {"order_id": "SO20260905001"}))
        self.assertTrue(r2.success)
        self.assertTrue(r2.data["refund_id"].startswith("RF"))


# ---------------- 路由 ----------------
class TestRouter(unittest.TestCase):
    def _route(self, text, state=None):
        ctx = make_ctx()
        ctx.session.state = state or {}
        from app.agents.router import Router
        from app.agents.faq_agent import FAQAgent
        from app.agents.order_agent import OrderAgent
        from app.agents.human_agent import HumanAgent
        router = Router({"order": OrderAgent(), "faq": FAQAgent(), "human": HumanAgent()})
        return router.route(ctx, text)

    def test_explicit_human_request_wins(self):
        d = self._route("我要投诉，转人工")
        self.assertEqual(d.route, "human")

    def test_policy_question_goes_to_faq(self):
        d = self._route("你们支持7天无理由退货吗")
        self.assertEqual(d.route, "faq")

    def test_refund_request_goes_to_order(self):
        d = self._route("我想申请退款")
        self.assertEqual(d.route, "order")

    def test_pending_resumes_previous_flow(self):
        d = self._route("好的，就这个单", state={"pending": {"agent": "order", "action": "refund", "stage": "await_order_id"}})
        self.assertEqual(d.route, "order")
        self.assertTrue(d.resumed)


# ---------------- 调度引擎端到端（多轮） ----------------
class TestSchedulerMultiTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scheduler = Scheduler(store=FileSessionStore(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_refund_flow_and_persistence(self):
        sid = "ut-1"
        r1 = self.scheduler.process(sid, "你好，我买的耳机有问题，想申请退款")
        self.assertEqual(r1.agent, "order")
        self.assertIn("订单号", r1.text)  # 缺订单号 -> 反问

        r2 = self.scheduler.process(sid, "订单号是 SO20260905001")
        self.assertIn("符合退款条件", r2.text)
        self.assertIn("确认", r2.text)  # 并行核查后请求确认
        self.assertTrue({"query_order", "query_logistics", "check_refund_eligibility"}
                        <= set(r2.tools_used))

        r3 = self.scheduler.process(sid, "确认，提交退款")
        self.assertIn("RF", r3.text)  # 确认门禁放行，拿到退款单号

        r4 = self.scheduler.process(sid, "你们支持7天无理由退货吗")
        self.assertEqual(r4.agent, "faq")  # 跨 Agent，但槽位仍可用
        self.assertIn("无理由", r4.text)

        r5 = self.scheduler.process(sid, "服务太差了，我要投诉，转人工")
        self.assertEqual(r5.agent, "human")
        self.assertIn("TK", r5.text)

        # 会话已持久化：槽位、记忆、轮次跨进程可恢复
        path = pathlib.Path(self.tmp) / "ut-1.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text("utf-8"))
        self.assertEqual(data["slots"]["order_id"], "SO20260905001")
        self.assertEqual(data["turn_count"], 5)
        self.assertEqual(len(data["messages"]), 10)

    def test_session_reloaded_from_store(self):
        sid = "ut-2"
        self.scheduler.process(sid, "订单号是 SO20260905001，帮我查下物流")
        scheduler2 = Scheduler(store=FileSessionStore(self.tmp))  # 模拟重启
        r = scheduler2.process(sid, "帮我申请退款")
        self.assertEqual(r.agent, "order")
        self.assertIn("符合退款条件", r.text)  # 槽位从磁盘恢复，无需再问订单号

    def test_clarify_when_unroutable(self):
        r = self.scheduler.process("ut-3", "嗯嗯")
        self.assertEqual(r.agent, "clarify")
        self.assertIn("转人工", r.text)

    def test_preferred_agent_bypasses_automatic_route(self):
        r = self.scheduler.process("ut-preset", "我要查一下规则", preferred_agent="order")
        self.assertEqual(r.agent, "order")
        self.assertIn("用户选择预设 Agent", r.route_reason)


# ---------------- LLM 全权路由（mode="llm"） ----------------
class TestLLMRouter(unittest.TestCase):
    def _route(self, text, state=None):
        from app.agents.faq_agent import FAQAgent
        from app.agents.human_agent import HumanAgent
        from app.agents.order_agent import OrderAgent
        from app.agents.router import Router
        ctx = make_ctx()
        ctx.session.state = state or {}
        router = Router({"order": OrderAgent(), "faq": FAQAgent(), "human": HumanAgent()}, mode="llm")
        return router.route(ctx, text)

    def test_policy_question_goes_to_faq(self):
        self.assertEqual(self._route("退货的条件是什么").route, "faq")

    def test_action_request_goes_to_order(self):
        self.assertEqual(self._route("我想申请退款").route, "order")

    def test_pending_continuation_decided_by_llm(self):
        d = self._route("订单号是 SO20260905001",
                        state={"pending": {"agent": "order", "action": "refund", "stage": "await_order_id"}})
        self.assertEqual(d.route, "order")
        self.assertIn("LLM", d.reason)  # 续接由 LLM 依据上下文判断，而非硬编码

    def test_human_escape(self):
        self.assertEqual(self._route("我要投诉，转人工").route, "human")


# ---------------- function calling 多 Agent 协作（mode="orchestrator"） ----------------
class ScriptedLLM(MockLLM):
    """按剧本回放的假 LLM：元素为 ToolCall（发起调用）或 str（最终答复）。"""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.i = 0

    def chat(self, messages, tools=None, temperature=0.3):
        item = self.script[self.i] if self.i < len(self.script) else "（剧本已耗尽）"
        self.i += 1
        if isinstance(item, ToolCall):
            return LLMResponse(tool_calls=[item])
        return LLMResponse(content=item)


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scheduler = Scheduler(store=FileSessionStore(self.tmp), mode="orchestrator")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_six_turn_delegation_flow(self):
        sid = "orch-1"
        r1 = self.scheduler.process(sid, "你好，我买的耳机有问题，想申请退款")
        self.assertEqual(r1.agent, "orchestrator")
        self.assertIn("订单号", r1.text)  # 委派订单专家后转达其反问

        r2 = self.scheduler.process(sid, "订单号是 SO20260905001")
        self.assertIn("符合退款条件", r2.text)
        self.assertIn("ask_order_agent", r2.tools_used)       # 委派函数
        self.assertIn("check_refund_eligibility", r2.tools_used)  # 专家内部并行编排

        r3 = self.scheduler.process(sid, "确认，提交退款")
        self.assertIn("RF", r3.text)

        r4 = self.scheduler.process(sid, "对了，帮我查下这单的物流到哪了")
        self.assertIn("已签收", r4.text)  # 槽位复用，无需再问订单号

        r5 = self.scheduler.process(sid, "你们支持7天无理由退货吗")
        self.assertIn("无理由", r5.text)
        self.assertIn("ask_policy_agent", r5.tools_used)

        r6 = self.scheduler.process(sid, "服务态度太差了，我要投诉，转人工")
        self.assertIn("TK", r6.text)
        self.assertIn("transfer_to_human", r6.tools_used)

        # 编排对话草稿已作为检查点持久化（断点续接能力的依据）
        data = json.loads((pathlib.Path(self.tmp) / "orch-1.json").read_text("utf-8"))
        self.assertIn("orch_messages", data["state"])

    def test_delegation_follows_agent_handoff(self):
        from app.agents.faq_agent import FAQAgent
        from app.agents.human_agent import HumanAgent
        from app.agents.order_agent import OrderAgent
        from app.agents.orchestrator import AgentDelegationTool
        sub = {"order": OrderAgent(), "faq": FAQAgent(), "human": HumanAgent()}
        tool = AgentDelegationTool("faq", sub["faq"], sub)
        result = tool.run(make_ctx(), "你们就是骗子吧，我要曝光你们")  # FAQ 未命中且情绪不佳
        self.assertTrue(result.success)
        self.assertEqual(result.data["agent"], "human")  # 自动接力移交到人工
        self.assertIn("TK", result.message)

    def test_orchestrator_level_confirmation_gate(self):
        # 总控直连敏感工具的场景：第一轮被确认门禁拦截，用户“确认”后第二轮放行重试
        llm = ScriptedLLM([
            ToolCall("c1", "submit_refund", {"order_id": "SO20260905001"}),
            "该操作需要您确认后才能执行，请回复“确认”。",
            ToolCall("c2", "submit_refund", {"order_id": "SO20260905001"}),
            "退款申请已为您提交。",
        ])
        tmp = tempfile.mkdtemp()
        try:
            scheduler = Scheduler(llm=llm, store=FileSessionStore(tmp), mode="orchestrator")
            r1 = scheduler.process("gate-1", "帮我直接提交订单 SO20260905001 的退款")
            self.assertTrue(any(e["event"] == "tool_blocked" and e["tool"] == "submit_refund"
                                for e in r1.trace))
            r2 = scheduler.process("gate-1", "确认")
            self.assertTrue(any(e["event"] == "tool_end" and e["tool"] == "submit_refund"
                                and e.get("success") for e in r2.trace))
            self.assertIn("已为您提交", r2.text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 预回答草稿：复用客户端 Agent 链路但不落库 ----------------
class TestSchedulerPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.scheduler = Scheduler(store=FileSessionStore(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_draft_executor_keeps_read_tools_only(self):
        executor = self.scheduler._draft_tool_executor()
        self.assertIsNone(executor.registry.get("create_ticket"))
        self.assertIsNone(executor.registry.get("submit_refund"))
        self.assertIsNotNone(executor.registry.get("query_order"))
        self.assertIsNotNone(executor.registry.get("search_knowledge"))

    def test_preview_runs_agent_pipeline_without_mutating_session(self):
        sid = "pv-1"
        self.scheduler.process(sid, "订单号是 SO20260905001，帮我查物流")
        path = pathlib.Path(self.tmp) / "pv-1.json"
        before = json.loads(path.read_text("utf-8"))

        preview = self.scheduler.preview_reply(sid, "再帮我看看退款资格")
        # 走的是与客户端相同的订单 Agent + 只读工具，并复用已记忆的订单号槽位
        self.assertEqual(preview.agent, "order")
        self.assertEqual(preview.slots.get("order_id"), "SO20260905001")
        self.assertIn("check_refund_eligibility", preview.tools_used)
        self.assertTrue(preview.text)

        # 真实会话未被写入草稿消息、轮次未增加、内容未变化
        after = json.loads(path.read_text("utf-8"))
        self.assertEqual(len(before["messages"]), len(after["messages"]))
        self.assertEqual(before["turn_count"], after["turn_count"])
        self.assertNotIn(preview.text, [m["content"] for m in after["messages"]])

    def test_preview_reroutes_human_to_support_and_creates_no_ticket(self):
        from app.support.store import ticket_store
        before_count = len(ticket_store.list())
        preview = self.scheduler.preview_reply("pv-human", "我要投诉，转人工")
        # 草稿模式不建单，改由托管 Agent 生成内容
        self.assertEqual(preview.agent, "support")
        self.assertTrue(preview.text)
        self.assertEqual(len(ticket_store.list()), before_count)
        # 预览会话不落盘
        self.assertFalse((pathlib.Path(self.tmp) / "pv-human.json").exists())


if __name__ == "__main__":
    unittest.main()
