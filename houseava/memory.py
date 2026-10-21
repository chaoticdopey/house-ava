"""Her own memory — separate from desktop Ava. Facts she's told ("bedtime is ten", "the porch
key is under the mat") plus a short rolling conversation so follow-ups make sense. JSON on disk.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

_STOP = {"the", "a", "an", "is", "are", "my", "me", "i", "to", "of", "in", "on", "at", "for", "and", "that",
         "this", "it", "do", "you", "what", "when", "where", "who", "how", "remember", "about", "know", "tell",
         "whats", "what's", "does", "did", "was", "were", "be", "have", "has", "with", "from", "your", "our"}


def _words(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9']+", (s or "").lower()):
        if w in _STOP or len(w) < 2:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


class Memory:
    def __init__(self, path: Path, max_turns: int = 12):
        self.path = Path(path)
        self.facts: list[dict] = []
        self.turns: list[dict] = []           # [{role, text, room, ts}]
        self.max_turns = max_turns
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self.facts = d.get("facts", []) or []
                self.turns = d.get("turns", []) or []
        except Exception:
            self.facts, self.turns = [], []

    def _save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps({"facts": self.facts, "turns": self.turns[-40:]}, indent=2),
                               encoding="utf-8")
                tmp.replace(self.path)
            except Exception:
                pass

    # ---- facts ----
    def remember(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "say": "Remember what?"}
        low = text.lower()
        self.facts = [f for f in self.facts if f["text"].lower() != low]
        self.facts.append({"text": text, "ts": time.time()})
        self._save()
        return {"ok": True, "say": "Got it, I'll remember that."}

    def recall(self, query: str, n: int = 3) -> list[str]:
        qw = _words(query)
        if not qw:
            return []
        scored = [(len(qw & _words(f["text"])), f["text"]) for f in self.facts]
        return [t for s, t in sorted(scored, reverse=True) if s > 0][:n]

    def forget(self, query: str) -> dict:
        qw = _words(query)
        before = len(self.facts)
        self.facts = [f for f in self.facts if not (qw and qw <= _words(f["text"]))]
        self._save()
        gone = before - len(self.facts)
        return {"ok": gone > 0, "say": "Forgotten." if gone else "I had nothing like that saved."}

    # ---- conversation ----
    def add_turn(self, role: str, text: str, room: str = "") -> dict:
        entry = {"role": role, "text": text, "room": room, "ts": time.time()}
        self.turns.append(entry)
        self.turns = self.turns[-40:]
        self._save()
        return entry

    def drop_turn(self, entry) -> bool:
        """Take back exactly this turn (what add_turn returned) - speech she decided wasn't for her is never
        remembered - even if other turns (another Echo, the console) were added after it."""
        kept = [t for t in self.turns if t is not entry]
        if len(kept) == len(self.turns):
            return False
        self.turns = kept
        self._save()
        return True

    def recent(self, room: str = "", n: int | None = None) -> list[dict]:
        n = n or self.max_turns
        rows = [t for t in self.turns if (not room or t.get("room") == room)]
        return [{"role": t["role"], "content": t["text"]} for t in rows[-n:]]
