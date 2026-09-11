"""HTTP 服务层：托管客服工作台并把调度引擎暴露为对话 API。

启动: uvicorn app.api.server:app --port 8000
调用: curl -X POST http://127.0.0.1:8000/chat \
        -H "Content-Type: application/json" \
        -d '{"message": "我想退款", "session_id": "s1"}'

同一 session_id 的请求会自动带上历史记忆；不传 session_id 则新建会话。

LLM 配置：默认接入 DeepSeek（OpenAI 兼容协议）作为兜底模型——pipeline 模式下
规则优先路由，仅在规则未命中时由 DeepSeek 做意图分类，FAQ 答案组织与长期摘要
同样走真实模型；调用失败自动降级为 Mock 规则实现，服务不被外部依赖击穿。
可用环境变量 DEEPSEEK_API_KEY / LLM_BASE_URL / LLM_MODEL / AGENT_MODE 覆盖默认配置。
"""
from __future__ import annotations

import base64
import binascii
import calendar
from datetime import date, datetime, timedelta
import json
import os
import pathlib
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..core.llm import OpenAICompatLLM, ResilientLLM
from ..core.models import Message
from ..engine.scheduler import Scheduler
from ..support.store import customer_store, ticket_store


ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "app" / "web"
UPLOAD_DIR = ROOT / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _load_local_env(path: pathlib.Path) -> None:
    """读取简单 KEY=VALUE 配置；系统环境变量始终拥有更高优先级。"""
    if not path.exists():
        return
    for raw_line in path.read_text("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env(ROOT / ".env")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")
AGENT_MODE = os.getenv("AGENT_MODE", "pipeline")
LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen2.5:7b")
LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "ollama")
LOCAL_LLM_AUTO_SUGGEST = os.getenv("LOCAL_LLM_AUTO_SUGGEST", "true").lower() == "true"
AI_SETTINGS_PATH = ROOT / "data" / "settings" / "ai.json"
AI_SETTINGS_DEFAULTS = {"deepseek_enabled": True, "suggestion_provider": "local"}
CUSTOMER_TAGS = {
    "high_risk": "高风险用户", "violation": "有违规用户", "trusted": "信誉良好用户",
    "vip": "VIP用户", "enterprise": "企业客户",
}


