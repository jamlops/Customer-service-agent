"""工具执行器：三种执行形态——

1. call()          单次调用：统一做注册校验、确认门禁、异常兜底、轨迹记录；
2. execute_calls() 一批独立调用的并行执行（线程池扇出）；
3. run_plan()      声明式 DAG 编排：步骤间显式声明依赖，同层并行、跨层串行，
                   支持 slot:xxx（取槽位记忆）与 step:xxx.field（取上游结果）两种参数引用。
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field

from ..core.models import ToolCall, ToolResult
from .base import ToolRegistry


@dataclass
class PlanStep:
    id: str
    tool: str
    args: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)   # 依赖的前置步骤 id


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep]


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, max_workers: int = 4):
        self.registry = registry
        self.max_workers = max_workers

    # ---------- 单次调用 ----------
    def call(self, ctx, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        ctx.log("tool_start", tool=call.name, args=call.args)
        if tool is None:
            return ToolResult.fail(call.name, f"未注册的工具: {call.name}")
        if tool.spec.requires_confirm and not (ctx.state.get("user_confirmed") or call.args.get("_confirmed")):
            ctx.log("tool_blocked", tool=call.name, reason="needs_user_confirmation")
            return ToolResult.fail(call.name, "needs_confirmation", message="该操作需要用户确认后才能执行")
        try:
            args = dict(call.args)
            args.pop("_confirmed", None)
            result = tool.run(ctx, **args)
        except TypeError as e:
            result = ToolResult.fail(call.name, f"参数错误: {e}")
        except Exception as e:  # 工具异常不逃逸，转化为失败结果
            result = ToolResult.fail(call.name, f"执行异常: {e}")
        ctx.log("tool_end", tool=call.name, success=result.success)
        return result

    # ---------- 一批独立调用 ----------
    def execute_calls(self, ctx, calls: list[ToolCall], parallel: bool = True) -> list[ToolResult]:
        if len(calls) > 1 and parallel:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                return list(pool.map(lambda c: self.call(ctx, c), calls))
        return [self.call(ctx, c) for c in calls]

    # ---------- DAG 计划编排 ----------
    def run_plan(self, ctx, plan: Plan) -> dict[str, ToolResult]:
        """按依赖分层执行：同一波次内的步骤并行跑，依赖未满足的留到下一波次。"""
        results: dict[str, ToolResult] = {}
        pending: dict[str, PlanStep] = {s.id: s for s in plan.steps}
        done: set[str] = set()
        ctx.log("plan_start", goal=plan.goal, steps=list(pending))
        while pending:
            wave = [s for s in pending.values() if all(d in done for d in s.depends_on)]
            if not wave:  # 剩余步骤的上游全部失败，或声明了循环依赖
                for s in pending.values():
                    results[s.id] = ToolResult.fail(s.tool, "依赖失败或循环依赖", message=f"步骤 {s.id} 已跳过")
                    ctx.log("plan_skip", step=s.id)
                break
            for s in wave:  # 进场前解析参数引用（此时上游结果已就绪）
                s.args = {k: self._resolve(v, ctx, results) for k, v in s.args.items()}
            calls = [ToolCall(id=s.id, name=s.tool, args=s.args) for s in wave]
            if len(calls) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                    got = list(pool.map(lambda c: self.call(ctx, c), calls))
            else:
                got = [self.call(ctx, calls[0])]
            for s, r in zip(wave, got):
                results[s.id] = r
                if r.success:
                    done.add(s.id)
                else:
                    ctx.log("plan_step_failed", step=s.id, error=r.data.get("error"))
            for s in wave:
                pending.pop(s.id, None)
        ctx.log("plan_end", ok=[k for k, v in results.items() if v.success])
        return results

    def _resolve(self, value, ctx, results: dict[str, ToolResult]):
        """解析参数引用：'slot:order_id' / 'step:step_id.field'，其余原样透传。"""
        if isinstance(value, str) and value.startswith("slot:"):
            return ctx.memory.get_slot(value[5:])
        if isinstance(value, str) and value.startswith("step:"):
            step_id, _, path = value[5:].partition(".")
            node = results.get(step_id).data if results.get(step_id) else {}
            for part in path.split("."):
                if isinstance(node, dict):
                    node = node.get(part)
                else:
                    return None
            return node
        return value
