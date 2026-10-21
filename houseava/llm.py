"""The brain: a local Ollama model (qwen2.5:14b by default). Minimal, standalone.

By default this hits Ollama at 127.0.0.1:11434; point brain.url at another box if Ollama runs there. `chat` returns text, `chat_json` returns a dict.
"""

from __future__ import annotations

import json
import socket
from urllib.parse import urlparse

import requests


class BrainError(RuntimeError):
    pass


class Brain:
    def __init__(self, cfg):
        self.url = str(cfg.get("brain.url", "http://127.0.0.1:11434")).rstrip("/")
        self.model = str(cfg.get("brain.model", "qwen2.5:14b"))
        self.temperature = float(cfg.get("brain.temperature", 0.6))
        self.timeout = int(cfg.get("brain.timeout", 45))
        self._s = requests.Session()

    def _hostport(self) -> tuple[str, int]:
        p = urlparse(self.url)
        return (p.hostname or "127.0.0.1"), (p.port or 11434)

    def is_up(self) -> bool:
        host, port = self._hostport()
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            return False

    def status(self) -> tuple[bool, str]:
        if not self.is_up():
            return False, f"brain unreachable at {self.url}"
        try:
            r = self._s.get(f"{self.url}/api/tags", timeout=4)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            ok = any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)
            return (True, f"{self.model} ready") if ok else (False, f"{self.model} not pulled")
        except Exception as exc:
            return False, f"brain error: {exc}"

    def chat(self, system: str, user: str, *, temperature: float | None = None,
             history: list[dict] | None = None) -> str:
        msgs = [{"role": "system", "content": system}]
        if history:
            msgs += history
        msgs.append({"role": "user", "content": user})
        payload = {
            "model": self.model, "messages": msgs, "stream": False,
            "options": {"temperature": self.temperature if temperature is None else temperature},
        }
        try:
            r = self._s.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return (r.json().get("message", {}).get("content") or "").strip()
        except Exception as exc:
            raise BrainError(str(exc)) from exc

    def chat_json(self, system: str, user: str, *, temperature: float | None = None) -> dict:
        payload = {
            "model": self.model, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.2 if temperature is None else temperature},
        }
        try:
            r = self._s.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            txt = (r.json().get("message", {}).get("content") or "").strip()
            return json.loads(txt) if txt else {}
        except json.JSONDecodeError:
            return {}
        except Exception as exc:
            raise BrainError(str(exc)) from exc
