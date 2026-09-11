"""基于 JSON 文件的工单存储，接口可替换为数据库或工单平台。"""
from __future__ import annotations

import json
import hashlib
import pathlib
import threading
import time
import uuid


class TicketStore:
    def __init__(self, base_dir: str = "data/tickets"):
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, ticket_id: str) -> pathlib.Path:
        safe = "".join(c for c in ticket_id if c.isalnum() or c in "-_")
        return self.base_dir / f"{safe or 'ticket'}.json"

    def create(self, *, session_id: str, user_id: str, title: str,
               description: str, priority: str) -> dict:
        now = time.time()
        ticket = {
            "ticket_id": f"TK{uuid.uuid4().hex[:8].upper()}",
            "session_id": session_id,
            "user_id": user_id,
            "title": title,
            "description": description,
            "priority": priority,
            "status": "waiting",
            "assigned_to": None,
            "created_at": now,
            "updated_at": now,
        }
        self.save(ticket)
        return ticket

    def get(self, ticket_id: str) -> dict | None:
        path = self._path(ticket_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, ticket: dict) -> None:
        ticket["updated_at"] = time.time()
        with self._lock:
            path = self._path(ticket["ticket_id"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(ticket, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(path)

    def update(self, ticket_id: str, **changes) -> dict | None:
        ticket = self.get(ticket_id)
        if ticket is None:
            return None
        ticket.update(changes)
        self.save(ticket)
        return ticket

    def list(self, status: str | None = None) -> list[dict]:
        tickets = []
        for path in self.base_dir.glob("TK*.json"):
            try:
                ticket = json.loads(path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if status is None or ticket.get("status") == status:
                tickets.append(ticket)
        return sorted(tickets, key=lambda item: item.get("updated_at", 0), reverse=True)


ticket_store = TicketStore()


class CustomerStore:
    """跨工单保存客服对客户的备注与标签。"""

    def __init__(self, base_dir: str = "data/customers"):
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, user_id: str) -> pathlib.Path:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
        return self.base_dir / f"{digest}.json"

    def get(self, user_id: str) -> dict:
        path = self._path(user_id)
        if path.exists():
            try:
                return json.loads(path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {"user_id": user_id, "note": "", "tags": [], "updated_at": None}

    def save(self, user_id: str, note: str, tags: list[str]) -> dict:
        profile = {"user_id": user_id, "note": note, "tags": tags, "updated_at": time.time()}
        with self._lock:
            path = self._path(user_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(path)
        return profile

    def list(self) -> list[dict]:
        profiles = []
        for path in self.base_dir.glob("*.json"):
            try:
                profiles.append(json.loads(path.read_text("utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return profiles


customer_store = CustomerStore()
