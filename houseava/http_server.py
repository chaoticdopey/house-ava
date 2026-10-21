"""The tiny reply server: the Echo fetches each spoken answer from here.

TECHO5's echod plays a reply by URL (the TTS_END event carries it): WAV at any rate, or MP3. We
publish 16 kHz mono WAV bytes in memory and hand back http://<this box>:<port>/tts/<id>.wav.
No auth (echod can't send any) — so ids are random, files live only a few minutes, and nothing
but finished replies is ever served.
"""

from __future__ import annotations

import logging
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("houseava.replies")
_KEEP_SECONDS = 600


class ReplyServer:
    def __init__(self, cfg):
        self.host = str(cfg.get("http.host", "0.0.0.0"))
        port = cfg.get("http.port", 8790)
        self.port = 8790 if port in (None, "") else int(port)      # 0 = any free port (tests)
        self.public_host = str(cfg.get("http.public_host", "") or "")
        self._files: dict[str, tuple[float, bytes, str]] = {}
        self._wake: dict[str, tuple[bytes, str]] = {}      # full filename -> (bytes, ctype), for /wake/<filename>
        self._unresolved: set[str] = set()                   # names already warned about in host_for
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None

    def register_wake(self, filename: str, data: bytes, ctype: str) -> None:
        """Serve a wake-word file (its .json manifest AND its .tflite) at /wake/<filename>."""
        self._wake[filename] = (data, ctype)

    # ---- lifecycle ------------------------------------------------------------------------------
    def start(self) -> "ReplyServer":
        if self._httpd is not None:
            return self
        files, lock, wake = self._files, self._lock, self._wake

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                path = self.path.split("?", 1)[0]
                if path.startswith("/wake/"):                 # a custom wake-word file for an Echo to download
                    name = path.rsplit("/", 1)[-1]            # full filename (manifest .json or model .tflite)
                    item = wake.get(name)
                    if item is None:
                        self.send_error(404)
                        return
                    data, ctype = item
                    log.info("wake model %r downloaded by %s (%d bytes)", name, self.client_address[0], len(data))
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                key = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                with lock:
                    item = files.get(key)
                if item is None:
                    self.send_error(404)
                    return
                _, data, ctype = item
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):  # quiet
                pass

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="houseava-replies").start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None

    # ---- publishing -----------------------------------------------------------------------------
    def publish(self, data: bytes, reach_ip: str = "", ext: str = "wav") -> str:
        """Store a reply and return the URL a satellite at `reach_ip` can fetch it from."""
        now = time.time()
        key = secrets.token_urlsafe(12)
        ctype = "audio/wav" if ext == "wav" else "audio/mpeg"
        with self._lock:
            for k in [k for k, (t, _, _) in self._files.items() if now - t > _KEEP_SECONDS]:
                self._files.pop(k, None)
            self._files[key] = (now, data, ctype)
        return f"http://{self.host_for(reach_ip)}:{self.port}/tts/{key}.{ext}"

    def host_for(self, reach_ip: str = "") -> str:
        """This machine's LAN address as seen from the satellite (config wins; else ask the routing table). A name
        this box can't look up (an Echo added by its .local name on a server without mDNS - aioesphomeapi finds it
        on its own) gets the default route's address, not 127.0.0.1, which the Echo would take as itself."""
        if self.public_host:
            return self.public_host
        for target in ([reach_ip] if reach_ip else []) + ["10.255.255.255"]:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect((target, 9))            # no packet is sent for UDP connect
                    return s.getsockname()[0]
            except OSError:
                if target == reach_ip and reach_ip not in self._unresolved:
                    self._unresolved.add(reach_ip)
                    log.warning("can't look up %s on this box; giving it this box's main address (set "
                                "http.public_host if the Echo can't reach that)", reach_ip)
        return "127.0.0.1"
