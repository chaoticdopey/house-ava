"""Live two-way calls between the Echoes (TECHO5 >= 1.2.15's own intercom).

How the firmware does it (echod internal/feature/phone, read 10-16):
  * the CALLING Echo runs its action  intercom_call {device: "<the other Echo's friendly name>"}; audio then goes
    straight Echo-to-Echo (TCP 8181, Noise-encrypted with the "house word") - this hub never carries it
  * the called Echo rings (screen: Answer / Decline) for 30 s, or with its "Allow Drop In" switch on, chimes and
    connects by itself. Either side ends it on the screen, or with the phone_hangup button/action
  * text sensor "phone" = Ringing / Calling / In call during a call; between calls it is NEVER "Idle" but the SIP
    line's status (phone.go statusText: "Not set up" / "Ready" / "Connecting" / a problem) - so anything that isn't
    one of the three call phases means idle here (the 10-16 review caught the hub waiting for an "Idle" that never
    comes: held announcements were never released). event esphome.techo5_phone with data
    {event: dialing|ringing|answered|not_answered|declined|missed|ended, peer, device, kind, direction, reason, ...}
  * NEEDS the same house word typed on each Echo's setup page (no API can set it). Without it intercom_call fails
    with "this device has no house word" - then House Ava falls back to her walkie-talkie intercom
  * while an Echo is in a call its wake word is ignored and the call owns the speaker: announcements, replies and
    music sent to it would cut the call (or silence a ring for good), so they are HELD here and sent after it ends
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

log = logging.getLogger("houseava.livecall")

EVENT = "esphome.techo5_phone"
BUSY = ("Ringing", "Calling", "In call")


class EchoCall:
    """One Echo's call controls and state."""

    def __init__(self, name: str):
        self.name = name
        self.device_name = name                    # its friendly name = what the OTHER Echo dials
        self.services: dict = {}                   # action name -> UserService
        self.keys: dict[str, int] = {}             # phone, phone_peer, phone_answer, phone_hangup, intercom_drop_in, ...
        self.phase = "Idle"                        # Ringing / Calling / In call, or Idle (anything else the sensor says)
        self.line_status = ""                      # the sensor's own idle text ("Not set up" / "Ready" ...), for the app
        self.peer = ""
        self.drop_in: bool | None = None
        self.dnd: bool | None = None
        self.live_ok: bool | None = None           # did the last live call get through (False = no house word / failed)
        self.last_problem = ""
        self.incoming: tuple[str, float] = ("", 0.0)   # this Echo's own last incoming result (missed / declined)
        self.held: list[tuple[str, bool]] = []     # announcements waiting for the call to end
        self.draining = False                      # the ONE drain of `held` for this Echo is running
        self.events: deque = deque(maxlen=30)

    @property
    def busy(self) -> bool:
        return self.phase in BUSY