def _load_ai_settings() -> dict:
    try:
        saved = json.loads(AI_SETTINGS_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    settings = {**AI_SETTINGS_DEFAULTS, **saved}
    if settings.get("suggestion_provider") not in {"local", "deepseek"}:
        settings["suggestion_provider"] = "local"
    settings["deepseek_enabled"] = bool(settings.get("deepseek_enabled", True))
    return settings


_ai_settings_lock = threading.Lock()
_ai_settings = _load_ai_settings()


def _save_ai_settings(settings: dict) -> None:
    AI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = AI_SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(AI_SETTINGS_PATH)

AGENT_PRESETS = [
    {"id": "auto", "name": "智能调度", "description": "自动判断需求并分配合适的客服 Agent"},
    {"id": "order", "name": "订单售后", "description": "订单查询、物流跟踪、退款与退货"},
    {"id": "faq", "name": "政策顾问", "description": "规则、发票、会员与售后政策解答"},
    {"id": "human", "name": "人工服务", "description": "投诉受理并创建人工跟进工单"},
]

# 预回答来源 Agent 的中文名（与客户端 agent 回复链路保持同一套命名）
AGENT_DISPLAY_NAMES = {
    "order": "订单售后", "faq": "政策顾问", "human": "人工服务",
    "support": "排队托管 Agent", "clarify": "智能客服",
    "orchestrator": "智能调度",
}

app = FastAPI(title="Customer Service Agent API", version="0.1.0")
# 允许静态页面（如本机 Tomcat 上的聊天页）跨域调用对话接口
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 演示环境放开；生产应收敛为具体前端域名
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
deepseek_llm = OpenAICompatLLM(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)
llm = ResilientLLM(deepseek_llm)
llm.set_primary_enabled(_ai_settings["deepseek_enabled"])
local_llm = OpenAICompatLLM(base_url=LOCAL_LLM_BASE_URL, api_key=LOCAL_LLM_API_KEY,
                            model=LOCAL_LLM_MODEL, timeout=20)
scheduler = Scheduler(llm=llm, mode=AGENT_MODE)
_presence: dict[str, dict[str, float]] = {}
_presence_lock = threading.Lock()


def _message_dict(item: Message) -> dict:
    return {
        "id": item.meta.get("id") or f"m-{int(item.ts * 1_000_000)}",
        "role": item.role, "content": item.content, "timestamp": item.ts,
        "name": item.name, "meta": item.meta,
    }


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str = "anonymous"
    agent: str = "auto"
    attachments: list[dict] = []
    reply_to: dict | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    agent: str
    route_reason: str
    tools_used: list[str]
    slots: dict
    turns: int
    has_summary: bool
    support_status: str | None = None
    ticket_id: str | None = None
    assigned_to: str | None = None


class ClaimRequest(BaseModel):
    operator: str = "客服 001"


class HumanReplyRequest(BaseModel):
    message: str
    operator: str = "客服 001"
    attachments: list[dict] = []
    reply_to: dict | None = None


class CustomerProfileRequest(BaseModel):
    note: str = ""
    tags: list[str] = []


class UploadRequest(BaseModel):
    name: str
    mime_type: str
    data: str


class TypingRequest(BaseModel):
    actor: str
    typing: bool = True


class SuggestRequest(BaseModel):
    instruction: str = ""


class AISettingsRequest(BaseModel):
    deepseek_enabled: bool
    suggestion_provider: str


def _ai_settings_response() -> dict:
    with _ai_settings_lock:
        settings = dict(_ai_settings)
    effective_provider = (settings["suggestion_provider"]
                          if settings["deepseek_enabled"] else "local")
    return {
        **settings,
        "effective_suggestion_provider": effective_provider,
        "deepseek_api_configured": bool(LLM_API_KEY),
        "providers": [
            {"id": "local", "name": "本地模型", "model": LOCAL_LLM_MODEL,
             "description": "通过本机 Ollama 生成，不发送到外部服务"},
            {"id": "deepseek", "name": "DeepSeek Agent", "model": LLM_MODEL,
             "description": "复用客户端同一套 Agent 回复链路（记忆/路由/工具）生成预回答"},
        ],
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_index():
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/health")
def health():
    ai_settings = _ai_settings_response()
    return {
        "status": "ok", "llm": llm.name, "model": LLM_MODEL, "mode": scheduler.mode,
        "api_key_configured": bool(LLM_API_KEY),
        "deepseek_enabled": ai_settings["deepseek_enabled"],
        "local_llm": {"model": LOCAL_LLM_MODEL, "base_url": LOCAL_LLM_BASE_URL,
                      "auto_suggest": LOCAL_LLM_AUTO_SUGGEST,
                      "suggestion_provider": ai_settings["effective_suggestion_provider"]},
    }


@app.get("/config")
def config():
    return {"model": LLM_MODEL, "mode": scheduler.mode, "agents": AGENT_PRESETS,
            "deepseek_enabled": _ai_settings_response()["deepseek_enabled"]}


@app.get("/admin/api/ai-settings")
def get_ai_settings():
    return _ai_settings_response()


@app.put("/admin/api/ai-settings")
def update_ai_settings(req: AISettingsRequest):
    if req.suggestion_provider not in {"local", "deepseek"}:
        raise HTTPException(status_code=422, detail="未知的预回答模型")
    updated = {
        "deepseek_enabled": req.deepseek_enabled,
        "suggestion_provider": req.suggestion_provider,
    }
    with _ai_settings_lock:
        _ai_settings.update(updated)
        _save_ai_settings(_ai_settings)
    llm.set_primary_enabled(req.deepseek_enabled)
    return _ai_settings_response()


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    session = scheduler.store.get_or_create(session_id)
    support = session.state.get("support") or {}
    return {
        "session_id": session.session_id,
        "messages": [_message_dict(item) for item in session.messages],
        "slots": session.slots,
        "turns": session.turn_count,
        "summary": session.summary,
        "support_status": support.get("status"),
        "ticket_id": support.get("ticket_id"),
        "assigned_to": support.get("assigned_to"),
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    message = req.message.strip()
    if not message and not req.attachments:
        raise HTTPException(status_code=422, detail="消息不能为空")
    message = message or "[图片]"
    preset_ids = {item["id"] for item in AGENT_PRESETS}
    if req.agent not in preset_ids:
        raise HTTPException(status_code=422, detail="未知的 Agent 预设")
    session_id = req.session_id or f"web-{uuid.uuid4().hex[:8]}"
    session = scheduler.store.get_or_create(session_id, req.user_id)
    support = session.state.get("support") or {}
    if support.get("status") == "active":
        session.turn_count += 1
        session.messages.append(Message(
            role="user", content=message,
            meta={"id": f"m-{uuid.uuid4().hex[:12]}", "attachments": req.attachments,
                  "reply_to": req.reply_to}))
        scheduler.store.save(session)
        return ChatResponse(
            session_id=session_id, reply="", agent="human",
            route_reason="消息已进入人工客服会话", tools_used=[], slots=session.slots,
            turns=session.turn_count, has_summary=bool(session.summary),
            support_status="active", ticket_id=support.get("ticket_id"),
            assigned_to=support.get("assigned_to"),
        )
    preferred_agent = None if req.agent == "auto" else req.agent
    reply = scheduler.process(session_id, message, req.user_id, preferred_agent=preferred_agent)
    session = scheduler.store.get_or_create(session_id, req.user_id)
    if session.messages:
        for item in reversed(session.messages):
            if item.role == "user":
                item.meta.update({"id": item.meta.get("id") or f"m-{uuid.uuid4().hex[:12]}",
                                  "attachments": req.attachments, "reply_to": req.reply_to})
                break
        scheduler.store.save(session)
    support = session.state.get("support") or {}
    return ChatResponse(
        session_id=session_id, reply=reply.text, agent=reply.agent,
        route_reason=reply.route_reason, tools_used=reply.tools_used, slots=reply.slots,
        turns=reply.turns, has_summary=reply.has_summary,
        support_status=support.get("status"), ticket_id=support.get("ticket_id"),
        assigned_to=support.get("assigned_to"))


def _ticket_or_404(ticket_id: str) -> dict:
    ticket = ticket_store.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ticket


def _session_for_ticket(ticket: dict):
    return scheduler.store.get_or_create(ticket["session_id"], ticket.get("user_id", "anonymous"))


@app.get("/admin/api/tickets")
def list_tickets(status: str | None = None, q: str = ""):
    allowed = {None, "waiting", "active", "closed"}
    if status not in allowed:
        raise HTTPException(status_code=422, detail="未知的工单状态")
    items = []
    for ticket in ticket_store.list(status):
        session = _session_for_ticket(ticket)
        profile = customer_store.get(ticket.get("user_id", "anonymous"))
        last_message = session.messages[-1].content if session.messages else ticket["description"]
        haystack = " ".join((ticket.get("ticket_id", ""), ticket.get("title", ""),
                             ticket.get("user_id", ""), last_message, profile.get("note", ""),
                             " ".join(profile.get("tags", [])))).lower()
        if q.strip() and q.strip().lower() not in haystack:
            continue
        items.append({**ticket, "last_message": last_message, "turns": session.turn_count,
                      "slots": session.slots, "customer": profile})
    return {"tickets": items, "counts": {
        key: len(ticket_store.list(key)) for key in ("waiting", "active", "closed")
    }}


@app.get("/admin/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    ticket = _ticket_or_404(ticket_id)
    session = _session_for_ticket(ticket)
    return {"ticket": ticket,
            "customer": customer_store.get(ticket.get("user_id", "anonymous")),
            "session": {
        "session_id": session.session_id,
        "messages": [_message_dict(item) for item in session.messages],
        "slots": session.slots,
        "summary": session.summary,
        "turns": session.turn_count,
    }}


@app.post("/admin/api/tickets/{ticket_id}/claim")
def claim_ticket(ticket_id: str, req: ClaimRequest):
    ticket = _ticket_or_404(ticket_id)
    if ticket["status"] == "closed":
        raise HTTPException(status_code=409, detail="已关闭的工单不可接单")
    operator = req.operator.strip() or "客服 001"
    ticket = ticket_store.update(ticket_id, status="active", assigned_to=operator,
                                 claimed_at=time.time())
    session = _session_for_ticket(ticket)
    session.state["support"] = {"ticket_id": ticket_id, "status": "active",
                                "assigned_to": operator}
    session.messages.append(Message(
        role="assistant", name="human", content=f"人工客服 {operator} 已接入本次会话。",
        meta={"source": "human", "event": "claimed", "ticket_id": ticket_id}))
    scheduler.store.save(session)
    return {"ticket": ticket}


@app.post("/admin/api/tickets/{ticket_id}/reply")
def human_reply(ticket_id: str, req: HumanReplyRequest):
    ticket = _ticket_or_404(ticket_id)
    message = req.message.strip()
    if not message and not req.attachments:
        raise HTTPException(status_code=422, detail="回复内容不能为空")
    message = message or "[图片]"
    if ticket["status"] != "active":
        raise HTTPException(status_code=409, detail="请先接单再回复")
    session = _session_for_ticket(ticket)
    session.messages.append(Message(
        role="assistant", name="human", content=message,
        meta={"id": f"m-{uuid.uuid4().hex[:12]}", "source": "human",
              "operator": req.operator, "ticket_id": ticket_id,
              "attachments": req.attachments, "reply_to": req.reply_to}))
    scheduler.store.save(session)
    ticket = ticket_store.update(ticket_id, last_reply=message)
    return {"ok": True, "ticket": ticket}


@app.post("/admin/api/tickets/{ticket_id}/close")
def close_ticket(ticket_id: str, req: ClaimRequest):
    ticket = _ticket_or_404(ticket_id)
    ticket = ticket_store.update(ticket_id, status="closed", closed_at=time.time(),
                                 closed_by=req.operator.strip() or "客服 001")
    session = _session_for_ticket(ticket)
    session.state["support"] = {"ticket_id": ticket_id, "status": "closed",
                                "assigned_to": ticket.get("assigned_to")}
    session.messages.append(Message(
        role="assistant", name="human", content="本次人工服务已结束，后续消息将由智能客服继续为您处理。",
        meta={"source": "human", "event": "closed", "ticket_id": ticket_id}))
    scheduler.store.save(session)
    return {"ticket": ticket}


@app.post("/api/uploads")
def upload_image(req: UploadRequest):
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
               "image/webp": ".webp"}
    suffix = allowed.get(req.mime_type)
    if suffix is None:
        raise HTTPException(status_code=415, detail="仅支持 JPG、PNG、GIF、WebP 图片")
    try:
        content = base64.b64decode(req.data.split(",", 1)[-1], validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=422, detail="图片数据无效")
    if not content or len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片大小需在 5 MB 以内")
    valid = {
        ".jpg": content.startswith(b"\xff\xd8\xff"),
        ".png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        ".gif": content.startswith((b"GIF87a", b"GIF89a")),
        ".webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if not valid[suffix]:
        raise HTTPException(status_code=422, detail="图片内容与格式不匹配")
    filename = f"{uuid.uuid4().hex}{suffix}"
    (UPLOAD_DIR / filename).write_bytes(content)
    return {"name": pathlib.Path(req.name).name, "mime_type": req.mime_type,
            "url": f"/uploads/{filename}", "size": len(content)}


@app.post("/presence/{session_id}")
def set_presence(session_id: str, req: TypingRequest):
    if req.actor not in {"customer", "operator"}:
        raise HTTPException(status_code=422, detail="未知输入角色")
    with _presence_lock:
        actors = _presence.setdefault(session_id, {})
        if req.typing:
            actors[req.actor] = time.time() + 15
        else:
            actors.pop(req.actor, None)
    return {"ok": True}


@app.get("/presence/{session_id}")
def get_presence(session_id: str):
    now = time.time()
    with _presence_lock:
        actors = _presence.get(session_id, {})
        active = {key: expires > now for key, expires in actors.items()}
    return {"customer_typing": active.get("customer", False),
            "operator_typing": active.get("operator", False)}


@app.get("/admin/api/dashboard")
def dashboard():
    tickets = ticket_store.list()
    now = datetime.now()
    today = now.date()
    today_key = (now.year, now.timetuple().tm_yday)
    completed = [item for item in tickets if item.get("status") == "closed"]
    today_completed = []
    for item in completed:
        closed = time.localtime(item.get("closed_at", 0))
        if (closed.tm_year, closed.tm_yday) == today_key:
            today_completed.append(item)
    durations = [item["closed_at"] - item["created_at"] for item in completed
                 if item.get("closed_at") and item.get("created_at")]
    profiles = customer_store.list()

    week_dates = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    weekly = {day: {"created": 0, "completed": 0} for day in week_dates}
    hourly = [0] * 24
    month_days = calendar.monthrange(now.year, now.month)[1]
    monthly = {date(now.year, now.month, day): 0 for day in range(1, month_days + 1)}
    for item in tickets:
        if item.get("created_at"):
            created = datetime.fromtimestamp(item["created_at"])
            created_day = created.date()
            hourly[created.hour] += 1
            if created_day in weekly:
                weekly[created_day]["created"] += 1
            if created_day in monthly:
                monthly[created_day] += 1
        if item.get("closed_at"):
            closed_day = datetime.fromtimestamp(item["closed_at"]).date()
            if closed_day in weekly:
                weekly[closed_day]["completed"] += 1

    weekly_series = [
        {"date": day.isoformat(), "label": day.strftime("%m/%d"), **weekly[day]}
        for day in week_dates
    ]
    hourly_series = [{"hour": hour, "label": f"{hour:02d}:00", "count": count}
                     for hour, count in enumerate(hourly)]
    monthly_series = [
        {"date": day.isoformat(), "label": f"{day.day}日", "count": monthly[day]}
        for day in monthly
    ]
    weekly_peak = max(weekly_series, key=lambda item: item["created"])
    hourly_peak = max(hourly_series, key=lambda item: item["count"])
    monthly_peak = max(monthly_series, key=lambda item: item["count"])
    return {
        "waiting": sum(item.get("status") == "waiting" for item in tickets),
        "active": sum(item.get("status") == "active" for item in tickets),
        "completed": len(completed), "today_completed": len(today_completed),
        "total": len(tickets),
        "completion_rate": round(len(completed) / len(tickets) * 100, 1) if tickets else 0,
        "average_handle_minutes": round(sum(durations) / len(durations) / 60, 1) if durations else 0,
        "tag_counts": {key: sum(key in profile.get("tags", []) for profile in profiles)
                       for key in CUSTOMER_TAGS},
        "tag_labels": CUSTOMER_TAGS,
        "weekly_series": weekly_series,
        "hourly_series": hourly_series,
        "monthly_series": monthly_series,
        "peaks": {
            "weekly_day": {"date": weekly_peak["date"], "label": weekly_peak["label"],
                           "count": weekly_peak["created"]},
            "hour_range": {"hour": hourly_peak["hour"],
                           "label": f"{hourly_peak['hour']:02d}:00-{(hourly_peak['hour'] + 1) % 24:02d}:00",
                           "count": hourly_peak["count"]},
            "monthly_day": {"date": monthly_peak["date"], "label": monthly_peak["label"],
                            "count": monthly_peak["count"]},
        },
    }


@app.put("/admin/api/customers/{user_id}")
def update_customer(user_id: str, req: CustomerProfileRequest):
    if set(req.tags) - set(CUSTOMER_TAGS):
        raise HTTPException(status_code=422, detail="包含未知用户标签")
    return customer_store.save(user_id, req.note.strip()[:1000], list(dict.fromkeys(req.tags)))


@app.post("/admin/api/tickets/{ticket_id}/suggest")
def suggest_reply(ticket_id: str, req: SuggestRequest):
    ticket = _ticket_or_404(ticket_id)
    session = _session_for_ticket(ticket)
    settings = _ai_settings_response()
    provider = settings["effective_suggestion_provider"]

    # DeepSeek 预回答：直接复用客户端 /chat 的同一套 Agent 回复链路
    # （记忆 + 路由 + 专家/托管 Agent + 工具），以不落库的预览方式生成草稿，
    # 因此后台看到的建议与客户实际会收到的 Agent 回复完全一致，且不会污染真实会话。
    if provider == "deepseek":
        if not LLM_API_KEY:
            raise HTTPException(status_code=503, detail="尚未配置 DeepSeek API Key")
        last_customer = next(
            (item.content for item in reversed(session.messages) if item.role == "user"), "")
        if not last_customer:
            raise HTTPException(status_code=422, detail="暂无可生成预回答的客户消息")
        try:
            preview = scheduler.preview_reply(
                ticket["session_id"], last_customer, ticket.get("user_id", "anonymous"))
        except Exception:
            raise HTTPException(status_code=503,
                                detail="DeepSeek Agent 暂不可用，请检查配置或稍后重试")
        suggestion = (preview.text or "").strip()
        if not suggestion:
            raise HTTPException(status_code=503, detail="DeepSeek Agent 未返回内容")
        return {
            "suggestion": suggestion, "model": LLM_MODEL, "provider": "deepseek",
            "provider_name": "DeepSeek Agent", "agent": preview.agent,
            "agent_name": AGENT_DISPLAY_NAMES.get(preview.agent, preview.agent),
            "route_reason": preview.route_reason, "tools_used": preview.tools_used,
        }

    # 本地模型（Ollama）：沿用坐席助手提示词，直接生成一条草稿
    profile = customer_store.get(ticket.get("user_id", "anonymous"))
    dialogue = "\n".join(
        f"{'客户' if item.role == 'user' else '客服'}：{item.content}"
        for item in session.messages[-10:]
    )
    prompt = (
        "你是客服坐席的 AI 助手。根据上下文生成一条可直接发送的预回答。"
        "不得编造订单状态、退款结果或赔付承诺，只输出回复正文。\n"
        f"客户标签：{[CUSTOMER_TAGS.get(tag, tag) for tag in profile.get('tags', [])]}\n"
        f"客服备注：{profile.get('note') or '无'}\n已知槽位：{session.slots}\n"
        f"额外要求：{req.instruction or '无'}\n对话：\n{dialogue}"
    )
    try:
        response = local_llm.chat([
            {"role": "system", "content": "你是客服工作站中的回复建议助手。"},
            {"role": "user", "content": prompt},
        ], temperature=0.3)
    except Exception:
        raise HTTPException(
            status_code=503,
            detail=f"本地模型暂不可用，请启动 Ollama 并确认已安装 {LOCAL_LLM_MODEL}")
    suggestion = (response.content or "").strip()
    if not suggestion:
        raise HTTPException(status_code=503, detail="本地模型未返回内容")
    return {"suggestion": suggestion, "model": LOCAL_LLM_MODEL, "provider": "local",
            "provider_name": "本地模型"}
