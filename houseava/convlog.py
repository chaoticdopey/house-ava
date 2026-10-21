"""A rolling log of every exchange with House Ava, tagged by room/satellite, so you can see what's
been asked of each Echo. Appends to
conversations.jsonl and keeps the recent ones in memory for the admin UI's Conversations tab.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque

from .config import data_dir


class ConversationLog:
    def __init__(self, keep: int = 500):
        self._recent: deque[dict] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._path = data_dir() / "conversations.jsonl"
        self._load_tail(keep)

    def _load_tail(self, keep: int) -> None:
        try:
            if self._path.exists():
                lines = self._path.read_text(encoding="utf-8").splitlines()[-keep:]
                for ln in lines:
                    try:
                        self._recent.append(json.loads(ln))
                    except Exception:
                        pass
        except Exception:
            pass

    def add(self, source: str, room: str, satellite: str, user: str, reply: str) -> None:
        rec = {"t": time.time(), "source": source, "room": room or "", "satellite": satellite or "",
               "user": user or "", "reply": reply or ""}
        with self._lock:
            self._recent.append(rec)
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except Exception:
                pass

    def recent(self, room: str | None = None, limit: int = 150) -> list[dict]:
        with self._lock:
            items = list(self._recent)
        if room:
            items = [r for r in items if r.get("room") == room]
        return items[-limit:]

    def rooms(self) -> list[str]:
        with self._lock:
            return sorted({r.get("room", "") for r in self._recent if r.get("room")})
