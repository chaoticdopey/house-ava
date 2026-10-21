"""WiZ bulbs over the LAN (UDP 38899, JSON) — no cloud, works in an outage.

Room-aware: a satellite passes its room so "turn the light off" means that room - or every bulb when that room has
none (the kitchen) or there is no room (the app's chat). "all" / "every" / "everything" always means the whole house.
Sentences are read by lightcmd.parse(); this module resolves the target and talks to the bulbs (all at once).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import lightcmd
from .lightcmd import LightCmd, Target

log = logging.getLogger("houseava.lights")

PORT = 38899
COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "white": (255, 255, 255),
    "warm white": (255, 170, 90), "warm": (255, 170, 90), "yellow": (255, 200, 0), "orange": (255, 110, 0),
    "purple": (150, 0, 255), "pink": (255, 60, 180), "cyan": (0, 220, 255), "teal": (0, 180, 160),
    "cool white": (255, 255, 255),
}
_TEMPS = {"warm white": 2700, "warm": 2700, "soft white": 2700, "cool white": 6000, "daylight": 6000}
_ALL_WORDS = ("all", "every", "everything", "everywhere", "whole house", "entire house", "house", "the house",
              "every room", "all rooms", "all the lights", "all lights", "all the", "all of them", "both")
_STEP = 25                                       # dim / brighten step, in percent


def _send(ip: str, payload: dict, timeout: float = 1.2) -> dict | None:
    if os.environ.get("HOUSEAVA_FAKE_BULBS"):      # test runs: never touch the real bulbs (a test once switched the
        return {"method": payload.get("method"), "result": {"success": True, "state": False}}   # bedroom off)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(json.dumps(payload).encode(), (ip, PORT))
        data, _ = s.recvfrom(4096)
        s.close()
        return json.loads(data.decode(errors="ignore"))
    except Exception:
        return None


def _send_all(ips: list[str], payload_for, tries: int = 3) -> dict[str, dict | None]:
    """Send to every bulb at once (one slow/unplugged bulb doesn't hold up the rest); bulbs that didn't answer get
    up to 2 more tries (UDP drops packets - one lost reply used to read as "I couldn't reach the bedroom light").
    payload_for(ip) -> dict."""
    out: dict[str, dict | None] = {}
    todo = list(dict.fromkeys(ips))
    for _ in range(max(1, tries)):
        if not todo:
            break
        with ThreadPoolExecutor(max_workers=min(16, len(todo))) as ex:
            futs = {ip: ex.submit(_send, ip, payload_for(ip)) for ip in todo}
            for ip, f in futs.items():
                out[ip] = f.result()
        todo = [ip for ip in todo if not out.get(ip)]
    return out


def _when(seconds: int) -> str:
    """10 -> "10 seconds", 600 -> "10 minutes", 11274 -> "3 hours and 8 minutes" (never "11274 seconds")."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    mins = (seconds + 30) // 60
    h, m = divmod(mins, 60)
    hs = f"{h} hour{'s' if h != 1 else ''}" if h else ""
    ms = f"{m} minute{'s' if m != 1 else ''}" if m else ""
    return " and ".join(x for x in (hs, ms) if x)


class Lights:
    def __init__(self, cfg):
        self.cfg = cfg
        self._timers: list[threading.Timer] = []

    def bulbs(self) -> dict[str, str]:
        return dict(self.cfg.get("lights.bulbs", {}) or {})

    def rooms(self) -> dict[str, list]:
        return {k.lower(): v for k, v in (self.cfg.get("lights.rooms", {}) or {}).items()}

    def aliases(self) -> dict:
        return dict(self.cfg.get("lights.aliases", {}) or {})

    # ---- reading a sentence ----
    def parse(self, text: str, plugs=None) -> LightCmd | None:
        return lightcmd.parse(text, self.rooms(), self.bulbs(), plugs or set(), self.aliases())

    RECENT_S = 180                               # "turn them off" = the lights from a command this recent (same room)

    def parse_all(self, text: str, plugs=None, room: str = "") -> list[LightCmd]:
        """Light commands in a sentence. "it/them" (Target kind "ref") = the lights of a light command said in this
        room in the last 3 minutes; without one, that clause isn't a light command (it may be the music)."""
        cmds = lightcmd.parse_all(text, self.rooms(), self.bulbs(), plugs or set(), self.aliases())
        out = []
        for c in cmds:
            if c.target.other_one:              # "turn on the other one" after "turn off bedroom light one"
                tg, at, _ = (getattr(self, "_recent", {}).get((room or "").lower(), (None, 0.0, "")) + ("",))[:3]
                if tg is not None and time.time() - at <= self.RECENT_S and tg.kind == "bulbs":
                    c.target = lightcmd._other_bulbs(tg.bulbs, self.rooms(), self.bulbs())
            if c.target.kind == "ref":
                tg, at, last = (getattr(self, "_recent", {}).get((room or "").lower(), (None, 0.0, "")) + ("",))[:3]
                temp = re.search(lightcmd._TEMP_RX, c.text)
                if tg is None or time.time() - at > self.RECENT_S:
                    # nothing said lately: "make it darker" / "make it blue" can only be this room's lights (the music
                    # isn't darker or blue); "turn it off" / "turn it up" could be anything, and "make it warmer /
                    # cooler" is the heat -> not ours
                    if (c.action == "color" and not temp) or \
                            re.search(r"\b(?:darker|brighter|dimmer|dim|brighten|lighter)\b", c.text):
                        c.target = Target()             # (plain HERE: "make it brighter" may light the 2nd lamp)
                        out.append(c)
                    continue
                if last == "off" and c.action == "color" and temp:
                    continue                        # "turn off the bedroom lights" ... "make it warmer" = the heat
                ph = c.target                       # "turn them off but not the bedroom": keep that exclusion
                c.target = copy.deepcopy(tg)        # (a copy: the remembered target must never be edited)
                bulbs = self.bulbs()
                if ph.minus or ph.except_here or ph.bulbs:
                    lightcmd._leave_out(c.target, [x for x in ph.minus if x not in bulbs],
                                        [x for x in ph.minus + ph.bulbs if x in bulbs], ph.except_here)
                if ph.minus_nums:                   # "turn them off except light one" (resolve binds the room)
                    c.target.minus_nums = list(ph.minus_nums)
                c.was_ref = True
            out.append(c)
        return out

    def run_all(self, cmds: list[LightCmd], room: str = "") -> dict:
        """Several commands from one sentence: do each, say each result."""
        if len(cmds) == 1:
            return self.run(cmds[0], room)
        res = [self.run(c, room) for c in cmds]
        say = " ".join(r.get("say", "").strip() for r in res if r.get("say"))
        return {"ok": any(r.get("ok") for r in res), "say": say}

    # ---- who's being talked about ----
    def _here(self, room: str) -> str | None:
        """The caller's room if it has bulbs (else None = the whole house)."""
        r = (room or "").lower().strip()
        rooms = self.rooms()
        return r if r and [n for n in rooms.get(r, []) if n in self.bulbs()] else None

    def resolve(self, tg: Target, room: str = "") -> tuple[list[tuple[str, str]], str, str]:
        """(name, ip) pairs, a spoken label ("the bedroom lights"), and an error to say instead (or "")."""
        b, rooms = self.bulbs(), self.rooms()
        known = ", ".join(sorted(rooms)) or "none"
        if tg.ask:
            return [], "", "Did you mean the " + " or the ".join(tg.ask) + "?"

        def of_rooms(rs):
            seen: dict[str, str] = {}
            for r in rs:
                for n in rooms.get(r, []):
                    if n in b:
                        seen[n] = b[n]
            return list(seen.items())

        own = self._here(room)
        if tg.minus_nums:                       # "... except light one": a bulb of the room named, or of HERE
            base = tg.rooms[0] if tg.kind == "rooms" and len(tg.rooms) == 1 else own
            if not base:
                n = tg.minus_nums[0]
                return [], "", f"Which room's light {n}? Say something like bedroom light {n}."
            extra = [f"{base} {n}" for n in tg.minus_nums if f"{base} {n}" in b]
            tg = Target(**{**copy.deepcopy(tg).__dict__, "minus_nums": [], "minus": list(tg.minus) + extra})
        if tg.place and tg.kind == "here" and not own:
            # "the lamp by the window" / "the table lamp" said where there are no lights: not every bulb in the house
            return [], "", f"I don't know which light that is. The rooms with lights are {known}."
        if tg.other_one:                        # "the other lamp" with no bulb named before it: which one?
            mine = [n for n in rooms.get(own, []) if n in b] if own else []
            return [], "", (f"Which one? Say {' or '.join(mine)}." if mine else
                            "Which light do you mean? Say something like bedroom light two.")
        if tg.kind == "here" and any(x in rooms for x in tg.minus) and not tg.after_off and not tg.here_said:
            # "turn off the lights but leave the bedroom on": naming a room to keep means the WHOLE house minus it
            # (said in the bedroom, here minus here was nothing; from the living room it switched only the living
            # room) - the same as "turn off the lights except the bedroom"
            return self.resolve(Target(kind="except", rooms=[x for x in tg.minus if x in rooms],
                                       bulbs=[x for x in tg.minus if x not in rooms], except_here=tg.except_here), room)
        if (tg.minus or tg.except_here) and tg.kind in ("all", "rooms", "here"):
            # "... but not the dining room" / "... but leave bedroom 2 on" (a bulb) / "... but not in here"
            inner = Target(**{**tg.__dict__, "minus": [], "except_here": False})
            t, label, err = self.resolve(inner, room)
            if err:
                return t, label, err
            here = self._here(room) if tg.except_here else None
            drop = list(tg.minus) + ([here] if here and here not in tg.minus else [])
            out = [(n, ip) for n, ip in t if n not in drop and not any(n in rooms.get(r, []) for r in drop)]
            names = [f"the {x}" if x in rooms else x for x in drop]
            label = label if len(out) == len(t) else (label + " except " + " and ".join(names))
            return out, label, ("" if out else "That leaves no lights.")
        if tg.kind == "all":
            return list(b.items()), "all the lights", ""
        if tg.kind == "except":
            skip_rooms = list(tg.rooms)
            if tg.except_here:
                here = self._here(room)
                if here:
                    skip_rooms.append(here)
            out = [(n, ip) for n, ip in b.items()
                   if not any(n in rooms.get(r, []) for r in skip_rooms) and n not in tg.bulbs]
            what = " and ".join([f"the {r}" for r in skip_rooms] + list(tg.bulbs)) or "nothing"
            label = "the other lights" if tg.except_here else f"all the lights except {what}"
            return out, label, ("" if out else "That's all of them.")
        if tg.kind == "rooms":
            if not tg.rooms:
                place = tg.unknown or "there"
                where = place if re.fullmatch(r"upstairs|downstairs|next door|across the hall|down the hall|outside|"
                                              r"out back|out front|there", place) or \
                    re.match(r"(?:by|next to|beside|near|over|above|under|underneath|below|behind|on)\b", place) \
                    else f"in the {place.removeprefix('the ')}"
                return [], "", f"I don't have any lights {where}. The rooms with lights are {known}, or say all the lights."
            here = self._here(room) if tg.with_here else None          # "this room and the bedroom"
            rs = list(tg.rooms) + ([here] if here and here not in tg.rooms else [])
            t = of_rooms(rs)
            if tg.count and t:
                t = t[:1]
            label = t[0][0] if len(t) == 1 else "the " + " and ".join(rs) + " lights"
            return t, label, ("" if t else f"The {' and '.join(rs)} has no lights set up.")
        if tg.kind == "bulbs":
            t = [(n, b[n]) for n in tg.bulbs if n in b]
            missing = [n for n in tg.bulbs if n not in b]
            if missing and not t:
                r = missing[0].rsplit(" ", 1)[0]
                have = [n.rsplit(" ", 1)[-1] for n in rooms.get(r, []) if n in b]
                if have:
                    return [], "", f"The {r} only has light{'s' if len(have) > 1 else ''} {' and '.join(have)}."
                return [], "", f"I don't have a light called {missing[0]}."
            return t, (t[0][0] if len(t) == 1 else " and ".join(n for n, _ in t)), ""
        # here
        here = self._here(room)
        if tg.nums:
            if not here:
                return [], "", (f"Which room's light {tg.nums[0]}? Say something like bedroom light {tg.nums[0]}. "
                                f"The rooms with lights are {known}.")
            names = [f"{here} {n}" for n in tg.nums]
            t = [(n, b[n]) for n in names if n in b]
            if not t:
                have = [n.rsplit(" ", 1)[-1] for n in rooms.get(here, []) if n in b]
                return [], "", f"The {here} only has light{'s' if len(have) > 1 else ''} {' and '.join(have)}."
            return t, (t[0][0] if len(t) == 1 else " and ".join(n for n, _ in t)), ""
        if here:
            t = of_rooms([here])
            if tg.count:
                t = t[:1]
                return t, t[0][0], ""
            return t, f"the {here} lights", ""
        t = list(b.items())
        if tg.count and t:
            return t[:1], t[0][0], ""
        return t, "all the lights", ("" if t else "No lights are set up.")

    def target_from_words(self, which: str, room: str = "") -> Target:
        """A routine's / the model's target words ("all", "bedroom", "bedroom 2", "living room and dining room",
        "", "kitchen") -> a Target with the same meaning a spoken sentence would have."""
        w = lightcmd.normalize(which or "")
        w = re.sub(rf"\b{lightcmd.LIGHT_NOUN}\b", " ", w)
        w = re.sub(r"^(?:the|my|our|your|a)\s+", "", re.sub(r"\s+", " ", w).strip())
        if (room and w and w == room.lower().strip() and not self._here(room)):
            w = ""                                  # the model filled in the caller's own lightless room
        if not w or w in ("light", "lights", "here", "in here", "this room"):
            return Target(kind="here")
        if w in _ALL_WORDS or re.fullmatch(r"(?:all|every|everything|everywhere|whole house|entire house)(?: the)?(?: house| home)?", w):
            return Target(kind="all")
        pairs = lightcmd._aliases(self.rooms(), self.aliases())
        rooms = lightcmd._find_rooms(w, pairs)
        all_word = bool(re.search(r"\b(?:all|every|everything)\b", w))
        return lightcmd._target(w + " lights", pairs, self.rooms(), self.bulbs(), all_word, rooms)

    def targets(self, which: str, room: str = "") -> list[tuple[str, str]]:
        t, _, _ = self.resolve(self.target_from_words(which, room), room)
        return t

    # ---- doing it ----
    def run(self, cmd: LightCmd, room: str = "") -> dict:
        if cmd.action == "status":
            return self.status(cmd.target, room, cmd.text)
        if cmd.action == "refuse":              # "when the movie ends" / "at sunset": nothing switched now
            return {"ok": False, "say": "I can only time the lights by the clock - like in 10 minutes, or at 7. "
                                        "Nothing's changed."}
        t, label, err = self.resolve(cmd.target, room)
        if err:
            return {"ok": False, "say": err}
        if cmd.delay:
            later = LightCmd(cmd.action, cmd.target, cmd.percent, cmd.color, 0, cmd.duration, cmd.text)
            tm = threading.Timer(cmd.delay, lambda: self._later(later, room))
            tm.daemon = True
            tm.start()
            self._timers.append(tm)
            verb = {"on": "turn on", "off": "turn off", "toggle": "switch", "set": "set", "dim": "dim",
                    "brighten": "brighten", "color": "change"}.get(cmd.action, cmd.action)
            when = time.strftime("%I:%M %p", time.localtime(time.time() + cmd.delay)).lstrip("0")
            extra = f" to {cmd.percent} percent" if cmd.action == "set" else (f" to {cmd.color}" if cmd.action == "color" else "")
            return {"ok": True, "say": f"Okay, I'll {verb} {label}{extra} in {_when(cmd.delay)}, at {when}."}
        ips = [ip for _, ip in t]
        before = None
        if cmd.duration:
            before = self._read(ips)
        res = self._do(cmd, ips, label, room)
        if res.get("ok"):                                   # "turn them off" in the next 3 minutes = these lights
            if not hasattr(self, "_recent"):
                self._recent = {}
            # (target, when, what was done - after an "off", "make it warmer" is the heat, not these lights)
            self._recent[(room or "").lower()] = (copy.deepcopy(cmd.target), time.time(), res.pop("did", cmd.action))
        if cmd.duration and res.get("ok"):
            tm = threading.Timer(cmd.duration, lambda: self._put_back(before or {}))
            tm.daemon = True
            tm.start()
            self._timers.append(tm)
            res["say"] = res["say"].rstrip(".") + f" for {_when(cmd.duration)}."
        return res

    def _later(self, cmd: LightCmd, room: str) -> None:
        try:
            r = self.run(cmd, room)
            log.info("timed light command %s -> %s", cmd.text, r.get("say"))
        except Exception:
            log.exception("timed light command failed")

    def _read(self, ips: list[str]) -> dict[str, dict]:
        got = _send_all(ips, lambda ip: {"method": "getPilot"})
        return {ip: (r or {}).get("result") for ip, r in got.items() if isinstance(r, dict) and r.get("result")}

    def _flash(self, ips: list[str], label: str) -> dict:
        """Blink the bulb(s) 3 times, then put them back - so you can see which one is which."""
        before = self._read(ips)
        if not before:
            return {"ok": False, "say": f"I couldn't reach {label}."}

        def blink():
            for _ in range(3):
                _send_all(list(before), lambda ip: {"method": "setPilot", "params": {"state": True, "dimming": 100,
                                                                                    "r": 255, "g": 255, "b": 255}}, 1)
                time.sleep(0.45)
                _send_all(list(before), lambda ip: {"method": "setPilot", "params": {"state": False}}, 1)
                time.sleep(0.45)
            self._put_back(before)
        threading.Thread(target=blink, daemon=True).start()
        return {"ok": True, "say": f"Flashing {label}."}

    def _lost_note(self, got: dict) -> str:
        """Which bulbs didn't answer, said usefully: a whole room silent is almost always its wall switch (10-19: both
        living room bulbs were off the network - "1 of them didn't answer" told him nothing)."""
        lost = {ip for ip, r in got.items() if not r}
        if not lost:
            return ""
        b = self.bulbs()
        dead = [r for r, ns in self.rooms().items() if [n for n in ns if n in b] and all(b[n] in lost for n in ns if n in b)]
        if dead:
            return f" The {' and '.join(dead)} lights didn't answer - is the wall switch off?"
        names = {ip: n for n, ip in b.items()}
        return f" ({', '.join(sorted(names.get(ip, ip) for ip in lost))} didn't answer.)"

    def _do(self, cmd: LightCmd, ips: list[str], label: str, room: str = "") -> dict:
        from . import quips
        a = cmd.action
        if a == "flash":
            return self._flash(ips, label)
        if a == "toggle":
            states = self._read(ips)
            any_on = any(bool(s.get("state")) for s in states.values())
            a = "off" if any_on else "on"
        if a in ("on", "off"):
            on = a == "on"
            got = _send_all(ips, lambda ip: {"method": "setPilot", "params": {"state": on}})
            ok = sum(1 for r in got.values() if r)
            if not ok:
                return {"ok": False, "say": f"I couldn't reach {label} - is the wall switch off?"}
            say = quips.light_confirm(label, on, self.cfg)
            if ok < len(ips):
                say = say.rstrip(".") + "." + self._lost_note(got)
            return {"ok": True, "say": say, "did": a}
        if a == "set":
            pct = max(10, min(100, int(cmd.percent or 50)))
            got = _send_all(ips, lambda ip: {"method": "setPilot", "params": {"state": True, "dimming": pct}})
            ok = sum(1 for r in got.values() if r)
            return {"ok": ok > 0, "say": f"Set {label} to {pct} percent." if ok else f"I couldn't reach {label}."}
        if a in ("dim", "brighten"):
            states = self._read(ips)

            step = int(getattr(cmd, "step", 0) or 0) or _STEP          # "brighten the lights by 20 percent"

            def new_level(ip):
                s = states.get(ip) or {}
                cur = int(s.get("dimming") or 100) if s.get("state") else 0
                if a == "dim":
                    return max(10, (cur or 100) - step) if cur else 30
                return min(100, (cur or 0) + step) if cur else 100

            if not states:
                return {"ok": False, "say": f"I couldn't reach {label}."}
            # only the lights that are ON (the 10-16 review: "make it darker" from the kitchen switched four dark bulbs
            # ON at 30%). All off: brighter turns them on; "dim the bedroom lights" (a room named in THIS sentence) or
            # "turn the lights on dim" is a 30% night light; a darker / too-bright remark, or "it" meaning lights
            # that were just switched off, has nothing to do
            Label = label[:1].upper() + label[1:]
            are = "is" if len(ips) == 1 else "are"
            on_ips = [ip for ip in ips if (states.get(ip) or {}).get("state")]
            known = [ip for ip in ips if states.get(ip)]
            named = cmd.target.kind in ("rooms", "bulbs", "except") and not cmd.was_ref and not cmd.inherited
            mine = named or (cmd.target.kind == "here" and self._here(room) is not None and not cmd.inherited)
            levels = None
            if a == "dim" and getattr(cmd, "turn_on", False):
                use = known                     # "turn on the lights dim": ALL of them on, at the night-light level
                levels = {ip: 30 for ip in use}
            elif on_ips:
                use = on_ips
                if a == "brighten" and all(int((states.get(ip) or {}).get("dimming") or 100) >= 100 for ip in on_ips):
                    off = [ip for ip in known if ip not in on_ips]
                    if off and mine:
                        use = on_ips + off      # "brighter" with one of two bulbs on at full: light the other one
                    elif off:
                        return {"ok": True, "say": "The ones that are on are already at full brightness."}
                    else:
                        return {"ok": True, "say": f"{Label} {are} already at full brightness."}
            elif a == "brighten" or named:
                use = known
            else:
                return {"ok": True, "say": f"{Label} {are} already off."}
            if set(use) != set(ips):            # say what really changed, not "all the lights"
                names = {ip: n for n, ip in self.bulbs().items()}
                label = " and ".join(names.get(ip, ip) for ip in use) if len(use) <= 2 else \
                    f"the {len(use)} lights that were on"
            levels = levels or {ip: new_level(ip) for ip in use}
            got = _send_all(use, lambda ip: {"method": "setPilot", "params": {"state": True, "dimming": levels[ip]}})
            ok = sum(1 for r in got.values() if r)
            if not ok:
                return {"ok": False, "say": f"I couldn't reach {label}."}
            lv = sorted(set(levels[ip] for ip in use if got.get(ip)))
            at = f"{lv[0]} percent" if len(lv) == 1 else "a bit " + ("lower" if a == "dim" else "higher")
            return {"ok": True, "say": f"{'Dimmed' if a == 'dim' else 'Brightened'} {label} to {at}."}
        if a == "color":
            name = (cmd.color or "").lower()
            if name in _TEMPS:
                params = {"state": True, "temp": _TEMPS[name]}
            else:
                rgb = COLORS.get(name)
                if not rgb:
                    return {"ok": False, "say": f"I don't know the color {cmd.color}."}
                params = {"state": True, "r": rgb[0], "g": rgb[1], "b": rgb[2]}
            got = _send_all(ips, lambda ip: {"method": "setPilot", "params": params})
            ok = sum(1 for r in got.values() if r)
            return {"ok": ok > 0, "say": f"Set {label} to {cmd.color}." if ok else f"I couldn't reach {label}."}
        return {"ok": False, "say": "I'm not sure what to do with the lights."}

    def _put_back(self, before: dict[str, dict]) -> None:
        for ip, p in before.items():
            _send(ip, {"method": "setPilot", "params": self._pilot_params(p)})

    @staticmethod
    def _pilot_params(p: dict) -> dict:
        out = {"state": bool(p.get("state", True))}
        if not out["state"]:
            return out
        if p.get("dimming") is not None:
            out["dimming"] = p["dimming"]
        if p.get("sceneId"):
            out["sceneId"] = p["sceneId"]
        elif p.get("temp"):
            out["temp"] = p["temp"]
        else:
            for k in ("r", "g", "b", "c", "w"):
                if k in p:
                    out[k] = p[k]
        return out

    def status(self, tg: Target, room: str = "", text: str = "") -> dict:
        """"Is the bedroom light on?" -> about the bedroom; "what lights are on?" / "are the lights on?" -> the whole
        house, room by room; "how bright is the living room?" / "what color is the bedroom light?" -> the level /
        colour of the ones that are on (only reads the bulbs, never changes them)."""
        # ("are the lights on in here?" = this room, not the whole house)
        specific = tg.kind != "here" or bool(tg.nums) or bool(tg.ask) or bool(getattr(tg, "here_said", False))
        if specific:
            t, label, err = self.resolve(tg, room)
            if err:
                return {"ok": False, "say": err}
        else:
            t, label = list(self.bulbs().items()), "all the lights"
        if not t:
            return {"ok": False, "say": "No lights are set up."}
        states = self._read([ip for _, ip in t])
        if not states:
            return {"ok": False, "say": "None of the lights are answering right now."}
        rooms = self.rooms()
        answered = [(n, ip) for n, ip in t if ip in states]
        on = [n for n, ip in answered if states[ip].get("state")]
        lost = [n for n, ip in t if ip not in states]

        def pretty(names: list[str]) -> tuple[str, bool]:       # ("both bedroom lights and living room 1", plural?)
            parts, plural = [], len(names) > 1
            by: dict[str, list] = {}
            for n in names:
                by.setdefault(next((r for r, ns in rooms.items() if n in ns), n), []).append(n)
            for r, ns in by.items():
                whole = [x for x in rooms.get(r, []) if x in dict(t)]
                if len(ns) == len(whole) and len(whole) > 1:
                    parts.append(f"both {r} lights" if len(whole) == 2 else f"the {r} lights")
                else:
                    parts.extend(ns)
            return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1], plural

        cap = lambda s: s[:1].upper() + s[1:]                    # noqa: E731
        one = len(t) == 1
        want = "color" if re.search(r"\bcolou?r\b", text or "") else \
            "level" if re.search(r"\b(?:how bright|how dim|brightness|level|percent|how bright)\b", text or "") else ""
        if want and on:
            def color_of(st: dict) -> str:
                if st.get("temp"):
                    k = int(st["temp"])
                    return "warm white" if k <= 3000 else "soft white" if k <= 4200 else "daylight white"
                if all(c in st for c in ("r", "g", "b")):
                    rgb = (st["r"], st["g"], st["b"])
                    return min(COLORS, key=lambda n: sum((a - b) ** 2 for a, b in zip(COLORS[n], rgb)))
                return "a scene" if st.get("sceneId") else "white"
            vals = {n: (color_of(states[ip]) if want == "color" else f"{states[ip].get('dimming', 100)} percent")
                    for n, ip in answered if states[ip].get("state")}
            if len(set(vals.values())) == 1:
                p, pl = pretty(list(vals))
                v = next(iter(vals.values()))
                say = f"{cap(p)} {'are' if pl else 'is'} {'at ' if want == 'level' else ''}{v}."
            else:
                say = ", ".join(f"{n} {'at ' if want == 'level' else ''}{v}" for n, v in vals.items())
                say = cap(say) + "."
            off = [n for n, ip in answered if not states[ip].get("state")]
            if off and specific:
                p, pl = pretty(off)
                say += f" {cap(p)} {'are' if pl else 'is'} off."
            return {"ok": True, "say": say}
        if specific:
            if not on:
                say = f"{cap(label)} {'is' if one else 'are'} off."
            elif len(on) == len(answered):
                say = f"{cap(label)} {'is' if one else 'are'} on."
            else:
                p, pl = pretty(on)
                say = f"Only {p} {'are' if pl else 'is'} on."
        else:
            if not on:
                say = "All the lights are off."
            elif len(on) == len(answered):
                say = "Every light is on."
            else:
                p, pl = pretty(on)
                say = f"{cap(p)} {'are' if pl else 'is'} on. Everything else is off."
        if lost:
            say += f" ({', '.join(lost)} didn't answer.)"
        return {"ok": True, "say": say}

    # ---- the older entry points (routines, the model's intent) - same meanings as a spoken sentence ----
    def set_state(self, which: str, on: bool, room: str = "") -> dict:
        return self.run(LightCmd("on" if on else "off", self.target_from_words(which, room)), room)

    def set_brightness(self, which: str, pct: int, room: str = "") -> dict:
        return self.run(LightCmd("set", self.target_from_words(which, room), percent=int(pct)), room)

    def set_color(self, which: str, color: str, room: str = "") -> dict:
        c = lightcmd.COLORS.get((color or "").lower().strip(), (color or "").lower().strip())
        return self.run(LightCmd("color", self.target_from_words(which, room), color=c), room)

    def panic_red(self) -> dict:
        """Save every bulb's current state (for "back to normal"), then set them all bright red at 100%."""
        b = self.bulbs()
        if not b:
            return {"ok": False, "say": "No lights are set up."}
        self._saved = self._read(list(b.values()))
        params = {"method": "setPilot", "params": {"state": True, "r": 255, "g": 0, "b": 0, "dimming": 100}}
        got = _send_all(list(b.values()), lambda ip: params)
        ok = sum(1 for r in got.values() if r)
        return {"ok": ok > 0, "lit": ok, "total": len(b)}

    def restore(self) -> dict:
        """Put every bulb back to the color/brightness it had before the intruder alert."""
        saved = getattr(self, "_saved", None)
        if not saved:
            return {"ok": False, "say": "I don't have a saved look to go back to."}
        got = _send_all(list(saved), lambda ip: {"method": "setPilot", "params": self._pilot_params(saved[ip])})
        self._saved = {}
        return {"ok": sum(1 for r in got.values() if r) > 0}

    def _clean(self, which: str) -> str:            # (kept for callers that format names)
        w = lightcmd.normalize(which or "all")
        w = re.sub(r"^(?:the|my|our|your|a)\s+", "", w)
        w = re.sub(rf"\s+{lightcmd.LIGHT_NOUN}$", "", w).strip(" ,")
        return w

    def _unknown(self, which: str) -> str:
        names = ", ".join(sorted(self.rooms())) or "none set up"
        return f"I don't have a light called {self._clean(which)}. The rooms I know are {names}."