class LiveCalls:
    def __init__(self, hub):
        self.hub = hub
        self.echoes: dict[str, EchoCall] = {}

    def of(self, sat) -> EchoCall:
        ec = self.echoes.get(sat.name)
        if ec is None:
            ec = self.echoes[sat.name] = EchoCall(sat.name)
        return ec

    def busy(self, sat) -> bool:
        ec = self.echoes.get(getattr(sat, "name", ""))
        return bool(ec and ec.busy)

    def queued(self, sat) -> bool:
        """In a call, OR its held lines are still being said: anything new for its speaker waits its turn (sent now,
        it would cut a held line off, or be cut off by the next one)."""
        ec = self.echoes.get(getattr(sat, "name", ""))
        return bool(ec and (ec.busy or ec.draining or ec.held))

    # ---- learning a connected Echo ----------------------------------------------------------------
    def learn(self, sat, ents, services, friendly: str = "") -> None:
        ec = self.of(sat)
        ec.services = {s.name: s for s in (services or [])}
        if friendly:
            ec.device_name = friendly
        want = ("phone", "phone_peer", "phone_answer", "phone_hangup", "intercom_drop_in", "intercom_do_not_disturb",
                "phone_ring_sound", "setup_page", "screen_call_button")
        ec.keys = {e.object_id: e.key for e in ents if getattr(e, "object_id", "") in want}
        if "intercom_call" in ec.services:
            log.info("%s: live intercom available (dials as %r)", sat.name, ec.device_name)

    def available(self, sat) -> bool:
        return "intercom_call" in self.of(sat).services

    def on_state(self, sat, st) -> None:
        """Every entity state from this Echo (called from its subscribe_states callback)."""
        ec = self.echoes.get(sat.name)
        if ec is None or not ec.keys:
            return
        key = getattr(st, "key", None)
        if key == ec.keys.get("phone"):
            raw = str(getattr(st, "state", "") or "")
            was = ec.phase
            ec.phase = raw if raw in BUSY else "Idle"
            if raw not in BUSY:
                ec.line_status = raw
            if was in BUSY and not ec.busy:
                self._release(sat, ec)
        elif key == ec.keys.get("phone_peer"):
            ec.peer = str(getattr(st, "state", "") or "")
        elif key == ec.keys.get("intercom_drop_in"):
            ec.drop_in = bool(getattr(st, "state", False))
        elif key == ec.keys.get("intercom_do_not_disturb"):
            ec.dnd = bool(getattr(st, "state", False))

    def on_service_call(self, sat, call) -> None:
        """esphome.techo5_phone events from this Echo (subscribe_service_calls)."""
        if not getattr(call, "is_event", False) or getattr(call, "service", "") != EVENT:
            return
        data = dict(getattr(call, "data", {}) or {})
        if data.get("kind") not in (None, "", "intercom"):
            return
        ec = self.of(sat)
        ev = str(data.get("event", ""))
        ec.events.append((time.time(), ev, data))
        log.info("%s: call %s %s", sat.name, ev, {k: v for k, v in data.items() if k not in ("event", "device", "kind")})
        if ev in ("missed", "declined") and data.get("direction") == "incoming":
            ec.incoming = (ev, time.time())
        elif ev == "answered":
            ec.live_ok, ec.last_problem = True, ""
        elif ev == "not_answered" and data.get("direction", "outgoing") == "outgoing":
            reason = str(data.get("reason", ""))
            peer = str(data.get("peer", ""))
            self._tell_why(sat, ec, reason, peer)

    def _room_of(self, device_name: str) -> str:
        for s in getattr(self.hub, "sats", []):
            if self.of(s).device_name.lower() == (device_name or "").lower() or s.name.lower() == (device_name or "").lower():
                return f"the {s.room}" if s.room else s.name
        return device_name or "the other Echo"

    def _tell_why(self, sat, ec: EchoCall, reason: str, peer: str) -> None:
        where = self._room_of(peer)
        if reason == "busy":
            msg = f"{where.capitalize()} is on another call."
        elif reason == "do not disturb":
            msg = f"{where.capitalize()} has do not disturb on for calls."
        elif reason == "declined":
            callee = next((self.echoes.get(s.name) for s in getattr(self.hub, "sats", [])
                           if self.of(s).device_name.lower() == peer.lower()), None)
            missed = callee is not None and callee.incoming[0] == "missed" and time.time() - callee.incoming[1] < 15
            msg = f"Nobody answered in {where}." if missed or callee is None else f"{where.capitalize()} didn't pick up."
        elif reason == "failed":
            ec.live_ok, ec.last_problem = False, "failed"
            msg = (f"I couldn't connect to {where}. Its house word may be missing or different. "
                   f"You can say intercom {where.replace('the ', '')} to leave a message.")
        elif reason == "cancelled":
            return
        else:
            msg = f"The call to {where} didn't go through."
        self.hold(sat, msg)                                   # said once this Echo is back to idle

    # ---- holding sound for an Echo in a call ------------------------------------------------------
    def hold(self, sat, text: str, start_conversation: bool = False, front: bool = False) -> bool:
        """Queue an announcement for this Echo if it's in a call - or its held lines are still being said, so this
        one goes after them (True = queued); if idle, send it now. front = back to the head of the queue."""
        ec = self.of(sat)
        if ec.busy or ec.draining or ec.held:
            # never queue an open-the-mic line: said minutes later, after a call, the mic would open as a plain
            # command turn (the intercom recording it was for is long gone)
            item = (text, False)
            if front:
                ec.held.insert(0, item)
            else:
                ec.held.append(item)
            if not ec.busy and not ec.draining:
                self._release(sat, ec)
            return True
        self._send(sat, text, start_conversation)
        return False

    def _release(self, sat, ec: EchoCall) -> None:
        """The call ended: say what waited, ONE AFTER ANOTHER (each announcement takes the Echo's speaker from the
        one playing - sent together, only the last would be heard), by ONE drain per Echo (a second call that came
        and went inside one line used to start a second drain: lines over each other, out of order). A new call
        stops it with the rest still queued in order; that call ending releases them again."""
        if ec.draining or not ec.held:
            return
        loop = getattr(self.hub, "_loop", None)
        if loop is None or not hasattr(self.hub, "_announce_on"):
            return
        ec.draining = True

        async def drain():
            try:
                while ec.held and not ec.busy:
                    text, sc = ec.held.pop(0)
                    try:
                        r = await self.hub._announce_on(sat, text, sc, force=True, requeue=True)
                    except Exception as exc:                   # one failed line mustn't lose the rest
                        log.warning("%s: held announcement failed: %s", sat.name, exc)
                        continue
                    if r == "held":                            # a call started while it was being synthesized
                        break
            finally:
                ec.draining = False
            if ec.held and not ec.busy:                       # queued just as the loop ended
                self._release(sat, ec)

        asyncio.run_coroutine_threadsafe(drain(), loop)

    def _send(self, sat, text: str, start_conversation: bool = False) -> None:
        loop = getattr(self.hub, "_loop", None)
        if loop is not None and hasattr(self.hub, "_announce_on"):
            asyncio.run_coroutine_threadsafe(self.hub._announce_on(sat, text, start_conversation, force=True), loop)

    # ---- doing things -------------------------------------------------------------------------------
    async def call(self, src, dst) -> dict:
        """Ring dst from src. -> {"ok", "say"} or {"fallback": True, "why"} (no house word / old firmware ...)."""
        s_ec, d_ec = self.of(src), self.of(dst)
        where = f"the {dst.room}" if dst.room else dst.name
        if src is dst:
            return {"ok": False, "say": "That's this Echo. Name another room."}
        if src.client is None or not src.connected:
            return {"ok": False, "say": "This Echo isn't connected right now."}
        if dst.client is None or not dst.connected:
            return {"ok": False, "say": f"The {dst.room or dst.name} Echo isn't connected right now."}
        if s_ec.busy:
            return {"ok": False, "say": "You're already on a call."}
        if d_ec.busy:
            return {"ok": False, "say": f"{where.capitalize()} is already on a call."}
        if d_ec.dnd:
            return {"ok": False, "say": f"{where.capitalize()} has do not disturb on for calls."}
        svc = s_ec.services.get("intercom_call")
        if svc is None:
            return {"fallback": True, "why": "this Echo's firmware has no live intercom"}
        try:
            r = await src.client.execute_service(svc, {"device": d_ec.device_name}, return_response=True, timeout=4)
        except (asyncio.TimeoutError, TimeoutError):
            r = None                                          # unknown: judge by the phone sensor below
        except Exception as exc:
            log.warning("%s: intercom_call failed: %s", src.name, exc)
            return {"fallback": True, "why": str(exc)}
        if r is not None and not getattr(r, "success", True):
            err = str(getattr(r, "error_message", "") or "")
            log.info("%s: intercom_call refused: %s", src.name, err)
            if "already on a call" in err:
                return {"ok": False, "say": "One of the Echoes is already on a call."}
            s_ec.live_ok, s_ec.last_problem = False, err
            return {"fallback": True, "why": err}
        for _ in range(25):                                   # up to 2.5 s for the phone sensor to say so
            if s_ec.phase in ("Calling", "In call"):
                break
            await asyncio.sleep(0.1)
        else:
            if r is None:
                return {"fallback": True, "why": "the call didn't start"}
        log.info("live call %s -> %s", src.name, dst.name)
        return {"ok": True, "say": f"Dropping in on {where}." if d_ec.drop_in else f"Calling {where}."}

    async def press(self, sat, which: str) -> bool:
        """phone_answer / phone_hangup on this Echo."""
        ec = self.of(sat)
        key = ec.keys.get(which)
        if sat.client is not None and key is not None:
            try:
                sat.client.button_command(key)
                return True
            except Exception as exc:
                log.warning("%s: %s failed: %s", sat.name, which, exc)
        svc = ec.services.get(which)
        if sat.client is not None and svc is not None:
            try:
                await sat.client.execute_service(svc, {})
                return True
            except Exception as exc:
                log.warning("%s: %s action failed: %s", sat.name, which, exc)
        return False

    def set_switch(self, sat, which: str, on: bool) -> bool:
        """intercom_drop_in / intercom_do_not_disturb / setup_page."""
        key = self.of(sat).keys.get(which)
        if sat.client is None or key is None:
            return False
        try:
            sat.client.switch_command(key, bool(on))
            return True
        except Exception as exc:
            log.warning("%s: %s -> %s failed: %s", sat.name, which, on, exc)
            return False

    def status(self, sat) -> dict:
        ec = self.of(sat)
        return {"live_intercom": "ok" if ec.live_ok else ("not set up" if ec.live_ok is False else "unknown"),
                "available": "intercom_call" in ec.services, "call_phase": ec.phase, "call_peer": ec.peer,
                "line": ec.line_status,
                "drop_in": ec.drop_in, "dnd": ec.dnd, "problem": ec.last_problem, "dials_as": ec.device_name,
                "setup_url": f"http://{sat.ip}:8181/setup" if getattr(sat, "ip", "") else ""}
