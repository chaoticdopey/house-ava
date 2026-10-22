"""Admin web server for House Ava — settings, people, voice enrollment.

Runs on port 8792 (configurable). The phone app and any browser on the LAN connect
here to manage House Ava: change settings, add/remove people, enroll voices, view status.
PIN-protected (admin.pin in config; a blank one is replaced with a random PIN on the first run).

    Started automatically by the satellite hub, or standalone:
        python -m houseava.admin
"""

from __future__ import annotations

import copy
import io
import hmac
import json
import logging
import re
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from .config import Config, data_dir
from .voiceprint import VoiceProfile

log = logging.getLogger("houseava.admin")
# wrong PINs per address: ip -> (misses in a row, no tries before this time)
_MISSES: dict[str, tuple[int, float]] = {}
_MISS_LOCK = threading.Lock()

_VOICEPRINTS_DIR = "voiceprints"


def _vp_dir() -> Path:
    d = data_dir() / _VOICEPRINTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _vp_path(key: str) -> Path:
    slug = re.sub(r"[^a-z0-9_-]", "_", key.lower().strip())
    return _vp_dir() / f"{slug}.json"


class AdminHandler(BaseHTTPRequestHandler):
    server: "AdminServer"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _pin_ok(self, got: str) -> bool:
        """got == the PIN, with a backoff per address: after 5 misses in a row each try waits 30 s, doubling up to
        10 min, so a short PIN can't be found by running through them all."""
        if not got:
            return False                    # (the app's "is there a PIN at all?" probe isn't a guess)
        ip = self.client_address[0] if self.client_address else ""
        now = time.time()
        with _MISS_LOCK:
            misses, until = _MISSES.get(ip, (0, 0.0))
            if now < until:
                return False
        ok = hmac.compare_digest(str(got).encode(), str(self.server.cfg.get("admin.pin", "")).encode())
        with _MISS_LOCK:
            if ok:
                _MISSES.pop(ip, None)
            else:
                misses += 1
                wait = min(600.0, 30.0 * 2 ** (misses - 5)) if misses >= 5 else 0.0
                _MISSES[ip] = (misses, now + wait)
        return ok

    def _locked_for(self) -> float:
        ip = self.client_address[0] if self.client_address else ""
        with _MISS_LOCK:
            return max(0.0, _MISSES.get(ip, (0, 0.0))[1] - time.time())

    def _no_pin(self, error: str) -> None:
        wait = self._locked_for()
        if wait:
            self._json(429, {"error": f"Too many wrong PINs - try again in {int(wait) + 1} s"})
        else:
            self._json(401, {"error": error})

    def _auth(self) -> bool:
        pin = self.server.cfg.get("admin.pin", "")
        if not pin:
            return True
        got = self.headers.get("X-Pin", "")
        if not got:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("ava_pin="):
                    got = part[8:]
                    break
        if self._pin_ok(got):
            return True
        self._no_pin("Invalid PIN")
        return False

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, html: str) -> None:
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict | None:
        try:
            return json.loads(self._body())
        except Exception:
            self._json(400, {"error": "Invalid JSON"})
            return None

    def _query(self) -> dict[str, str]:
        qs = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in qs.items()}

    def _route(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    # ---- HTTP methods ----
    def do_OPTIONS(self):
        # the app is served from this same server, so no other site gets to call the API from a browser
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        route = self._route()
        if route == "/" or route == "/admin":
            self._html(200, ADMIN_HTML)
            return
        if not self._auth():
            return
        if route == "/api/status":
            self._get_status()
        elif route == "/api/conversations":
            self._get_conversations()
        elif route == "/api/config":
            self._get_config()
        elif route == "/api/satellites":
            self._get_satellites()
        elif route == "/api/people":
            self._get_people()
        elif route == "/api/routines":
            self._get_routines()
        else:
            self._json(404, {"error": "not found"})

    # ---- routines (automations: routines.py) ----
    def _routines(self):
        hub = self.server.hub
        rt = getattr(hub, "routines", None) if hub is not None else None
        if rt is None:
            self._json(503, {"error": "Routines run on the Echo hub, and it isn't running."})
        return rt

    def _get_routines(self):
        rt = self._routines()
        if rt is not None:
            self._json(200, {"routines": rt.list(), "meta": rt.meta()})

    def _post_routine(self):
        from .routines import Invalid

        data = self._json_body()
        if data is None:
            return
        rt = self._routines()
        if rt is None:
            return
        try:
            self._json(200, {"ok": True, "routine": rt.upsert(data)})
        except Invalid as exc:
            self._json(400, {"error": str(exc)})
        except Exception:                            # (a wrongly shaped body: say so, never drop the connection)
            log.exception("saving a routine failed")
            self._json(400, {"error": "that routine isn't something I can save"})

    def _post_routine_run(self):
        data = self._json_body()
        if data is None:
            return
        if not isinstance(data, dict):
            self._json(400, {"error": "send {\"id\": ...}"})
            return
        rt = self._routines()
        if rt is not None:
            res = rt.run(str(data.get("id", "")))
            self._json(200 if res.get("ok") else 404, res)

    def _delete_routine(self):
        rt = self._routines()
        if rt is not None:
            ok = rt.delete(self._query().get("id", ""))
            self._json(200 if ok else 404, {"ok": ok})

    def do_POST(self):
        route = self._route()
        if route == "/api/auth":
            self._post_auth()
            return
        if not self._auth():
            return
        if route == "/api/satellites":
            self._post_satellite()
        elif route == "/api/chat":
            self._post_chat()
        elif route == "/api/people":
            self._post_person()
        elif route == "/api/enroll":
            self._post_enroll()
        elif route == "/api/reset-voice":
            self._post_reset_voice()
        elif route == "/api/restart":
            self._post_restart()
        elif route == "/api/bluetooth":
            self._post_bluetooth()
        elif route == "/api/call":
            self._post_call()
        elif route == "/api/routines":
            self._post_routine()
        elif route == "/api/routines/run":
            self._post_routine_run()
        else:
            self._json(404, {"error": "not found"})

    def _post_call(self):
        """Live calls between the Echoes (livecall.py). {"action": "call", "from": <Echo name>, "to": <Echo name>} /
        {"action": "answer"|"hangup", "name": <Echo or "">} / {"action": "drop_in"|"dnd", "name": <Echo>, "on": bool}."""
        data = self._json_body()
        if data is None:
            return
        hub = self.server.hub
        if hub is None or not hasattr(hub, "call_cmd"):
            self._json(200, {"ok": False, "say": "The Echoes aren't running here."})
            return
        action = str(data.get("action", ""))
        names = {s.name: s for s in hub.sats}

        def where(n):                                    # the hub finds Echoes by room (or name)
            s = names.get(n or "")
            return (s.room or s.name) if s else (n or "")

        try:
            if action == "call":
                res = hub.call_cmd("call", target=where(data.get("to")), source=where(data.get("from")))
            elif action in ("answer", "hangup"):
                res = hub.call_cmd(action, target=where(data.get("name")) if data.get("name") else "")
            elif action in ("drop_in", "dnd"):
                res = hub.call_cmd(action, target=where(data.get("name")), on=bool(data.get("on")))
            else:
                self._json(400, {"error": "unknown action"})
                return
        except Exception as exc:
            log.exception("call action failed")
            res = {"ok": False, "say": f"That didn't work: {exc}"}
        res.pop("intercom_target", None)                 # (an Echo object - not for JSON)
        res.pop("intercom_open", None)
        self._json(200, res)

    def _post_bluetooth(self):
        """{"name": <satellite>, "action": "pair" | "stop"} -> Bluetooth pairing mode on that Echo (a phone pairs and
        plays its own music through it)."""
        data = self._json_body()
        if data is None:
            return
        hub = self.server.hub
        if hub is None or not hasattr(hub, "bluetooth_cmd"):
            self._json(503, {"error": "The Echo hub isn't running."})
            return
        action = str(data.get("action", "pair"))
        if action not in ("pair", "stop"):
            self._json(400, {"error": "action must be pair or stop"})
            return
        res = hub.bluetooth_cmd(action, satellite=str(data.get("name", "") or ""))
        self._json(200 if res.get("ok") else 409, {"ok": bool(res.get("ok")), "say": res.get("say", "")})

    def do_PATCH(self):
        if not self._auth():
            return
        route = self._route()
        if route == "/api/config":
            self._patch_config()
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._auth():
            return
        route = self._route()
        if route == "/api/satellites":
            self._delete_satellite()
        elif route == "/api/people":
            self._delete_person()
        elif route == "/api/routines":
            self._delete_routine()
        else:
            self._json(404, {"error": "not found"})

    # ---- auth ----
    def _post_auth(self):
        data = self._json_body()
        if data is None:
            return
        if not self.server.cfg.get("admin.pin", "") or self._pin_ok(str(data.get("pin", ""))):
            self._json(200, {"ok": True})
        else:
            self._no_pin("Wrong PIN")

    # ---- chat (type to her from the phone) ----
    def _post_chat(self):
        data = self._json_body()
        if data is None:
            return
        text = str(data.get("text", "")).strip()
        if not text:
            self._json(400, {"error": "empty"})
            return
        hub = self.server.hub
        if hub is None or not getattr(hub, "router", None):
            self._json(503, {"error": "Ava's brain isn't running."})
            return
        room = str(data.get("room", "") or self.server.cfg.get("console_room", "") or "")
        try:
            res = hub.router.handle(text, room)
            say = str((res or {}).get("say", ""))
            if getattr(hub, "convlog", None):
                hub.convlog.add("console", room or "console", "console", text, say)
            self._json(200, {"say": say, "ok": bool((res or {}).get("ok", True))})
        except Exception as exc:
            log.exception("console chat failed")
            self._json(500, {"error": str(exc)})

    # ---- conversations (what's been asked of each Echo) ----
    def _get_conversations(self):
        hub = self.server.hub
        if hub is None or not getattr(hub, "convlog", None):
            self._json(200, {"rooms": [], "items": []})
            return
        q = self._query()
        room = q.get("room", "") or None
        try:
            limit = max(1, min(500, int(q.get("limit", "150"))))
        except ValueError:
            limit = 150
        self._json(200, {"rooms": hub.convlog.rooms(), "items": hub.convlog.recent(room=room, limit=limit)})

    # ---- status ----
    def _get_status(self):
        hub = self.server.hub
        sats = []
        if hub:
            for s in hub.sats:
                sats.append({"name": s.name, "ip": s.ip, "room": s.room or "", "connected": s.connected})
        self._json(200, {
            "running": hub is not None,
            "satellites": sats,
            "music": hub.music is not None if hub else False,
            "uptime": time.time() - self.server.start_time,
        })

    # ---- config ----
    def _get_config(self):
        # deep-copy so redacting the PSK / dropping the admin block never mutates the live config
        cfg = copy.deepcopy(self.server.cfg.data)
        for s in cfg.get("satellites", []):
            if s.get("psk"):
                s["psk"] = "***"
        cfg.pop("admin", None)
        if isinstance(cfg.get("facecast"), dict):
            cfg["facecast"].pop("key", None)             # the Echo gets it from the hub; the app never needs it
        from .facecast import list_faces
        cfg["_faces"] = list_faces()
        self._json(200, cfg)

    def _patch_config(self):
        data = self._json_body()
        if data is None:
            return
        forbidden = {"admin", "satellites", "people"}
        changed = set()
        for key, val in data.items():
            top = key.split(".")[0]
            if top in forbidden or key.startswith("_") or key == "facecast.key":
                continue
            if self.server.cfg.get(key, None) != val:
                changed.add(key)
            self.server.cfg.set(key, val)
        self.server.cfg.save()
        # voice settings the Echo holds (listening, follow-ups, EQ, volume): push them now, not at its next reconnect
        hub = getattr(self.server, "hub", None)
        if hub is not None and hasattr(hub, "reapply_prefs") and \
                any(k.startswith("voice.") or k == "wake.threshold" for k in changed):
            try:
                hub.reapply_prefs(volume="voice.echo_volume" in changed)
            except Exception:
                log.exception("couldn't push the voice settings to the Echoes")
        self._json(200, {"ok": True})

    # ---- satellites ----
    def _get_satellites(self):
        sats = self.server.cfg.get("satellites", []) or []
        hub = self.server.hub
        result = []
        for s in sats:
            connected = has_bt = pairing = False
            call = {}
            if hub:
                for hs in hub.sats:
                    if hs.name == s.get("name"):
                        connected = hs.connected
                        has_bt = getattr(hs, "bt_pair_key", None) is not None
                        pairing = bool(getattr(hs, "bt_pairing", False))
                        try:
                            call = hub.calls.status(hs) if getattr(hub, "calls", None) else {}
                        except Exception:
                            call = {}
                        break
            result.append({
                "name": s.get("name", ""),
                "ip": s.get("ip", ""),
                "room": s.get("room", ""),
                "has_psk": bool(s.get("psk")),
                "connected": connected,
                "bluetooth": has_bt,
                "bt_pairing": pairing,
                "call": call,
            })
        self._json(200, result)

    def _post_satellite(self):
        data = self._json_body()
        if data is None:
            return
        name = data.get("name", "").strip()
        ip = data.get("ip", "").strip()
        if not name or not ip:
            self._json(400, {"error": "name and ip required"})
            return
        sats = self.server.cfg.get("satellites", []) or []
        sats = [s for s in sats if s.get("name") != name]
        entry = {"name": name, "ip": ip, "room": data.get("room", "").strip(), "psk": data.get("psk", "").strip()}
        existing = [s for s in (self.server.cfg.get("satellites", []) or []) if s.get("name") == name]
        if existing and not entry["psk"]:
            entry["psk"] = existing[0].get("psk", "")
        sats.append(entry)
        self.server.cfg.set("satellites", sats)
        self.server.cfg.save()
        self._json(200, {"ok": True, "count": len(sats)})

    def _delete_satellite(self):
        name = self._query().get("name", "")
        if not name:
            self._json(400, {"error": "name required"})
            return
        sats = [s for s in (self.server.cfg.get("satellites", []) or []) if s.get("name") != name]
        self.server.cfg.set("satellites", sats)
        self.server.cfg.save()
        self._json(200, {"ok": True, "count": len(sats)})

    # ---- people ----
    def _get_people(self):
        people = self.server.cfg.get("people.enrolled", []) or []
        result = []
        for p in people:
            key = p.get("key", p.get("name", "").lower().replace(" ", "_"))
            vp = VoiceProfile(_vp_path(key))
            result.append({
                "key": key,
                "name": p.get("name", ""),
                "adult": p.get("adult", False),
                "voice": vp.info(),
            })
        self._json(200, {
            "people": result,
            "identify": self.server.cfg.get("people.identify", True),
            "sensitivity": self.server.cfg.get("people.sensitivity", 1.0),
            "unknown_voice": self.server.cfg.get("people.unknown_voice", "allow"),
        })

    def _post_person(self):
        data = self._json_body()
        if data is None:
            return
        # settings-only updates (from the Voice Identification card) carry the "_settings" sentinel —
        # apply the fields but never add a person named "_settings".
        name = data.get("name", "").strip()
        key = (data.get("key") or name.lower().replace(" ", "_")).strip()
        settings_only = key == "_settings"
        if not settings_only:
            if not name:
                self._json(400, {"error": "name required"})
                return
            people = self.server.cfg.get("people.enrolled", []) or []
            found = False
            for p in people:
                pk = p.get("key", p.get("name", "").lower().replace(" ", "_"))
                if pk == key:
                    p["name"] = name
                    if "adult" in data:
                        p["adult"] = bool(data["adult"])
                    found = True
                    break
            if not found:
                people.append({"key": key, "name": name, "adult": bool(data.get("adult", False))})
            self.server.cfg.set("people.enrolled", people)
        if "identify" in data:
            self.server.cfg.set("people.identify", bool(data["identify"]))
        if "sensitivity" in data:
            self.server.cfg.set("people.sensitivity", float(data["sensitivity"]))
        if "unknown_voice" in data:
            self.server.cfg.set("people.unknown_voice", data["unknown_voice"])
        self.server.cfg.save()
        self._json(200, {"ok": True})

    def _delete_person(self):
        key = self._query().get("key", "")
        if not key:
            self._json(400, {"error": "key required"})
            return
        people = [p for p in (self.server.cfg.get("people.enrolled", []) or [])
                  if p.get("key", p.get("name", "").lower().replace(" ", "_")) != key]
        self.server.cfg.set("people.enrolled", people)
        self.server.cfg.save()
        vp = VoiceProfile(_vp_path(key))
        vp.delete()
        self._json(200, {"ok": True})

    # ---- voice enrollment ----
    def _post_enroll(self):
        q = self._query()
        key = q.get("key", "")
        label = q.get("label", "")
        if not key:
            self._json(400, {"error": "key required"})
            return
        raw = self._body()
        if len(raw) < 16000:
            self._json(400, {"error": "Audio too short (need ~12 seconds of speech)"})
            return
        pcm = _decode_audio(raw)
        if pcm is None:
            self._json(400, {"error": "Could not decode audio. Send 16 kHz int16 mono PCM or WAV."})
            return
        if len(pcm) < 16000 * 6:
            self._json(400, {"error": "Audio too short after decoding"})
            return
        vp = VoiceProfile(_vp_path(key))
        ok = vp.enroll(pcm, label=label)
        if ok:
            self._json(200, {"ok": True, "samples": vp.count, "info": vp.info()})
        else:
            self._json(400, {"error": "Not enough voiced speech. Read aloud clearly for 12 seconds."})

    def _post_reset_voice(self):
        key = self._query().get("key", "")
        if not key:
            self._json(400, {"error": "key required"})
            return
        vp = VoiceProfile(_vp_path(key))
        vp.delete()
        self._json(200, {"ok": True})

    # ---- restart ----
    def _post_restart(self):
        self._json(200, {"ok": True, "message": "Restart requested. Service will reload."})
        # TODO: signal the satellite hub to restart


def _decode_audio(raw: bytes) -> np.ndarray | None:
    """Decode raw PCM (int16 LE) or WAV to int16 mono 16 kHz ndarray."""
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return _decode_wav(raw)
    if len(raw) % 2 == 0:
        return np.frombuffer(raw, dtype=np.int16)
    return None


def _decode_wav(data: bytes) -> np.ndarray | None:
    """Minimal WAV parser — extract int16 mono PCM. Resamples if not 16 kHz."""
    try:
        bio = io.BytesIO(data)
        bio.read(12)  # RIFF header
        fmt_found = False
        sr = 16000
        channels = 1
        bits = 16
        while True:
            chunk_id = bio.read(4)
            if len(chunk_id) < 4:
                break
            chunk_size = struct.unpack("<I", bio.read(4))[0]
            if chunk_id == b"fmt ":
                fmt_data = bio.read(chunk_size)
                audio_fmt = struct.unpack("<H", fmt_data[:2])[0]
                if audio_fmt != 1:  # PCM only
                    return None
                channels = struct.unpack("<H", fmt_data[2:4])[0]
                sr = struct.unpack("<I", fmt_data[4:8])[0]
                bits = struct.unpack("<H", fmt_data[14:16])[0]
                fmt_found = True
            elif chunk_id == b"data":
                if not fmt_found:
                    bio.read(chunk_size)
                    continue
                pcm_data = bio.read(chunk_size)
                if bits == 16:
                    pcm = np.frombuffer(pcm_data, dtype=np.int16)
                elif bits == 32:
                    pcm = (np.frombuffer(pcm_data, dtype=np.int32).astype(np.float32) / 65536).astype(np.int16)
                else:
                    return None
                if channels > 1:
                    pcm = pcm.reshape(-1, channels)[:, 0].copy()
                if sr != 16000:
                    pcm = _resample(pcm, sr, 16000)
                return pcm
            else:
                bio.read(chunk_size)
        return None
    except Exception:
        return None


def _resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Simple linear-interpolation resample."""
    if src_rate == dst_rate:
        return pcm
    ratio = dst_rate / src_rate
    n_out = int(len(pcm) * ratio)
    indices = np.arange(n_out) / ratio
    idx = indices.astype(int)
    frac = indices - idx
    idx = np.clip(idx, 0, len(pcm) - 2)
    out = pcm[idx] * (1 - frac) + pcm[idx + 1] * frac
    return out.astype(np.int16)


class AdminServer:
    def __init__(self, cfg: Config, hub=None, port: int = 0):
        self.cfg = cfg
        self.hub = hub
        self.port = int(port or cfg.get("admin.port", 8792))      # (a --port override isn't saved into the config)
        self.start_time = time.time()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # No PIN would mean no login at all, for anything on the LAN, so the first run makes one and keeps it.
        if not str(self.cfg.get("admin.pin", "") or "").strip():
            import secrets
            pin = f"{secrets.randbelow(10**6):06d}"
            self.cfg.set("admin.pin", pin)
            saved = self.cfg.save()
            # (not the PIN itself when it's saved: people attach this log to public bug reports)
            if saved:
                log.warning("admin: no PIN was set, so one was made: it's admin.pin in %s", self.cfg.path)
            else:
                log.warning("admin: no PIN was set, so one was made: %s (it couldn't be saved, so it lasts until a "
                            "restart)", pin)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), AdminHandler)
        self.port = self._httpd.server_address[1]       # resolve 0 -> the real bound port
        self._httpd.cfg = self.cfg
        self._httpd.hub = self.hub
        self._httpd.start_time = self.start_time
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        log.info("admin server on port %s", self.port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()


# ---- standalone ----
def main():
    import argparse
    ap = argparse.ArgumentParser(description="House Ava admin server (standalone)")
    ap.add_argument("--port", type=int, default=0, help="override port (default: from config or 8792)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    srv = AdminServer(cfg, port=args.port)
    srv.start()
    log.info("House Ava admin: http://0.0.0.0:%d", srv.port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()


# -------------------------------------------------------------------------------------
# The admin UI — a single-page app served at GET /
# -------------------------------------------------------------------------------------

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>House Ava</title>
<style>
:root {
  --bg: #0f0f14; --surface: #1a1a24; --card: #22223a; --border: #2e2e48;
  --text: #e8e8f0; --dim: #8888a0; --accent: #7c5cfc; --accent2: #a78bfa;
  --green: #34d399; --red: #f87171; --yellow: #fbbf24;
  --radius: 12px; --gap: 12px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); min-height: 100dvh; display: flex; flex-direction: column;
  -webkit-tap-highlight-color: transparent; }
button { font: inherit; cursor: pointer; border: none; background: none; color: inherit; }
input, select, textarea { font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px; width: 100%; outline: none; }
input:focus, select:focus, textarea:focus { border-color: var(--accent); }

/* --- PIN screen --- */
#pin-screen { position: fixed; inset: 0; background: var(--bg); display: flex; flex-direction: column;
  align-items: center; justify-content: center; z-index: 100; gap: 16px; padding: 24px; }
#pin-screen.hidden { display: none; }
#pin-screen h2 { color: var(--accent2); font-size: 22px; }
#pin-screen input { max-width: 200px; text-align: center; font-size: 24px; letter-spacing: 8px; }
#pin-screen .err { color: var(--red); font-size: 14px; min-height: 20px; }
.pin-btn { background: var(--accent); color: #fff; padding: 12px 32px; border-radius: 8px; font-weight: 600; }

/* --- header --- */
header { background: var(--surface); padding: 12px 16px; display: flex; align-items: center; gap: 12px;
  border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 50; }
header h1 { font-size: 18px; font-weight: 700; flex: 1; }
header h1 span { color: var(--accent2); }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.dot.on { background: var(--green); } .dot.off { background: var(--red); }

/* --- tabs --- */
nav { display: flex; background: var(--surface); border-top: 1px solid var(--border);
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 50; padding-bottom: env(safe-area-inset-bottom, 0); }
nav button { flex: 1; padding: 10px 4px 8px; display: flex; flex-direction: column; align-items: center;
  gap: 2px; font-size: 11px; color: var(--dim); transition: color .15s; }
nav button.active { color: var(--accent2); }
nav svg { width: 22px; height: 22px; }

/* --- pages --- */
main { flex: 1; overflow-y: auto; padding: 16px 16px 80px; }
.page { display: none; }
.page.active { display: block; }

/* --- cards --- */
.card { background: var(--card); border-radius: var(--radius); padding: 16px; margin-bottom: var(--gap); }
.card h3 { font-size: 15px; font-weight: 600; margin-bottom: 12px; color: var(--accent2); }
.card-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.card-row:last-child { margin-bottom: 0; }
.card-row label { font-size: 13px; color: var(--dim); min-width: 80px; }
.card-row input, .card-row select { flex: 1; min-width: 0; }

/* --- list items --- */
.list-item { display: flex; align-items: center; gap: 10px; padding: 12px; background: var(--surface);
  border-radius: 8px; margin-bottom: 8px; }
.list-item .name { flex: 1; font-weight: 500; }
.list-item .sub { font-size: 12px; color: var(--dim); }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.badge.ok { background: rgba(52,211,153,.15); color: var(--green); }
.badge.warn { background: rgba(248,113,113,.15); color: var(--red); }
.badge.info { background: rgba(124,92,252,.15); color: var(--accent2); }

/* --- buttons --- */
.btn { padding: 10px 16px; border-radius: 8px; font-weight: 600; font-size: 14px; text-align: center; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-danger { background: var(--red); color: #fff; }
.btn-outline { border: 1px solid var(--border); color: var(--dim); }
.btn-sm { padding: 6px 12px; font-size: 12px; }
.btn:active { opacity: .8; }
.btn-row { display: flex; gap: 8px; margin-top: 12px; }

/* --- toggle --- */
.toggle { position: relative; width: 44px; height: 24px; flex-shrink: 0; }
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle .slider { position: absolute; inset: 0; background: var(--border); border-radius: 12px; transition: .2s;
  cursor: pointer; }
.toggle .slider::before { content: ''; position: absolute; width: 18px; height: 18px; border-radius: 50%;
  background: var(--dim); bottom: 3px; left: 3px; transition: .2s; }
.toggle input:checked + .slider { background: var(--accent); }
.toggle input:checked + .slider::before { transform: translateX(20px); background: #fff; }

/* --- recording UI --- */
.record-area { text-align: center; padding: 20px; }
.record-btn { width: 80px; height: 80px; border-radius: 50%; background: var(--accent); display: inline-flex;
  align-items: center; justify-content: center; transition: .2s; }
.record-btn.recording { background: var(--red); animation: pulse 1s infinite; }
@keyframes pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.08); } }
.record-btn svg { width: 32px; height: 32px; fill: #fff; }
.record-timer { font-size: 28px; font-weight: 700; margin: 12px 0; font-variant-numeric: tabular-nums; }
.record-hint { font-size: 13px; color: var(--dim); }

/* --- toast --- */
.toast { position: fixed; bottom: 70px; left: 50%; transform: translateX(-50%); background: var(--card);
  color: var(--text); padding: 10px 20px; border-radius: 8px; font-size: 14px; z-index: 200;
  opacity: 0; transition: opacity .3s; pointer-events: none; border: 1px solid var(--border); }
.toast.show { opacity: 1; }

/* --- modal --- */
.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 100; display: none;
  align-items: flex-end; justify-content: center; }
.modal-bg.open { display: flex; }
.modal { background: var(--surface); border-radius: 16px 16px 0 0; padding: 24px 16px 32px; width: 100%;
  max-width: 480px; max-height: 85vh; overflow-y: auto; }
.modal h3 { margin-bottom: 16px; }

/* --- chat / console --- */
#page-chat { height: calc(100dvh - 128px); }
#page-chat.active { display: flex; flex-direction: column; }
.chat-log { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding-bottom: 4px; }
.chat-msg { max-width: 82%; padding: 10px 14px; border-radius: 15px; font-size: 15px; line-height: 1.38; white-space: pre-wrap; word-wrap: break-word; }
.chat-msg.me { align-self: flex-end; background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
.chat-msg.ava { align-self: flex-start; background: var(--card); color: var(--text); border-bottom-left-radius: 4px; }
.chat-msg.typing { color: var(--dim); font-style: italic; }
.chat-empty { color: var(--dim); font-size: 14px; text-align: center; margin: auto; }
.chat-input-row { display: flex; gap: 8px; padding-top: 10px; }
.chat-input-row input { flex: 1; }

/* --- conversations --- */
.conv-item { background: var(--card); border-radius: 10px; padding: 12px; margin-bottom: 8px; }
.conv-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.conv-room { font-size: 12px; font-weight: 600; color: var(--accent2); }
.conv-time { font-size: 11px; color: var(--dim); }
.conv-you { font-size: 14px; color: var(--text); margin-bottom: 4px; }
.conv-you b, .conv-ava b { color: var(--dim); font-weight: 600; }
.conv-ava { font-size: 14px; color: var(--dim); }

/* --- routines --- */
.day-chips { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 4px; }
.chip { padding: 5px 9px; border-radius: 14px; border: 1px solid var(--border); color: var(--dim); font-size: 12px;
  background: transparent; }
.chip.on { background: var(--accent); color: #fff; border-color: var(--accent); }
.hint { font-size: 11px; color: var(--dim); margin: 0 0 10px; }
.rt-action { background: var(--card); border-radius: 10px; padding: 10px; margin-bottom: 8px; }
.rt-act-head { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; }
.rt-act-head select { flex: 1; min-width: 0; }
.rt-num { color: var(--accent2); font-weight: 700; min-width: 16px; }
.rt-act-body { display: flex; gap: 6px; flex-wrap: wrap; }
.rt-act-body select, .rt-act-body input { flex: 1; min-width: 110px; }
.ideas { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
</style>
</head>
<body>

<!-- PIN screen -->
<div id="pin-screen">
  <h2>House Ava</h2>
  <p style="color:var(--dim);font-size:14px">Enter your PIN</p>
  <input type="password" id="pin-input" inputmode="numeric" maxlength="8" autocomplete="off">
  <div class="err" id="pin-err"></div>
  <button class="pin-btn" onclick="checkPin()">Enter</button>
</div>

<!-- Header -->
<header>
  <h1>House <span>Ava</span></h1>
  <span class="dot" id="status-dot"></span>
</header>

<!-- Main content -->
<main>
  <!-- Chat page -->
  <div class="page active" id="page-chat">
    <div id="chat-log" class="chat-log"></div>
    <div class="chat-input-row">
      <input id="chat-input" placeholder="Type to Ava..." autocomplete="off">
      <button class="btn btn-primary" id="chat-send" onclick="sendChat()">Send</button>
    </div>
  </div>

  <!-- Status page -->
  <div class="page" id="page-status">
    <div class="card">
      <h3>Satellites</h3>
      <div id="sat-status-list"><p style="color:var(--dim)">Loading...</p></div>
    </div>
    <div class="card">
      <h3>Service</h3>
      <div class="card-row"><label>Running</label><span id="svc-running">-</span></div>
      <div class="card-row"><label>Music</label><span id="svc-music">-</span></div>
      <div class="card-row"><label>Uptime</label><span id="svc-uptime">-</span></div>
    </div>
  </div>

  <!-- People page -->
  <div class="page" id="page-people">
    <div class="card">
      <h3>Voice Identification</h3>
      <div class="card-row">
        <label>Enabled</label>
        <label class="toggle"><input type="checkbox" id="ppl-identify" onchange="savePeopleSettings()"><span class="slider"></span></label>
      </div>
      <div class="card-row">
        <label>Sensitivity</label>
        <input type="range" id="ppl-sensitivity" min="0.5" max="2.0" step="0.1" onchange="savePeopleSettings()">
      </div>
      <div class="card-row">
        <label>Unknown voice</label>
        <select id="ppl-unknown" onchange="savePeopleSettings()">
          <option value="allow">Allow</option>
          <option value="restrict">Restrict (like a kid)</option>
        </select>
      </div>
    </div>
    <div class="card">
      <h3>People</h3>
      <div id="people-list"></div>
      <div class="btn-row">
        <button class="btn btn-primary" onclick="showAddPerson()">Add Person</button>
      </div>
    </div>
  </div>

  <!-- Settings page -->
  <div class="page" id="page-settings">
    <div class="card">
      <h3>Character</h3>
      <div class="card-row"><label>Name</label><input id="cfg-name" value="Ava"></div>
      <div class="card-row"><label>Persona</label>
        <select id="cfg-persona"><option>sassy</option><option>warm</option><option>deadpan</option></select>
      </div>
    </div>
    <div class="card">
      <h3>Personality</h3>
      <div class="card-row">
        <label>Swearing</label>
        <label class="toggle"><input type="checkbox" id="cfg-cussing"><span class="slider"></span></label>
      </div>
      <div class="card-row">
        <label>Adult jokes</label>
        <label class="toggle"><input type="checkbox" id="cfg-adult"><span class="slider"></span></label>
      </div>
      <p style="color:var(--dim);font-size:12px">Adult language in her replies and light/plug confirmations. No kid mode — applies to everyone in the house.</p>
    </div>
    <div class="card">
      <h3>Brain</h3>
      <div class="card-row"><label>Model</label><input id="cfg-brain-model" value="qwen2.5:14b"></div>
      <div class="card-row"><label>Temperature</label><input type="number" id="cfg-brain-temp" step="0.1" min="0" max="2"></div>
      <div class="card-row"><label>Timeout (s)</label><input type="number" id="cfg-brain-timeout" min="5" max="120"></div>
    </div>
    <div class="card">
      <h3>Voice</h3>
      <div class="card-row"><label>TTS Voice</label>
        <select id="cfg-voice-edge">
          <option value="en-US-AvaNeural">Ava (adult woman)</option>
          <option value="en-US-AriaNeural">Aria</option>
          <option value="en-US-JennyNeural">Jenny</option>
          <option value="en-US-MichelleNeural">Michelle</option>
          <option value="en-US-EmmaNeural">Emma</option>
          <option value="en-GB-SoniaNeural">Sonia (British)</option>
          <option value="en-US-AndrewNeural">Andrew (man)</option>
          <option value="en-US-BrianNeural">Brian (man)</option>
          <option value="en-US-AnaNeural">Ana (child)</option>
        </select>
      </div>
      <div class="card-row"><label>Rate</label><input id="cfg-voice-rate" value="+0%"></div>
      <div class="card-row"><label>Pitch</label><input id="cfg-voice-pitch" value="+0Hz"></div>
      <div class="card-row"><label>Loudness</label><input id="cfg-voice-volume" value="+0%"></div>
      <div class="card-row"><label>Echo volume</label><input type="range" id="cfg-echo-volume" min="0" max="1" step="0.05"></div>
      <div class="card-row"><label>Bass</label><input type="range" id="cfg-bass" min="-6" max="6" step="1"></div>
      <div class="card-row"><label>Treble</label><input type="range" id="cfg-treble" min="-6" max="6" step="1"></div>
    </div>
    <div class="card">
      <h3>Listening</h3>
      <div class="card-row"><label>"Alexa" strictness <span id="cfg-wake-thr-val" style="color:var(--dim)"></span></label>
        <input type="range" id="cfg-wake-thr" min="0.80" max="0.99" step="0.01"
               oninput="document.getElementById('cfg-wake-thr-val').textContent=this.value"></div>
      <div class="card-row"><label>"Hey Ava" strictness <span id="cfg-ava-thr-val" style="color:var(--dim)"></span></label>
        <input type="range" id="cfg-ava-thr" min="0.30" max="0.99" step="0.01"
               oninput="document.getElementById('cfg-ava-thr-val').textContent=this.value"></div>
      <div class="card-row"><label>Follow-up (seconds, 0 = off)</label><input type="number" id="cfg-follow-up" min="0" max="30"></div>
      <div class="card-row"><label>Follow-ups per wake</label><input type="number" id="cfg-follow-ups" min="1" max="10"></div>
      <div class="card-row"><label>Follow-up sound</label>
        <select id="cfg-follow-tone">
          <option value="None">Silent (like Alexa)</option>
          <option value="Default">Same as the wake chime</option>
          <option value="Chirp">Chirp</option>
          <option value="Ding">Ding</option>
          <option value="Rise">Rise</option>
        </select>
      </div>
      <div class="card-row"><label>Longest listen (seconds)</label><input type="number" id="cfg-max-listen" min="5" max="30"></div>
      <div class="card-row">
        <label>Ignore talk that isn't for her</label>
        <label class="toggle"><input type="checkbox" id="cfg-ignore-chatter"><span class="slider"></span></label>
      </div>
      <p style="color:var(--dim);font-size:12px;margin:6px 0 0">Higher strictness = fewer false wake-ups from the TV or
        people talking. If she starts missing real "Alexa"s or "Hey Ava"s, lower that one a notch.</p>
    </div>
    <div class="card">
      <h3>Microphone</h3>
      <div class="card-row"><label>Gain <span id="cfg-mic-gain-val" style="color:var(--dim)"></span></label>
        <input type="range" id="cfg-mic-gain" min="0" max="59" step="1"
               oninput="document.getElementById('cfg-mic-gain-val').textContent=this.value+' dB'"></div>
      <div class="card-row"><label>Mics</label>
        <select id="cfg-mic-mixing">
          <option value="All microphones">All seven mics (hears further)</option>
          <option value="Center mic">Centre mic only</option>
        </select>
      </div>
      <div class="card-row">
        <label>Auto-level quiet speech</label>
        <label class="toggle"><input type="checkbox" id="cfg-mic-leveling"><span class="slider"></span></label>
      </div>
      <div class="card-row">
        <label>Noise reduction</label>
        <label class="toggle"><input type="checkbox" id="cfg-mic-denoise"><span class="slider"></span></label>
      </div>
      <p style="color:var(--dim);font-size:12px;margin:6px 0 0">Can't be heard from the next room? Raise the gain first
        (20 was the factory setting; 30 is the new normal), then lower "Alexa" strictness a notch. Changes apply the
        moment you save. By voice: "Alexa, turn your mic up".</p>
    </div>
    <div class="card">
      <h3>Offline Voice</h3>
      <div class="card-row">
        <label>Fallback</label>
        <label class="toggle"><input type="checkbox" id="cfg-offline-fallback"><span class="slider"></span></label>
      </div>
      <div class="card-row"><label>When offline</label>
        <select id="cfg-offline-voice">
          <option value="af_heart">Heart - US female (best)</option>
          <option value="af_bella">Bella - US female</option>
          <option value="af_nicole">Nicole - US female, soft</option>
          <option value="af_sarah">Sarah - US female</option>
          <option value="bf_emma">Emma - British female</option>
          <option value="am_michael">Michael - US male</option>
          <option value="bm_george">George - British male</option>
        </select>
      </div>
      <p style="color:var(--dim);font-size:12px;margin-top:4px">Used when the internet is down and Microsoft's voice can't be reached, so she never goes silent.</p>
    </div>
    <div class="card">
      <h3>Screen</h3>
      <div class="card-row"><label>Face</label>
        <select id="cfg-face" onchange="faceChoice()"></select>
      </div>
      <div class="card-row" id="cfg-face-path-row" style="display:none"><label>Picture</label>
        <input id="cfg-face-path" placeholder="/home/you/pictures/me.png">
      </div>
      <p style="color:var(--dim);font-size:12px">What the Echo screens show beside the clock. "My own picture" takes a
        picture (jpg / png / webp) or a folder of them on the House Ava box. It switches about a second after you save.</p>
      <h3>Music</h3>
      <div class="card-row">
        <label>Enabled</label>
        <label class="toggle"><input type="checkbox" id="cfg-music-enabled"><span class="slider"></span></label>
      </div>
      <div class="card-row"><label>Music root</label><input id="cfg-music-root" readonly></div>
    </div>
    <div class="btn-row" style="padding:0 0 16px">
      <button class="btn btn-primary" onclick="saveSettings()" style="width:100%">Save Settings</button>
    </div>
  </div>

  <!-- Satellites page -->
  <div class="page" id="page-sats">
    <div class="card">
      <h3>Echo Satellites</h3>
      <div id="sats-list"></div>
      <div class="btn-row">
        <button class="btn btn-primary" onclick="showAddSat()">Add Satellite</button>
      </div>
      <p style="color:var(--dim);font-size:12px;margin-top:10px">Add each Echo here once it's flashed — name it, set its room, and paste its key.</p>
    </div>
  </div>

  <!-- Routines page (automations: houseava/routines.py) -->
  <div class="page" id="page-routines">
    <div class="card">
      <h3>Routines</h3>
      <p class="hint" style="margin-bottom:8px">Like Alexa routines: something starts it (a phrase, a time, sunrise/sunset,
        someone walking in), then House Ava does the steps in order. <span id="rt-sun"></span></p>
      <div class="btn-row" style="margin-top:0">
        <button class="btn btn-primary" onclick="rtOpen(null)">New routine</button>
        <button class="btn btn-outline" onclick="loadRoutines()">Refresh</button>
      </div>
      <div class="ideas">
        <span class="hint" style="margin:6px 4px 0 0">Ideas:</span>
        <button class="chip" onclick="rtOpen(null,'goodnight')">Good night</button>
        <button class="chip" onclick="rtOpen(null,'morning')">Good morning</button>
        <button class="chip" onclick="rtOpen(null,'movie')">Movie time</button>
        <button class="chip" onclick="rtOpen(null,'porch')">Porch light at sunset</button>
        <button class="chip" onclick="rtOpen(null,'porchoff')">Porch off at bedtime</button>
        <button class="chip" onclick="rtOpen(null,'nightlight')">Night light on motion</button>
      </div>
    </div>
    <div id="rt-list"></div>
  </div>

  <!-- Conversations page -->
  <div class="page" id="page-convos">
    <div class="card">
      <h3>Conversations</h3>
      <div class="card-row">
        <label>Room</label>
        <select id="conv-room" onchange="loadConversations()"><option value="">All</option></select>
        <button class="btn btn-sm btn-outline" onclick="loadConversations()">Refresh</button>
      </div>
    </div>
    <div id="conv-list"></div>
  </div>
</main>

<!-- Bottom nav -->
<nav>
  <button class="active" onclick="switchTab('chat',this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"/></svg>
    Chat
  </button>
  <button onclick="switchTab('status',this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0a1 1 0 01-1-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 01-1 1"/></svg>
    Status
  </button>
  <button onclick="switchTab('people',this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
    People
  </button>
  <button onclick="switchTab('settings',this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 15a3 3 0 100-6 3 3 0 000 6z"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>
    Settings
  </button>
  <button onclick="switchTab('sats',this)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 010 8.49m-8.48-.01a6 6 0 010-8.49m11.31-2.82a10 10 0 010 14.14m-14.14 0a10 10 0 010-14.14"/></svg>
    Satellites
  </button>
  <button onclick="switchTab('routines',this); loadRoutines()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
    Routines
  </button>
  <button onclick="switchTab('convos',this); loadConversations()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 9a2 2 0 01-2 2H6l-4 4V4a2 2 0 012-2h8a2 2 0 012 2z"/><path d="M18 9h2a2 2 0 012 2v11l-4-4h-6a2 2 0 01-2-2v-1"/></svg>
    Convos
  </button>
</nav>

<!-- Toast -->
<div class="toast" id="toast"></div>

<!-- Add Person Modal -->
<div class="modal-bg" id="modal-person">
  <div class="modal">
    <h3 id="modal-person-title">Add Person</h3>
    <div class="card-row"><label>Name</label><input id="mp-name"></div>
    <div class="card-row">
      <label>Adult</label>
      <label class="toggle"><input type="checkbox" id="mp-adult"><span class="slider"></span></label>
    </div>
    <div class="btn-row">
      <button class="btn btn-outline" onclick="closeModal('modal-person')">Cancel</button>
      <button class="btn btn-primary" onclick="savePerson()">Save</button>
    </div>
  </div>
</div>

<!-- Add Satellite Modal -->
<div class="modal-bg" id="modal-sat">
  <div class="modal">
    <h3>Add Satellite</h3>
    <div class="card-row"><label>Name</label><input id="ms-name" placeholder="e.g. kitchen echo"></div>
    <div class="card-row"><label>IP</label><input id="ms-ip" placeholder="192.168.1.x"></div>
    <div class="card-row"><label>Room</label><input id="ms-room" placeholder="kitchen"></div>
    <div class="card-row"><label>PSK</label><input id="ms-psk" placeholder="base64 encryption key"></div>
    <div class="btn-row">
      <button class="btn btn-outline" onclick="closeModal('modal-sat')">Cancel</button>
      <button class="btn btn-primary" onclick="saveSat()">Save</button>
    </div>
  </div>
</div>

<!-- Routine editor -->
<div class="modal-bg" id="modal-routine">
  <div class="modal">
    <h3 id="rt-title">New routine</h3>
    <div class="card-row"><label>Name</label><input id="rt-name" placeholder="e.g. Good night" maxlength="60"></div>
    <div class="card-row"><label>Starts</label>
      <select id="rt-trigger" onchange="rtTriggerChanged(this.value)">
        <option value="voice">When I say…</option>
        <option value="time">At a time</option>
        <option value="sun">At sunrise / sunset</option>
        <option value="presence">When someone walks in</option>
        <option value="manual">Only when I tap Run</option>
      </select>
    </div>
    <div id="rt-trigger-fields"></div>
    <div id="rt-limits"></div>
    <h3 style="margin:14px 0 8px;font-size:15px">Then</h3>
    <div id="rt-actions"></div>
    <button class="btn btn-sm btn-outline" onclick="rtAddAction()">+ Add a step</button>
    <div id="rt-reply-row" class="card-row" style="margin-top:12px"><label>She says</label>
      <input id="rt-reply" placeholder="Okay. (when no step says anything)" maxlength="200"></div>
    <div id="rt-err" style="color:var(--red);font-size:13px;min-height:18px;margin-top:8px"></div>
    <div class="btn-row">
      <button class="btn btn-danger" id="rt-delete" onclick="rtDelete(this)">Delete</button>
      <button class="btn btn-outline" onclick="closeModal('modal-routine')">Cancel</button>
      <button class="btn btn-primary" onclick="rtSave()">Save</button>
    </div>
  </div>
</div>

<!-- Voice Enrollment Modal -->
<div class="modal-bg" id="modal-enroll">
  <div class="modal">
    <h3>Voice Enrollment</h3>
    <p style="color:var(--dim);font-size:13px;margin-bottom:16px" id="enroll-person-name"></p>
    <div class="record-area">
      <button class="record-btn" id="record-btn" onclick="toggleRecord()">
        <svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
      </button>
      <div class="record-timer" id="record-timer">12</div>
      <div class="record-hint" id="record-hint">Tap to start recording. Read aloud for 12 seconds.</div>
    </div>
    <div class="btn-row">
      <button class="btn btn-outline" onclick="cancelRecord()">Close</button>
    </div>
  </div>
</div>

<script>
const API = location.origin;
let pin = '';
let recording = false;
let mediaRecorder = null;
let audioCtx = null;
let enrollKey = '';

// ---- PIN ----
function checkPin() {
  const v = document.getElementById('pin-input').value;
  fetch(API + '/api/auth', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({pin: v}) })
    .then(r => { if (r.ok) { pin = v; rememberPin(v); document.getElementById('pin-screen').classList.add('hidden'); loadAll(); }
      else r.json().then(j => { document.getElementById('pin-err').textContent = (j && j.error) || 'Wrong PIN'; },
                         () => { document.getElementById('pin-err').textContent = 'Wrong PIN'; }); })
    .catch(() => { pin = v; document.getElementById('pin-screen').classList.add('hidden'); loadAll(); });
}
// this device remembers the PIN (the phone app / the PC window open straight in); a PIN that stops working is forgotten
function rememberPin(v) { try { localStorage.setItem('houseava_pin', v); } catch (e) {} }
function savedPin() { try { return localStorage.getItem('houseava_pin') || ''; } catch (e) { return ''; } }
document.getElementById('pin-input').addEventListener('keydown', e => { if (e.key === 'Enter') checkPin(); });
// auto-check: the remembered PIN, else no PIN set at all
(function () {
  const tryPin = v => fetch(API + '/api/auth', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({pin: v}) }).then(r => r.ok);
  const saved = savedPin();
  (saved ? tryPin(saved) : Promise.resolve(false)).then(ok => {
    if (ok) { pin = saved; return true; }
    if (saved) { try { localStorage.removeItem('houseava_pin'); } catch (e) {} }
    return tryPin('');
  }).then(ok => { if (ok) { document.getElementById('pin-screen').classList.add('hidden'); loadAll(); } }).catch(() => {});
})();

function hdr() { return { 'X-Pin': pin, 'Content-Type': 'application/json' }; }
function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign(hdr(), opts.headers || {});
  return fetch(API + path, opts).then(r => r.json());
}

// ---- tabs ----
function switchTab(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  btn.classList.add('active');
}

// ---- toast ----
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

// ---- modals ----
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

// ---- load all ----
function loadAll() { loadStatus(); loadPeople(); loadConfig(); loadSats(); loadRoutines(); }

// ---- status ----
function loadStatus() {
  api('/api/status').then(d => {
    document.getElementById('status-dot').className = 'dot ' + (d.running ? 'on' : 'off');
    document.getElementById('svc-running').textContent = d.running ? 'Yes' : 'No';
    document.getElementById('svc-music').textContent = d.music ? 'Active' : 'Off';
    const m = Math.floor(d.uptime / 60);
    document.getElementById('svc-uptime').textContent = m < 60 ? m + 'm' : Math.floor(m/60) + 'h ' + (m%60) + 'm';
    let html = '';
    (d.satellites || []).forEach(s => {
      html += '<div class="list-item"><span class="dot ' + (s.connected?'on':'off') + '"></span>' +
        '<div><div class="name">' + esc(s.name) + '</div><div class="sub">' + esc(s.ip) + ' &middot; ' + esc(s.room||'no room') + '</div></div></div>';
    });
    document.getElementById('sat-status-list').innerHTML = html || '<p style="color:var(--dim)">No satellites</p>';
  }).catch(() => {});
}

// ---- people ----
function loadPeople() {
  api('/api/people').then(d => {
    document.getElementById('ppl-identify').checked = d.identify;
    document.getElementById('ppl-sensitivity').value = d.sensitivity;
    document.getElementById('ppl-unknown').value = d.unknown_voice;
    let html = '';
    (d.people || []).forEach(p => {
      const v = p.voice;
      const vBadge = v.enrolled ? '<span class="badge ok">' + v.samples + ' sample' + (v.samples>1?'s':'') + '</span>'
        : '<span class="badge warn">No voice</span>';
      html += '<div class="list-item"><div style="flex:1"><div class="name">' + esc(p.name) + '</div>' +
        '<div class="sub">' + (p.adult ? 'Adult' : 'Restricted') + ' &middot; ' + vBadge + '</div></div>' +
        '<button class="btn btn-sm btn-primary" onclick="startEnroll(\'' + esc(p.key) + '\',\'' + esc(p.name) + '\')">Record</button>' +
        '<button class="btn btn-sm btn-outline" onclick="editPerson(\'' + esc(p.key) + '\',\'' + esc(p.name) + '\',' + p.adult + ')">Edit</button>' +
        '<button class="btn btn-sm btn-danger" onclick="deletePerson(\'' + esc(p.key) + '\')">X</button></div>';
    });
    document.getElementById('people-list').innerHTML = html || '<p style="color:var(--dim)">No people enrolled</p>';
  }).catch(() => {});
}

function savePeopleSettings() {
  api('/api/people', { method: 'POST', body: JSON.stringify({
    name: '_settings', key: '_settings',
    identify: document.getElementById('ppl-identify').checked,
    sensitivity: parseFloat(document.getElementById('ppl-sensitivity').value),
    unknown_voice: document.getElementById('ppl-unknown').value,
  })}).then(() => toast('Settings saved'));
}

function showAddPerson() {
  document.getElementById('modal-person-title').textContent = 'Add Person';
  document.getElementById('mp-name').value = '';
  document.getElementById('mp-adult').checked = false;
  document.getElementById('mp-name').dataset.key = '';
  document.getElementById('modal-person').classList.add('open');
}

function editPerson(key, name, adult) {
  document.getElementById('modal-person-title').textContent = 'Edit ' + name;
  document.getElementById('mp-name').value = name;
  document.getElementById('mp-adult').checked = adult;
  document.getElementById('mp-name').dataset.key = key;
  document.getElementById('modal-person').classList.add('open');
}

function savePerson() {
  const name = document.getElementById('mp-name').value.trim();
  if (!name) return toast('Name required');
  const key = document.getElementById('mp-name').dataset.key || name.toLowerCase().replace(/\s+/g, '_');
  api('/api/people', { method: 'POST', body: JSON.stringify({
    key, name, adult: document.getElementById('mp-adult').checked
  })}).then(() => { closeModal('modal-person'); loadPeople(); toast('Saved'); });
}

function deletePerson(key) {
  if (!confirm('Remove this person and their voice data?')) return;
  api('/api/people?key=' + encodeURIComponent(key), { method: 'DELETE' })
    .then(() => { loadPeople(); toast('Removed'); });
}

// ---- voice enrollment ----
function startEnroll(key, name) {
  enrollKey = key;
  document.getElementById('enroll-person-name').textContent = 'Recording voice for: ' + name;
  document.getElementById('record-timer').textContent = '12';
  document.getElementById('record-hint').textContent = 'Tap to start recording. Read aloud for 12 seconds.';
  document.getElementById('record-btn').classList.remove('recording');
  document.getElementById('modal-enroll').classList.add('open');
}

async function toggleRecord() {
  if (recording) return;
  recording = true;
  const btn = document.getElementById('record-btn');
  const timer = document.getElementById('record-timer');
  const hint = document.getElementById('record-hint');
  btn.classList.add('recording');
  hint.textContent = 'Recording... read aloud naturally.';

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: false } });
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    const source = audioCtx.createMediaStreamSource(stream);
    const processor = audioCtx.createScriptProcessor(4096, 1, 1);
    const chunks = [];

    processor.onaudioprocess = e => { chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
    source.connect(processor);
    processor.connect(audioCtx.destination);

    let secs = 12;
    timer.textContent = secs;
    const iv = setInterval(() => { secs--; timer.textContent = secs; if (secs <= 0) clearInterval(iv); }, 1000);

    await new Promise(r => setTimeout(r, 12000));
    clearInterval(iv);

    processor.disconnect();
    source.disconnect();
    stream.getTracks().forEach(t => t.stop());
    audioCtx.close();

    hint.textContent = 'Processing...';
    timer.textContent = '--';

    const totalLen = chunks.reduce((s, c) => s + c.length, 0);
    const merged = new Float32Array(totalLen);
    let off = 0;
    chunks.forEach(c => { merged.set(c, off); off += c.length; });

    const pcm = new Int16Array(merged.length);
    for (let i = 0; i < merged.length; i++) {
      const s = Math.max(-1, Math.min(1, merged[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }

    const resp = await fetch(API + '/api/enroll?key=' + encodeURIComponent(enrollKey) + '&label=' + encodeURIComponent(new Date().toLocaleDateString()), {
      method: 'POST',
      headers: { 'X-Pin': pin, 'Content-Type': 'application/octet-stream' },
      body: pcm.buffer,
    });
    const result = await resp.json();
    if (result.ok) {
      toast('Voice enrolled! ' + result.samples + ' sample(s).');
      hint.textContent = 'Done! Voice enrolled successfully.';
      btn.classList.remove('recording');
      loadPeople();
    } else {
      toast(result.error || 'Enrollment failed');
      hint.textContent = result.error || 'Failed. Try again.';
      btn.classList.remove('recording');
    }
  } catch (err) {
    toast('Microphone error: ' + err.message);
    hint.textContent = 'Microphone access denied or error.';
    btn.classList.remove('recording');
  }
  recording = false;
}

function cancelRecord() {
  recording = false;
  if (audioCtx && audioCtx.state !== 'closed') audioCtx.close().catch(()=>{});
  closeModal('modal-enroll');
}

// ---- config ----
// a voice set in the config that the list doesn't have gets added, so saving Settings keeps it (it came back
// blank and got saved as "")
function pick(id, value) {
  const sel = document.getElementById(id);
  if (value && ![...sel.options].some(o => o.value === value)) sel.add(new Option(value, value));
  sel.value = value;
}

function loadConfig() {
  api('/api/config').then(d => {
    document.getElementById('cfg-name').value = d.name || 'Ava';
    document.getElementById('cfg-persona').value = d.persona || 'sassy';
    document.getElementById('cfg-cussing').checked = (d.personality||{}).cussing !== false;
    document.getElementById('cfg-adult').checked = !!(d.personality||{}).adult;
    var fc = d.facecast || {}, faces = d._faces || [], sel = document.getElementById('cfg-face');
    sel.innerHTML = faces.map(f => '<option value="' + escA(f.id) + '">' + esc(f.name) +
      (f.voice ? ' (' + esc(f.voice.replace(/^[a-z]{2}-[A-Z]{2}-|Neural$/g, '')) + ' voice)' : '') + '</option>').join('') +
      '<option value="__own__">My own picture</option>';
    sel.dataset.pack = fc.face || 'ava';
    sel.value = fc.frames ? '__own__' : (fc.face || 'ava');
    if (!sel.value) sel.value = '__own__';
    document.getElementById('cfg-face-path').value = fc.frames || '';
    faceChoice();
    document.getElementById('cfg-brain-model').value = (d.brain||{}).model || '';
    document.getElementById('cfg-brain-temp').value = (d.brain||{}).temperature || 0.6;
    document.getElementById('cfg-brain-timeout').value = (d.brain||{}).timeout || 45;
    var v = d.voice || {};
    pick('cfg-voice-edge', v.edge_voice || 'en-US-AvaNeural');
    document.getElementById('cfg-voice-rate').value = v.rate || '+0%';
    document.getElementById('cfg-voice-pitch').value = v.pitch || '+0Hz';
    document.getElementById('cfg-voice-volume').value = v.volume || '+0%';
    document.getElementById('cfg-echo-volume').value = v.echo_volume != null ? v.echo_volume : 1.0;
    document.getElementById('cfg-bass').value = v.bass != null ? v.bass : 4;
    document.getElementById('cfg-treble').value = v.treble != null ? v.treble : 4;
    document.getElementById('cfg-wake-thr').value = v.wake_threshold != null ? v.wake_threshold : 0.87;
    document.getElementById('cfg-wake-thr-val').textContent = document.getElementById('cfg-wake-thr').value;
    var w = d.wake || {};
    document.getElementById('cfg-ava-thr').value = w.threshold != null ? w.threshold : 0.5;
    document.getElementById('cfg-ava-thr-val').textContent = document.getElementById('cfg-ava-thr').value;
    document.getElementById('cfg-follow-up').value = v.follow_up != null ? v.follow_up : 6;
    document.getElementById('cfg-follow-ups').value = v.follow_ups ? v.follow_ups : 1;
    document.getElementById('cfg-follow-tone').value = v.follow_up_tone || 'None';
    document.getElementById('cfg-max-listen').value = v.max_listen != null ? v.max_listen : 10;
    document.getElementById('cfg-ignore-chatter').checked = v.ignore_chatter !== false;
    document.getElementById('cfg-mic-gain').value = v.mic_gain != null ? v.mic_gain : 30;
    document.getElementById('cfg-mic-gain-val').textContent = document.getElementById('cfg-mic-gain').value + ' dB';
    document.getElementById('cfg-mic-mixing').value = v.mic_mixing || 'All microphones';
    document.getElementById('cfg-mic-leveling').checked = v.mic_leveling !== false;
    document.getElementById('cfg-mic-denoise').checked = !!v.mic_denoise;
    document.getElementById('cfg-offline-fallback').checked = v.offline_fallback !== false;
    pick('cfg-offline-voice', v.offline_voice || 'af_heart');
    document.getElementById('cfg-music-enabled').checked = (d.music||{}).enabled !== false;
    document.getElementById('cfg-music-root').value = (d.music||{}).root || '';
  }).catch(() => {});
}

function faceSel() { return document.getElementById('cfg-face').value; }
function faceChoice() {
  document.getElementById('cfg-face-path-row').style.display = faceSel() === '__own__' ? '' : 'none';
}

function saveSettings() {
  const data = {
    name: document.getElementById('cfg-name').value,
    persona: document.getElementById('cfg-persona').value,
    'personality.cussing': document.getElementById('cfg-cussing').checked,
    'personality.adult': document.getElementById('cfg-adult').checked,
    'facecast.face': faceSel() === '__own__' ? (document.getElementById('cfg-face').dataset.pack || 'ava') : faceSel(),
    'facecast.frames': faceSel() === '__own__' ? document.getElementById('cfg-face-path').value.trim() : '',
    'brain.model': document.getElementById('cfg-brain-model').value,
    'brain.temperature': parseFloat(document.getElementById('cfg-brain-temp').value),
    'brain.timeout': parseInt(document.getElementById('cfg-brain-timeout').value),
    'voice.edge_voice': document.getElementById('cfg-voice-edge').value,
    'voice.rate': document.getElementById('cfg-voice-rate').value,
    'voice.pitch': document.getElementById('cfg-voice-pitch').value,
    'voice.volume': document.getElementById('cfg-voice-volume').value,
    'voice.echo_volume': parseFloat(document.getElementById('cfg-echo-volume').value),
    'voice.bass': parseInt(document.getElementById('cfg-bass').value),
    'voice.treble': parseInt(document.getElementById('cfg-treble').value),
    'voice.wake_threshold': parseFloat(document.getElementById('cfg-wake-thr').value),
    'wake.threshold': parseFloat(document.getElementById('cfg-ava-thr').value),
    'voice.follow_up': Math.max(0, parseInt(document.getElementById('cfg-follow-up').value) || 0),
    'voice.follow_ups': Math.max(1, parseInt(document.getElementById('cfg-follow-ups').value) || 1),
    'voice.follow_up_tone': document.getElementById('cfg-follow-tone').value,
    'voice.max_listen': Math.min(30, Math.max(5, parseInt(document.getElementById('cfg-max-listen').value) || 10)),
    'voice.ignore_chatter': document.getElementById('cfg-ignore-chatter').checked,
    'voice.mic_gain': Math.min(59, Math.max(0, parseInt(document.getElementById('cfg-mic-gain').value) || 0)),
    'voice.mic_mixing': document.getElementById('cfg-mic-mixing').value,
    'voice.mic_leveling': document.getElementById('cfg-mic-leveling').checked,
    'voice.mic_denoise': document.getElementById('cfg-mic-denoise').checked,
    'voice.offline_fallback': document.getElementById('cfg-offline-fallback').checked,
    'voice.offline_voice': document.getElementById('cfg-offline-voice').value,
    'music.enabled': document.getElementById('cfg-music-enabled').checked,
  };
  if (faceSel() === '__own__' && !data['facecast.frames']) {
    toast('Type the picture\'s path first, or pick a face');
    return;
  }
  if (faceSel() !== '__own__') document.getElementById('cfg-face').dataset.pack = faceSel();
  api('/api/config', { method: 'PATCH', body: JSON.stringify(data) }).then(() => toast('Settings saved'));
}

// ---- satellites ----
function loadSats() {
  api('/api/satellites').then(sats => {
    let html = '';
    sats.forEach(s => {
      html += '<div class="list-item"><span class="dot ' + (s.connected?'on':'off') + '"></span>' +
        '<div style="flex:1"><div class="name">' + esc(s.name) + '</div>' +
        '<div class="sub">' + esc(s.ip) + ' &middot; ' + esc(s.room||'-') + ' &middot; PSK: ' + (s.has_psk?'set':'none') +
        (s.bt_pairing ? ' &middot; <b>Bluetooth pairing on</b>' : '') + '</div></div>' +
        (s.bluetooth && s.connected ? '<button class="btn btn-sm" style="margin-right:6px" data-name="' + escA(s.name) +
          '" onclick="btPair(this.dataset.name, ' + (s.bt_pairing ? "'stop'" : "'pair'") + ')">' + (s.bt_pairing ? 'Stop pairing' : 'Pair a phone') + '</button>' : '') +
        '<button class="btn btn-sm btn-danger" data-name="' + escA(s.name) + '" onclick="deleteSat(this.dataset.name)">X</button></div>' +
        callRow(s, sats);
    });
    document.getElementById('sats-list').innerHTML = html || '<p style="color:var(--dim)">No satellites configured</p>';
    if (sats.some(s => s.call && s.call.call_phase && s.call.call_phase !== 'Idle')) setTimeout(loadSats, 3000);
  }).catch(() => {});
}

// ---- live calls between the Echoes (livecall.py) ----
function callRow(s, sats) {
  const c = s.call || {};
  if (!s.connected || !c.available) return '';
  // names go in data- attributes, never inside a JS string in an onclick (an apostrophe - "Jordan's Echo" - would end
  // the string: the browser decodes &#39; back to ' before the JS runs, and the handler silently does nothing)
  const nm = ' data-name="' + escA(s.name) + '"';
  const phase = c.call_phase || 'Idle';
  const state = phase === 'Idle' ? (c.live_intercom === 'not set up'
      ? 'Live intercom: <b>not working</b> (check the house word on both Echoes)'
      : 'Live intercom: ' + (c.live_intercom === 'ok' ? 'working' : 'ready'))
    : '<b>' + esc(phase) + (c.call_peer ? ' &middot; ' + esc(c.call_peer) : '') + '</b>';
  let btns = '';
  if (phase === 'Idle') {
    sats.filter(o => o.name !== s.name && o.connected && o.call && o.call.available).forEach(o => {
      btns += '<button class="btn btn-sm" style="margin:4px 6px 0 0"' + nm + ' data-to="' + escA(o.name) +
              '" onclick="callAct({action:\'call\', from:this.dataset.name, to:this.dataset.to})">Call ' + esc(o.room || o.name) + '</button>';
    });
  }
  if (phase === 'Ringing') btns += '<button class="btn btn-sm" style="margin:4px 6px 0 0"' + nm + ' onclick="callAct({action:\'answer\', name:this.dataset.name})">Answer</button>';
  if (phase !== 'Idle') btns += '<button class="btn btn-sm btn-danger" style="margin:4px 6px 0 0"' + nm + ' onclick="callAct({action:\'hangup\', name:this.dataset.name})">Hang up</button>';
  const tog = (key, label, on) => '<label style="margin:4px 12px 0 0;display:inline-flex;gap:6px;align-items:center">' +
      '<input type="checkbox"' + nm + ' ' + (on ? 'checked' : '') + ' onchange="callSet(this.dataset.name, \'' + key + '\', this)">' + label + '</label>';
  return '<div class="sub" style="margin:-4px 0 10px 22px">' + state + '<div>' + btns + '</div><div>' +
    tog('drop_in', 'Allow Drop In', !!c.drop_in) + tog('dnd', 'Do not disturb (calls)', !!c.dnd) + '</div></div>';
}

function callAct(body) {
  api('/api/call', { method: 'POST', body: JSON.stringify(body) })
    .then(d => { toast(d.say || d.error || 'Done'); setTimeout(loadSats, 1500); })
    .catch(() => toast('Calls: no answer from House Ava'));
}

function callSet(name, key, box) {
  if (key === 'drop_in' && box.checked && !confirm('Allow Drop In on ' + name + '?\n\nWith Drop In on, a call to this Echo ' +
      'connects BY ITSELF after a chime - a live microphone into that room without anyone answering. Only turn it on if ' +
      'everyone in the house is OK with that.')) { box.checked = false; return; }
  callAct({action: key, name: name, on: box.checked});
}

// ---- routines (automations) ----
let RT = {list: [], meta: {rooms: [], bulbs: [], colors: [], plugs: [], echoes: [], sunrise: '', sunset: ''}};
let rtEdit = null, rtDelArmed = false;
const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const ACTION_LABELS = {lights: 'Lights on / off', brightness: 'Dim lights', color: 'Light color', plug: 'Smart plug',
  say: 'Say something', weather: 'Weather report', time: 'Say the time', music: 'Play music', music_stop: 'Stop music',
  volume: 'Echo volume', wait: 'Wait', security: 'Arm / disarm the house', ask: 'Ask Ava…'};
const ACTION_DEFAULTS = {lights: {target: 'all', on: true}, brightness: {target: 'all', percent: 50},
  color: {target: 'all', color: 'warm white'}, plug: {name: '', on: true}, say: {text: '', where: ''},
  weather: {when: 'today', where: ''}, time: {where: ''}, music: {query: '', where: ''}, music_stop: {where: ''},
  volume: {percent: 50, where: ''}, wait: {seconds: 60}, security: {arm: true}, ask: {text: '', where: ''}};
const TRIGGER_DEFAULTS = {voice: {phrases: []}, time: {at: '07:00', days: []}, sun: {event: 'sunset', offset: 0, days: []},
  presence: {room: '', cooldown: 10}, manual: {}};
const RT_IDEAS = {
  goodnight: {name: 'Good night', trigger: {type: 'voice', phrases: ['good night', 'goodnight']},
    actions: [{type: 'lights', target: 'all', on: false}, {type: 'say', text: 'Good night. Sleep tight.', where: ''}]},
  morning: {name: 'Good morning', trigger: {type: 'voice', phrases: ['good morning']},
    actions: [{type: 'lights', target: 'living room', on: true}, {type: 'brightness', target: 'living room', percent: 80},
      {type: 'time', where: ''}, {type: 'weather', when: 'today', where: ''}]},
  movie: {name: 'Movie time', trigger: {type: 'voice', phrases: ['movie time']},
    actions: [{type: 'brightness', target: 'living room', percent: 15}, {type: 'color', target: 'living room', color: 'warm white'},
      {type: 'say', text: 'Enjoy the movie.', where: ''}]},
  porch: {name: 'Porch light at sunset', trigger: {type: 'sun', event: 'sunset', offset: 0, days: []},
    actions: [{type: 'plug', name: 'porch', on: true}]},
  porchoff: {name: 'Porch light off at bedtime', trigger: {type: 'time', at: '23:30', days: []},
    actions: [{type: 'plug', name: 'porch', on: false}]},
  nightlight: {name: 'Night light when someone walks in', trigger: {type: 'presence', room: 'kitchen', cooldown: 10},
    limits: {between: ['sunset', 'sunrise']},
    actions: [{type: 'brightness', target: 'dining room', percent: 25}, {type: 'wait', seconds: 300},
      {type: 'lights', target: 'dining room', on: false}]},
};

function escA(s) { return esc(String(s == null ? '' : s)).replace(/"/g, '&quot;'); }
function cap(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }
function opt(v, label, sel) { return '<option value="' + escA(v) + '"' + (v === sel ? ' selected' : '') + '>' + esc(label) + '</option>'; }
function clone(o) { return JSON.parse(JSON.stringify(o)); }
function fmtTime(hhmm) {
  const p = String(hhmm || '').split(':').map(Number);
  if (p.length < 2 || isNaN(p[0])) return hhmm;
  return ((p[0] % 12) || 12) + ':' + String(p[1]).padStart(2, '0') + (p[0] < 12 ? ' AM' : ' PM');
}
function fmtDays(days) {
  if (!days || !days.length || days.length === 7) return 'every day';
  const s = days.join(',');
  if (s === '0,1,2,3,4') return 'weekdays';
  if (s === '5,6') return 'weekends';
  return days.map(d => DAY_NAMES[d]).join(', ');
}
function fmtWhen(w) { return (w === 'sunrise' || w === 'sunset') ? w : fmtTime(w); }
function rtSummary(r) {
  const t = r.trigger || {};
  let s;
  if (t.type === 'voice') s = 'When you say "' + (t.phrases || []).join('" or "') + '"';
  else if (t.type === 'time') s = 'At ' + fmtTime(t.at) + ', ' + fmtDays(t.days);
  else if (t.type === 'sun') {
    const o = t.offset || 0;
    s = (o ? Math.abs(o) + ' min ' + (o < 0 ? 'before ' : 'after ') : 'At ') + t.event + ', ' + fmtDays(t.days);
  } else if (t.type === 'presence') s = 'When someone is in ' + (t.room ? 'the ' + t.room : 'any room with an Echo');
  else s = 'Only when you tap Run';
  const l = r.limits || {};
  if (l.between) s += ' · only ' + fmtWhen(l.between[0]) + ' to ' + fmtWhen(l.between[1]);
  if (l.days && l.days.length) s += ' · only ' + fmtDays(l.days);
  const n = (r.actions || []).length;
  return s + ' · ' + n + ' step' + (n === 1 ? '' : 's');
}

function loadRoutines() {
  api('/api/routines').then(d => {
    const el = document.getElementById('rt-list');
    if (d.error) { el.innerHTML = '<p class="hint">' + esc(d.error) + '</p>'; return; }
    RT.list = d.routines || [];
    RT.meta = Object.assign(RT.meta, d.meta || {});
    document.getElementById('rt-sun').textContent = (RT.meta.sunrise === '-' && RT.meta.sunset === '-')
      ? 'Sunrise / sunset need your location: set location.lat and location.lon in the config.'
      : 'Today: sunrise ' + RT.meta.sunrise + ', sunset ' + RT.meta.sunset + '.';
    renderRoutines();
  }).catch(() => {});
}
function renderRoutines() {
  const el = document.getElementById('rt-list');
  if (!RT.list.length) {
    el.innerHTML = '<p class="hint" style="font-size:14px">No routines yet. Tap New routine, or start from one of the ideas.</p>';
    return;
  }
  el.innerHTML = RT.list.map((r, i) => '<div class="list-item" style="flex-wrap:wrap">' +
    '<div style="flex:1;min-width:170px"><div class="name">' + esc(r.name) + '</div><div class="sub">' + esc(rtSummary(r)) +
    '</div></div>' +
    '<label class="toggle"><input type="checkbox" ' + (r.enabled ? 'checked' : '') + ' onchange="rtToggle(' + i +
    ',this.checked)"><span class="slider"></span></label>' +
    '<button class="btn btn-sm btn-outline" onclick="rtRun(' + i + ')">Run</button>' +
    '<button class="btn btn-sm btn-outline" onclick="rtOpen(' + i + ')">Edit</button></div>').join('');
}
function rtToggle(i, on) {
  const r = clone(RT.list[i]);
  r.enabled = on;
  api('/api/routines', {method: 'POST', body: JSON.stringify(r)})
    .then(d => { if (d.error) toast(d.error); else toast(r.name + (on ? ' on' : ' off')); loadRoutines(); });
}
function rtRun(i) {
  api('/api/routines/run', {method: 'POST', body: JSON.stringify({id: RT.list[i].id})})
    .then(d => toast(d.say || d.error || 'Done'));
}

function rtOpen(i, idea) {
  const editing = (i !== null && i !== undefined && !idea);
  const base = idea ? RT_IDEAS[idea] : editing ? RT.list[i]
    : {name: '', trigger: {type: 'voice', phrases: []}, actions: [{type: 'lights', target: 'all', on: true}]};
  rtEdit = clone(base);
  if (!rtEdit.limits) rtEdit.limits = {};
  if (rtEdit.enabled === undefined) rtEdit.enabled = true;
  if (idea && rtEdit.trigger.type === 'presence' && RT.meta.echoes.length &&
      !RT.meta.echoes.some(e => e.room === rtEdit.trigger.room)) rtEdit.trigger.room = RT.meta.echoes[0].room || '';
  rtDelArmed = false;
  const del = document.getElementById('rt-delete');
  del.textContent = 'Delete';
  del.style.display = editing ? '' : 'none';
  document.getElementById('rt-title').textContent = editing ? 'Edit routine' : 'New routine';
  document.getElementById('rt-name').value = rtEdit.name || '';
  document.getElementById('rt-trigger').value = rtEdit.trigger.type;
  document.getElementById('rt-reply').value = rtEdit.reply || '';
  document.getElementById('rt-err').textContent = '';
  rtRenderTrigger();
  rtRenderActions();
  document.getElementById('modal-routine').classList.add('open');
}
function rtTriggerChanged(type) {
  rtEdit.trigger = Object.assign({type: type}, clone(TRIGGER_DEFAULTS[type]));
  if (type === 'presence' && RT.meta.echoes.length) rtEdit.trigger.room = RT.meta.echoes[0].room || '';
  rtRenderTrigger();
}
function dayChips(path, days) {
  days = days || [];
  return '<div class="day-chips">' + DAY_NAMES.map((d, i) => '<button type="button" class="chip' +
    (days.indexOf(i) >= 0 ? ' on' : '') + '" onclick="rtDay(\'' + path + '\',' + i + ')">' + d + '</button>').join('') +
    '</div><p class="hint">None picked = every day.</p>';
}
function rtDay(path, i) {
  const o = path === 'trigger' ? rtEdit.trigger : rtEdit.limits;
  o.days = o.days || [];
  const k = o.days.indexOf(i);
  if (k >= 0) o.days.splice(k, 1); else o.days.push(i);
  o.days.sort();
  rtRenderTrigger();
}
function echoRooms() {     // (lowercase: what the hub saves and matches on, whatever case the room was typed in)
  return Array.from(new Set(RT.meta.echoes.map(e => String(e.room || '').toLowerCase()).filter(Boolean)));
}
function whenPick(i, v) {
  const isTime = v !== 'sunrise' && v !== 'sunset';
  return '<select onchange="rtWhen(' + i + ',this.value)">' + opt('sunset', 'Sunset', v) + opt('sunrise', 'Sunrise', v) +
    opt('time', 'A time', isTime ? 'time' : '') + '</select>' +
    (isTime ? '<input type="time" value="' + escA(v) + '" oninput="rtEdit.limits.between[' + i + ']=this.value">' : '');
}
function rtWhen(i, v) { rtEdit.limits.between[i] = v === 'time' ? '22:00' : v; rtRenderTrigger(); }
function rtBetween(v) { if (v) rtEdit.limits.between = ['sunset', 'sunrise']; else delete rtEdit.limits.between; rtRenderTrigger(); }
function rtRenderTrigger() {
  const t = rtEdit.trigger;
  let h = '';
  if (t.type === 'voice') {
    h = '<div class="card-row"><label>I say</label><input value="' + escA((t.phrases || []).join(', ')) +
      '" placeholder="good night, goodnight" oninput="rtEdit.trigger.phrases=this.value.split(\',\')"></div>' +
      '<p class="hint">After "Alexa" or "Hey Ava". Separate several with commas. "Run" + the routine\'s name works too.</p>';
  } else if (t.type === 'time') {
    h = '<div class="card-row"><label>At</label><input type="time" value="' + escA(t.at) +
      '" oninput="rtEdit.trigger.at=this.value"></div>' + dayChips('trigger', t.days);
  } else if (t.type === 'sun') {
    h = '<div class="card-row"><label>At</label><select onchange="rtEdit.trigger.event=this.value">' +
      opt('sunset', 'Sunset', t.event) + opt('sunrise', 'Sunrise', t.event) + '</select></div>' +
      '<div class="card-row"><label>Minutes</label><input type="number" min="-240" max="240" value="' + (t.offset || 0) +
      '" oninput="rtEdit.trigger.offset=parseInt(this.value)||0"></div>' +
      '<p class="hint">Minus = before (-15 is 15 minutes before). Today: sunrise ' + esc(RT.meta.sunrise) + ', sunset ' +
      esc(RT.meta.sunset) + '.</p>' + dayChips('trigger', t.days);
  } else if (t.type === 'presence') {
    h = '<div class="card-row"><label>Room</label><select onchange="rtEdit.trigger.room=this.value">' +
      opt('', 'Any room with an Echo', t.room) + echoRooms().map(r => opt(r, cap(r), t.room)).join('') + '</select></div>' +
      '<div class="card-row"><label>Not again for</label><input type="number" min="1" max="1440" value="' +
      (t.cooldown || 10) + '" oninput="rtEdit.trigger.cooldown=parseInt(this.value)||10"><span class="hint" ' +
      'style="margin:0">minutes</span></div><p class="hint">Uses the Echo\'s presence sensor.</p>';
  } else {
    h = '<p class="hint">Run it from this list, or say "Alexa, run" and its name.</p>';
  }
  document.getElementById('rt-trigger-fields').innerHTML = h;
  let L = '';
  if (t.type === 'presence') {
    const b = rtEdit.limits.between;
    L = '<div class="card-row"><label>Only</label><select onchange="rtBetween(this.value)">' +
      opt('', 'Any time of day', b ? 'x' : '') + opt('x', 'Between…', b ? 'x' : '') + '</select></div>';
    if (b) L += '<div class="card-row"><label>From</label>' + whenPick(0, b[0]) + '</div>' +
      '<div class="card-row"><label>Until</label>' + whenPick(1, b[1]) + '</div>';
    L += '<div class="card-row" style="margin-bottom:4px"><label>Only on</label></div>' + dayChips('limits', rtEdit.limits.days);
  }
  document.getElementById('rt-limits').innerHTML = L;
  document.getElementById('rt-reply-row').style.display = t.type === 'voice' ? '' : 'none';
}
function targetOptions(sel) {
  let h = opt('all', 'All lights', sel);
  RT.meta.rooms.forEach(r => h += opt(r, cap(r), sel));
  RT.meta.bulbs.forEach(b => { if (RT.meta.rooms.indexOf(b) < 0) h += opt(b, cap(b), sel); });
  if (sel && sel !== 'all' && RT.meta.rooms.indexOf(sel) < 0 && RT.meta.bulbs.indexOf(sel) < 0) h += opt(sel, cap(sel), sel);
  return h;
}
function whereOptions(sel) {
  return opt('', 'The Echo it\'s about (else every Echo)', sel) + opt('all', 'Every Echo', sel) +
    echoRooms().map(r => opt(r, 'The ' + r + ' Echo', sel)).join('');
}
function rtAddAction() { rtEdit.actions.push(Object.assign({type: 'lights'}, clone(ACTION_DEFAULTS.lights))); rtRenderActions(); }
function rtActType(i, t) {
  rtEdit.actions[i] = Object.assign({type: t}, clone(ACTION_DEFAULTS[t]));
  if (t === 'plug' && RT.meta.plugs.length) rtEdit.actions[i].name = RT.meta.plugs[0];
  rtRenderActions();
}
function rtMove(i, d) {
  const a = rtEdit.actions, j = i + d;
  if (j < 0 || j >= a.length) return;
  const x = a[i]; a[i] = a[j]; a[j] = x;
  rtRenderActions();
}
function rtDel(i) { rtEdit.actions.splice(i, 1); rtRenderActions(); }
function rtActionBody(a, i) {
  const A = 'rtEdit.actions[' + i + ']';
  const where = '<select onchange="' + A + '.where=this.value">' + whereOptions(a.where || '') + '</select>';
  const onoff = (k, on, yes, no) => '<select onchange="' + A + '.' + k + '=this.value===\'1\'">' +
    opt('1', yes, on ? '1' : '0') + opt('0', no, on ? '1' : '0') + '</select>';
  const num = (k, v, lo, hi, unit) => '<input type="number" min="' + lo + '" max="' + hi + '" value="' + escA(v) +
    '" oninput="' + A + '.' + k + '=parseInt(this.value)||0"><span class="hint" style="margin:auto 0">' + unit + '</span>';
  const txt = (k, v, ph) => '<input value="' + escA(v) + '" placeholder="' + escA(ph) + '" oninput="' + A + '.' + k +
    '=this.value" style="flex-basis:100%">';
  const tgt = '<select onchange="' + A + '.target=this.value">' + targetOptions(a.target) + '</select>';
  switch (a.type) {
    case 'lights': return tgt + onoff('on', a.on, 'On', 'Off');
    case 'brightness': return tgt + num('percent', a.percent, 1, 100, '%');
    case 'color':
      // (what's shown is what's saved: a color / plug that isn't on this hub's list becomes the first one listed)
      if (RT.meta.colors.length && RT.meta.colors.indexOf(a.color) < 0) a.color = RT.meta.colors[0];
      return tgt + '<select onchange="' + A + '.color=this.value">' +
        RT.meta.colors.map(c => opt(c, cap(c), a.color)).join('') + '</select>';
    case 'plug':
      if (!RT.meta.plugs.length) return '<p class="hint" style="margin:0">No smart plugs are set up on House Ava.</p>';
      if (RT.meta.plugs.indexOf(a.name) < 0) a.name = RT.meta.plugs[0];
      return '<select onchange="' + A + '.name=this.value">' + RT.meta.plugs.map(p => opt(p, cap(p), a.name)).join('') +
        '</select>' + onoff('on', a.on, 'On', 'Off');
    case 'say': return txt('text', a.text, 'What she says') + where;
    case 'weather': return '<select onchange="' + A + '.when=this.value">' + opt('today', 'Today', a.when) +
      opt('tomorrow', 'Tomorrow', a.when) + '</select>' + where;
    case 'time': return where;
    case 'music': return txt('query', a.query, 'e.g. Fleetwood Mac, or some jazz') + where;
    case 'music_stop': return where;
    case 'volume': return num('percent', a.percent, 0, 100, '%') + where;
    case 'wait': return num('seconds', a.seconds, 1, 14400, 'seconds');
    case 'security': return onoff('arm', a.arm, 'Arm the house', 'Disarm');
    case 'ask': return txt('text', a.text, 'Anything you\'d say to her, e.g. turn on the porch') + where;
  }
  return '';
}
function rtRenderActions() {
  const el = document.getElementById('rt-actions');
  if (!rtEdit.actions.length) { el.innerHTML = '<p class="hint">No steps yet.</p>'; return; }
  el.innerHTML = rtEdit.actions.map((a, i) => '<div class="rt-action"><div class="rt-act-head"><span class="rt-num">' +
    (i + 1) + '</span><select onchange="rtActType(' + i + ',this.value)">' +
    Object.keys(ACTION_LABELS).map(k => opt(k, ACTION_LABELS[k], a.type)).join('') + '</select>' +
    '<button class="btn btn-sm btn-outline" onclick="rtMove(' + i + ',-1)" title="Up">&uarr;</button>' +
    '<button class="btn btn-sm btn-outline" onclick="rtMove(' + i + ',1)" title="Down">&darr;</button>' +
    '<button class="btn btn-sm btn-outline" onclick="rtDel(' + i + ')" title="Remove">&times;</button></div>' +
    '<div class="rt-act-body">' + rtActionBody(a, i) + '</div></div>').join('');
}
function rtSave() {
  rtEdit.name = document.getElementById('rt-name').value.trim();
  rtEdit.reply = document.getElementById('rt-reply').value.trim();
  if (rtEdit.trigger.type !== 'presence') rtEdit.limits = {};
  api('/api/routines', {method: 'POST', body: JSON.stringify(rtEdit)}).then(d => {
    if (d.error) { document.getElementById('rt-err').textContent = cap(d.error) + '.'; return; }
    closeModal('modal-routine');
    toast('Saved ' + d.routine.name);
    loadRoutines();
  }).catch(() => { document.getElementById('rt-err').textContent = 'No answer from House Ava.'; });
}
function rtDelete(btn) {
  if (!rtEdit || !rtEdit.id) return;
  if (!rtDelArmed) {                       // (two taps, not confirm(): the phone app's WebView may not show dialogs)
    rtDelArmed = true;
    btn.textContent = 'Tap again to delete';
    setTimeout(() => { rtDelArmed = false; btn.textContent = 'Delete'; }, 3000);
    return;
  }
  api('/api/routines?id=' + encodeURIComponent(rtEdit.id), {method: 'DELETE'})
    .then(d => {
      if (!d.ok) { document.getElementById('rt-err').textContent = d.error ? cap(d.error) + '.' : 'It wasn\'t deleted.'; return; }
      closeModal('modal-routine'); toast('Deleted'); loadRoutines();
    })
    .catch(() => { document.getElementById('rt-err').textContent = 'No answer from House Ava.'; });
}

function btPair(name, action) {
  api('/api/bluetooth', { method: 'POST', body: JSON.stringify({name: name, action: action}) })
    .then(d => { toast(d.say || d.error || 'Done'); setTimeout(loadSats, 1500); })
    .catch(() => toast('Bluetooth: no answer from the Echo'));
}

function showAddSat() {
  document.getElementById('ms-name').value = '';
  document.getElementById('ms-ip').value = '';
  document.getElementById('ms-room').value = '';
  document.getElementById('ms-psk').value = '';
  document.getElementById('modal-sat').classList.add('open');
}

function saveSat() {
  const name = document.getElementById('ms-name').value.trim();
  const ip = document.getElementById('ms-ip').value.trim();
  if (!name || !ip) return toast('Name and IP required');
  api('/api/satellites', { method: 'POST', body: JSON.stringify({
    name, ip, room: document.getElementById('ms-room').value.trim(),
    psk: document.getElementById('ms-psk').value.trim(),
  })}).then(() => { closeModal('modal-sat'); loadSats(); loadStatus(); toast('Satellite saved'); });
}

function deleteSat(name) {
  if (!confirm('Remove satellite "' + name + '"?')) return;
  api('/api/satellites?name=' + encodeURIComponent(name), { method: 'DELETE' })
    .then(() => { loadSats(); loadStatus(); toast('Removed'); });
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ---- conversations ----
function loadConversations() {
  const room = document.getElementById('conv-room').value;
  api('/api/conversations?limit=150' + (room ? '&room=' + encodeURIComponent(room) : '')).then(d => {
    const sel = document.getElementById('conv-room');
    const cur = sel.value;
    let opts = '<option value="">All</option>';
    (d.rooms || []).forEach(r => { opts += '<option value="' + esc(r) + '">' + esc(r) + '</option>'; });
    sel.innerHTML = opts; sel.value = cur;
    const items = (d.items || []).slice().reverse();   // newest first
    let html = '';
    items.forEach(it => {
      const when = new Date(it.t * 1000).toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
      const where = it.source === 'console' ? 'Console' : (it.room || it.satellite || 'Echo');
      html += '<div class="conv-item"><div class="conv-head"><span class="conv-room">' + esc(where) + '</span>' +
        '<span class="conv-time">' + esc(when) + '</span></div>' +
        '<div class="conv-you"><b>You:</b> ' + esc(it.user) + '</div>' +
        '<div class="conv-ava"><b>Ava:</b> ' + esc(it.reply) + '</div></div>';
    });
    document.getElementById('conv-list').innerHTML = html ||
      '<p style="color:var(--dim);text-align:center;margin-top:20px">Nothing yet. Talk to one of your Echoes and it\'ll show up here.</p>';
  }).catch(() => {});
}

// ---- chat / console ----
function addMsg(text, who) {
  const log = document.getElementById('chat-log');
  const empty = log.querySelector('.chat-empty');
  if (empty) empty.remove();
  const d = document.createElement('div');
  d.className = 'chat-msg ' + who;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}
function sendChat() {
  const inp = document.getElementById('chat-input');
  const text = inp.value.trim();
  if (!text) return;
  addMsg(text, 'me');
  inp.value = '';
  const typing = addMsg('Ava is thinking…', 'ava typing');
  document.getElementById('chat-send').disabled = true;
  api('/api/chat', { method: 'POST', body: JSON.stringify({ text }) })
    .then(r => { typing.remove(); addMsg(r.say || r.error || '…', 'ava'); })
    .catch(() => { typing.remove(); addMsg("Couldn't reach her — is she on your home Wi-Fi?", 'ava'); })
    .finally(() => { document.getElementById('chat-send').disabled = false; inp.focus(); });
}
document.getElementById('chat-input').addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });
(function(){ const log = document.getElementById('chat-log');
  if (log && !log.children.length) { const e = document.createElement('div'); e.className = 'chat-empty';
    e.textContent = 'Say hi to Ava, ask her anything, or control the house.'; log.appendChild(e); } })();

// auto-refresh status every 30s
setInterval(loadStatus, 30000);
</script>
</body>
</html>"""
