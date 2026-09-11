"""会话持久化：JSON 文件存储（可无缝替换为 Redis / DB，接口只有 get_or_create / save）。"""
from __future__ import annotations

import json
import pathlib
import re
import threading
import time

from ..core.models import SessionData


class FileSessionStore:
    def __init__(self, base_dir: str = "data/sessions"):
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, session_id: str) -> pathlib.Path:
        safe = re.sub(r"[^\w.-]", "_", session_id) or "session"
        return self.base_dir / f"{safe}.json"

    def get_or_create(self, session_id: str, user_id: str = "anonymous") -> SessionData:
        path = self._path(session_id)
        if path.exists():
            try:
                return SessionData.from_dict(json.loads(path.read_text("utf-8")))
            except Exception:
                pass  # 损坏的会话文件视为新建，不让单个会话拖垮服务
        return SessionData(session_id=session_id, user_id=user_id)

    def save(self, session: SessionData) -> None:
        session.updated_at = time.time()
        with self._lock:
            path = self._path(session.session_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(path)  # 先写临时文件再替换，避免并发/崩溃留下半截 JSON
