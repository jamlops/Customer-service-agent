"""Web API 与静态工作台的契约测试。"""
from __future__ import annotations

import unittest
import uuid
import calendar
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException

from app.core.models import AgentReply, LLMResponse
from app.api.server import (
    ChatRequest, ClaimRequest, HumanReplyRequest, admin_index, chat, claim_ticket,
    close_ticket, config, dashboard, get_presence, get_session, get_ticket, human_reply,
    get_ai_settings, index, list_tickets, set_presence, suggest_reply, update_ai_settings,
    update_customer, upload_image, AISettingsRequest, CustomerProfileRequest,
    SuggestRequest, TypingRequest, UploadRequest,
)
from app.api import server


class TestWebAPI(unittest.TestCase):
    def test_index_and_config(self):
        page = index()
        self.assertEqual(page.path.name, "index.html")
        self.assertEqual(admin_index().path.name, "admin.html")

        data = config()
        self.assertEqual(data["model"], "deepseek-v4-flash")
        self.assertEqual({item["id"] for item in data["agents"]},
                         {"auto", "order", "faq", "human"})

    def test_chat_with_agent_preset(self):
        data = chat(ChatRequest(
            message="订单号是 SO20260905001，帮我查物流",
            session_id=f"api-preset-{uuid.uuid4().hex[:8]}",
            agent="order",
        ))
        self.assertEqual(data.agent, "order")
        self.assertIn("query_logistics", data.tools_used)
        self.assertEqual(data.slots["order_id"], "SO20260905001")

    def test_chat_rejects_empty_message_and_unknown_preset(self):
        with self.assertRaises(HTTPException) as empty:
            chat(ChatRequest(message=" "))
        self.assertEqual(empty.exception.status_code, 422)
        with self.assertRaises(HTTPException) as unknown:
            chat(ChatRequest(message="你好", agent="missing"))
        self.assertEqual(unknown.exception.status_code, 422)

    def test_human_handoff_lifecycle(self):
        session_id = f"api-handoff-{uuid.uuid4().hex[:8]}"
        transferred = chat(ChatRequest(
            message="我要转人工客服", session_id=session_id, agent="human",
        ))
        self.assertEqual(transferred.support_status, "waiting")
        self.assertTrue(transferred.ticket_id.startswith("TK"))

        claimed = claim_ticket(transferred.ticket_id, ClaimRequest(operator="测试客服"))
        self.assertEqual(claimed["ticket"]["status"], "active")

        customer_message = chat(ChatRequest(
            message="这是人工接入后的补充", session_id=session_id,
        ))
        self.assertEqual(customer_message.support_status, "active")
        self.assertEqual(customer_message.reply, "")

        human_reply(transferred.ticket_id, HumanReplyRequest(
            message="已收到您的补充。", operator="测试客服",
            attachments=[{"name": "proof.png", "url": "/uploads/proof.png"}],
            reply_to={"id": "m-source", "content": "这是人工接入后的补充", "author": "客户"},
        ))
        detail = get_ticket(transferred.ticket_id)
        self.assertEqual(detail["session"]["messages"][-1]["content"], "已收到您的补充。")
        self.assertEqual(detail["session"]["messages"][-1]["name"], "human")
        self.assertEqual(detail["session"]["messages"][-1]["meta"]["attachments"][0]["name"],
                         "proof.png")
        self.assertEqual(detail["session"]["messages"][-1]["meta"]["reply_to"]["author"], "客户")

        closed = close_ticket(transferred.ticket_id, ClaimRequest(operator="测试客服"))
        self.assertEqual(closed["ticket"]["status"], "closed")
        self.assertEqual(get_session(session_id)["support_status"], "closed")

    def test_customer_profile_search_presence_and_dashboard(self):
        token = uuid.uuid4().hex[:8]
        user_id = f"profile-search-user-{token}"
        profile = update_customer(user_id, CustomerProfileRequest(
            note="重点企业续约客户", tags=["vip", "enterprise"],
        ))
        self.assertEqual(profile["tags"], ["vip", "enterprise"])

        transferred = chat(ChatRequest(
            message="需要人工协助续约", session_id=f"profile-search-session-{token}",
            user_id=user_id, agent="human",
        ))
        found = list_tickets(status="waiting", q="重点企业续约客户")
        self.assertTrue(any(item["ticket_id"] == transferred.ticket_id for item in found["tickets"]))

        session_id = f"profile-search-session-{token}"
        set_presence(session_id, TypingRequest(actor="customer", typing=True))
        self.assertTrue(get_presence(session_id)["customer_typing"])
        metrics = dashboard()
        self.assertGreaterEqual(metrics["total"], 1)
        self.assertIn("vip", metrics["tag_counts"])
        self.assertEqual(len(metrics["weekly_series"]), 7)
        self.assertEqual(len(metrics["hourly_series"]), 24)
        now = datetime.now()
        self.assertEqual(len(metrics["monthly_series"]),
                         calendar.monthrange(now.year, now.month)[1])
        self.assertEqual(set(metrics["peaks"]),
                         {"weekly_day", "hour_range", "monthly_day"})
        self.assertTrue(all("created" in item and "completed" in item
                            for item in metrics["weekly_series"]))
        self.assertEqual([item["hour"] for item in metrics["hourly_series"]],
                         list(range(24)))

    def test_image_upload_validation(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        uploaded = upload_image(UploadRequest(
            name="pixel.png", mime_type="image/png", data=png,
        ))
        self.assertTrue(uploaded["url"].endswith(".png"))
        with self.assertRaises(HTTPException):
            upload_image(UploadRequest(name="x.txt", mime_type="text/plain", data="eA=="))

    def test_ai_settings_switch_and_provider_validation(self):
        original = get_ai_settings()
        try:
            with patch("app.api.server._save_ai_settings"):
                disabled = update_ai_settings(AISettingsRequest(
                    deepseek_enabled=False, suggestion_provider="deepseek",
                ))
                self.assertFalse(disabled["deepseek_enabled"])
                self.assertEqual(disabled["suggestion_provider"], "deepseek")
                self.assertEqual(disabled["effective_suggestion_provider"], "local")
                self.assertFalse(server.llm.primary_enabled)
                with self.assertRaises(HTTPException) as invalid:
                    update_ai_settings(AISettingsRequest(
                        deepseek_enabled=True, suggestion_provider="unknown",
                    ))
                self.assertEqual(invalid.exception.status_code, 422)
        finally:
            with patch("app.api.server._save_ai_settings"):
                update_ai_settings(AISettingsRequest(
                    deepseek_enabled=original["deepseek_enabled"],
                    suggestion_provider=original["suggestion_provider"],
                ))

    def _set_suggestion_provider(self, provider: str):
        with patch("app.api.server._save_ai_settings"):
            update_ai_settings(AISettingsRequest(
                deepseek_enabled=True, suggestion_provider=provider))

    def test_suggest_deepseek_reuses_client_agent_reply(self):
        transferred = chat(ChatRequest(
            message="我要转人工客服",
            session_id=f"api-suggest-{uuid.uuid4().hex[:8]}",
            agent="human",
        ))
        self.assertEqual(transferred.support_status, "waiting")
        original = get_ai_settings()
        fake = AgentReply(
            session_id=transferred.session_id, text="这是 Agent 链路生成的预回答",
            agent="support", route_reason="人工排队中，由智能 Agent 临时托管",
            tools_used=[], slots={}, turns=1, has_summary=False,
        )
        try:
            self._set_suggestion_provider("deepseek")
            # DeepSeek 预回答必须复用 scheduler.preview_reply（即客户端 agent 回复链路）
            with patch.object(server.scheduler, "preview_reply", return_value=fake) as mocked:
                out = suggest_reply(transferred.ticket_id, SuggestRequest(instruction=""))
                self.assertTrue(mocked.called)
            self.assertEqual(out["provider"], "deepseek")
            self.assertEqual(out["provider_name"], "DeepSeek Agent")
            self.assertEqual(out["suggestion"], "这是 Agent 链路生成的预回答")
            self.assertEqual(out["agent_name"], "排队托管 Agent")

            with patch("app.api.server.LLM_API_KEY", ""):
                with self.assertRaises(HTTPException) as no_key:
                    suggest_reply(transferred.ticket_id, SuggestRequest())
                self.assertEqual(no_key.exception.status_code, 503)
        finally:
            self._set_suggestion_provider(original["suggestion_provider"])

    def test_suggest_local_still_uses_local_llm(self):
        transferred = chat(ChatRequest(
            message="我要转人工客服",
            session_id=f"api-suggest-local-{uuid.uuid4().hex[:8]}",
            agent="human",
        ))
        original = get_ai_settings()
        try:
            self._set_suggestion_provider("local")
            with patch.object(server.local_llm, "chat",
                              return_value=LLMResponse(content="本地模型草稿")):
                out = suggest_reply(transferred.ticket_id, SuggestRequest())
            self.assertEqual(out["provider"], "local")
            self.assertEqual(out["provider_name"], "本地模型")
            self.assertEqual(out["suggestion"], "本地模型草稿")
        finally:
            self._set_suggestion_provider(original["suggestion_provider"])


if __name__ == "__main__":
    unittest.main()
