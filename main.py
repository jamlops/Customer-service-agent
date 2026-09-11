"""客服 AI Agent 演示入口。

用法:
  python main.py                 # 运行内置多轮场景演示（离线、确定性）
  python main.py --mode llm          # LLM 全权语义路由
  python main.py --mode orchestrator # function calling 多 Agent 协作
  python main.py --chat              # 交互式对话
  python main.py --chat --session my-session-id
  python main.py --llm openai        # 使用 OpenAI 兼容服务（环境变量 OPENAI_BASE_URL/OPENAI_API_KEY/OPENAI_MODEL）
  python main.py --trace             # 额外打印每轮执行轨迹

三种运行模式:
  pipeline     规则优先路由 + 专家 Agent（默认）
  llm          LLM 全权语义路由 + 专家 Agent
  orchestrator 总控 Agent 用 function calling 统一调度工具与专家 Agent
"""
from __future__ import annotations

import argparse
import sys
import uuid

try:  # Windows 控制台中文/emoji 输出兜底
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.core.llm import MockLLM, OpenAICompatLLM
from app.engine.scheduler import Scheduler

DEMO_TURNS = [
    "你好，我买的耳机有问题，想申请退款",
    "订单号是 SO20260905001",
    "确认，提交退款",
    "对了，帮我查下这单的物流到哪了",
    "你们支持7天无理由退货吗？",
    "服务态度太差了，我要投诉，转人工",
]


def show(reply) -> None:
    print(f"🧭 调度 ：路由 → {reply.agent} ｜ {reply.route_reason}")
    if reply.tools_used:
        print(f"🧰 工具 ：{'、'.join(reply.tools_used)}")
    slots = "、".join(f"{k}={v}" for k, v in reply.slots.items()) or "（空）"
    summary = "已生成" if reply.has_summary else "未生成"
    print(f"📌 记忆 ：第 {reply.turns} 轮 ｜ 槽位：{slots} ｜ 长期摘要：{summary}")
    print(f"💬 客服 ：{reply.text}")
    if "--trace" in sys.argv:
        for e in reply.trace:
            print(f"    · {e}")


def run_demo(scheduler: Scheduler, mode: str) -> None:
    session_id = f"demo-{uuid.uuid4().hex[:6]}"
    print(f"—— 多轮场景演示（模式 {mode} ｜ 会话 {session_id}，每次运行均为全新会话）——")
    for i, text in enumerate(DEMO_TURNS, 1):
        print(f"\n{'=' * 66}\n【第 {i} 轮】👤 用户：{text}")
        show(scheduler.process(session_id, text))
    print(f"\n—— 演示结束，会话已持久化到 data/sessions/{session_id}.json ——")


def run_chat(scheduler: Scheduler, session_id: str) -> None:
    print(f"—— 交互式客服（会话 {session_id}）——")
    print("输入内容与客服对话；exit / quit / 退出 结束。\n")
    while True:
        try:
            text = input("👤 你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit") or text == "退出":
            break
        show(scheduler.process(session_id, text))
    print(f"会话已持久化到 data/sessions/{session_id}.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="客服 AI Agent 演示")
    parser.add_argument("--chat", action="store_true", help="交互式对话（默认运行内置演示）")
    parser.add_argument("--session", default=None, help="指定会话 ID（缺省自动生成）")
    parser.add_argument("--llm", choices=["mock", "openai"], default="mock", help="LLM 实现")
    parser.add_argument("--mode", choices=["pipeline", "llm", "orchestrator"],
                        default="pipeline", help="调度模式")
    parser.add_argument("--trace", action="store_true", help="打印每轮执行轨迹")
    args = parser.parse_args()

    llm = OpenAICompatLLM() if args.llm == "openai" else MockLLM()
    scheduler = Scheduler(llm=llm, mode=args.mode)
    session_id = args.session or f"cli-{uuid.uuid4().hex[:6]}"

    if args.chat:
        run_chat(scheduler, session_id)
    else:
        run_demo(scheduler, args.mode)


if __name__ == "__main__":
    main()
