"""Home security — "Arm the house".

Arming starts a short exit delay; after it, any presence an Echo reports (its occupancy sensor now,
ESP32 PIR/mmWave sensors later) is logged, pushed to your phone, and sounded on the Echo. "Disarm"
stops it. Phone push is ntfy.sh (no account — you subscribe to the topic in the ntfy app); a Telegram
path can be added later.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("houseava.security")


class Security:
    def __init__(self, cfg, convlog=None, announce=None):
        self.cfg = cfg
        self.convlog = convlog
        self.announce = announce            # callable(text, room) -> speak on the Echo(s)
        self.armed = False
        self.armed_at = 0.0
        self._last_alert = 0.0

    @property
    def exit_delay(self) -> float:
        return float(self.cfg.get("security.exit_delay", 40))

    def status(self) -> str:
        if not self.armed:
            return "The house is disarmed."
        left = self.exit_delay - (time.time() - self.armed_at)
        return f"The house is armed, watching for movement." if left <= 0 else \
            f"The house is arming — {int(left)} seconds left to get out."

    def arm(self) -> dict:
        self.armed = True
        self.armed_at = time.time()
        self._last_alert = 0.0
        d = int(self.exit_delay)
        log.info("security: ARMED (exit delay %ds)", d)
        return {"ok": True, "say": f"House armed. You've got {d} seconds to get out before I start watching."}

    def disarm(self) -> dict:
        was = self.armed
        self.armed = False
        log.info("security: DISARMED")
        return {"ok": True, "say": "House disarmed." if was else "It wasn't armed, but okay — disarmed."}

    def on_presence(self, room: str, present: bool) -> None:
        """An Echo (or sensor) reported presence. Alert if we're armed and past the exit delay."""
        if not present or not self.armed:
            return
        if time.time() - self.armed_at < self.exit_delay:
            return                                      # still the window to leave
        now = time.time()
        if now - self._last_alert < float(self.cfg.get("security.alert_cooldown", 25)):
            return                                      # don't spam while someone's moving around
        self._last_alert = now
        where = room or "the house"
        log.warning("security: MOTION while armed in %s", where)
        if self.convlog:
            self.convlog.add("security", room, "", "(motion)", f"Movement detected in {where} while armed.")
        self._push(f"Movement detected in {where}.", where)
        if self.cfg.get("security.announce", True) and self.announce:
            try:
                self.announce(f"Movement detected in {where}. The house is armed.", room)
            except Exception:
                log.exception("security announce failed")

    def _push(self, text: str, where: str) -> None:
        """Phone push, off-thread: on_presence is called from the Echo's state callback ON the hub's event loop, and a
        blocking HTTPS POST there (up to ~16 s on a dead WAN) would deafen every Echo for that long."""
        topic = str(self.cfg.get("security.ntfy_topic", "") or "").strip()
        if not topic:
            return
        import threading
        threading.Thread(target=self._post, args=(topic, text), daemon=True, name="security-push").start()

    @staticmethod
    def _post(topic: str, text: str) -> None:
        try:
            import requests
            requests.post(f"https://ntfy.sh/{topic}", data=text.encode(),
                          headers={"Title": "House Ava — Motion", "Priority": "urgent",
                                   "Tags": "rotating_light,house"}, timeout=8)
            log.info("security: pushed alert to ntfy topic")
        except Exception as exc:
            log.warning("security: phone push failed: %s", exc)
