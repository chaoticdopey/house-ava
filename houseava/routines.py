"""Routines - Alexa-style automations, made in the House Ava app (phone or PC), run by the hub.

A routine = a trigger + optional limits + actions, in order:

  trigger  voice     {"type": "voice", "phrases": ["good night", "goodnight"]}   said to an Echo (with the wake word) or
                     typed in the app's Chat. "run <routine name>" works for every routine.
           time      {"type": "time", "at": "07:00", "days": [0, 1, 2, 3, 4]}     days: Mon=0 .. Sun=6, [] = every day
           sun       {"type": "sun", "event": "sunset", "offset": -15, "days": []}  offset in minutes (- = before)
           presence  {"type": "presence", "room": "kitchen", "cooldown": 10}     someone shows up on that Echo's sensor
                                                                                  ("" = any Echo); not again for N min
           manual    {"type": "manual"}                                           only from the app's Run button
  limits   {"between": ["sunset", "06:00"], "days": [5, 6]}   optional, for the automatic triggers (time / sun / presence):
           times are "HH:MM", "sunrise" or "sunset", and may wrap past midnight
  actions  lights {target, on} / brightness {target, percent} / color {target, color} / plug {name, on} /
           say {text, where} / weather {when, where} / time {where} / music {query, where} / music_stop {where} /
           volume {percent, where} / wait {seconds} / security {arm} / ask {text, where}
           where = "" (the Echo it was said to / the room it's about, else every Echo), "all", or a room
  reply    what she says when it runs by voice and no action said anything (default "Okay.")

By voice, everything up to the first wait runs at once and whatever it says IS her reply (so she never talks over
herself); after a wait the rest runs in the background and speaks as announcements. By time / sun / motion / the app,
all of it runs in the background, speaking as announcements. Routines missed while the hub was down are skipped
(like Alexa). Stored in <data_dir>/routines.json.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import math
import re
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger("houseava.routines")

TRIGGERS = ("voice", "time", "sun", "presence", "manual")
ACTIONS = ("lights", "brightness", "color", "plug", "say", "weather", "time", "music", "music_stop", "volume", "wait",
           "security", "ask")
MAX_ACTIONS = 30
MAX_WAIT = 4 * 3600
# Where you are, for sunrise / sunset triggers: set location.lat / location.lon in the config. Not set = the
# sun triggers and "between sunset and sunrise" limits just don't fire.
DEFAULT_LAT, DEFAULT_LON = None, None

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_LEAD = re.compile(r"^(?:(?:alexa|ava|hey ava|hey|ok|okay|so|um|uh|alright|all right|well|right)\s+)*(?:(?:can|could|would|will) you\s+|please\s+|"
                   r"go ahead and\s+)?")
_TAIL = re.compile(r"(?:\s+(?:please|now|for me|thanks|thank you|ava|alexa|hey ava|everyone|everybody|guys|y ?all|you guys))+$")
# "run / start / activate / trigger <name>" (not "play": "play christmas" is music, even with a routine called Christmas)
_RUN = re.compile(r"^(?:run|start|activate|trigger)\s+(?P<name>.+)$")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def phrase_key(text: str) -> str:
    """'Alexa, good night!' / 'good night please' -> 'good night' (what a spoken phrase is matched on). Apostrophes go
    (whisper writes "I'm home", a phone keyboard "im home")."""
    t = (text or "").lower().replace(chr(0x2019), "").replace("'", "")
    t = " ".join(_NON_WORD.sub(" ", t).split())
    return _TAIL.sub("", _LEAD.sub("", t)).strip()


def name_key(text: str) -> str:
    """A routine's name as "run <name>" would say it: 'The Morning Routine' -> 'morning' (same on both sides)."""
    t = phrase_key(text)
    t = re.sub(r"^(?:the|my)\s+", "", t)
    return re.sub(r"\s+routine$", "", t).strip()


def _hhmm(s) -> int | None:
    m = _HHMM.match(str(s or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def sun_times(day: dt.date, lat: float, lon: float) -> tuple[int | None, int | None]:
    """(sunrise, sunset) for `day` at lat/lon, in local minutes after midnight (NOAA's equations, about a minute out).
    None when the sun doesn't rise or set that day."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g)
                       - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    c = math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl)) - math.tan(la) * math.tan(decl)
    if not -1.0 <= c <= 1.0:
        return None, None
    ha = math.degrees(math.acos(c))
    noon = dt.datetime(day.year, day.month, day.day, 12).astimezone()
    off = noon.utcoffset().total_seconds() / 60 if noon.utcoffset() else 0.0
    rise = 720 - 4 * (lon + ha) - eqtime + off
    sets = 720 - 4 * (lon - ha) - eqtime + off
    return int(round(rise)) % 1440, int(round(sets)) % 1440


def clock(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    h, m = divmod(int(minutes) % 1440, 60)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


# ---- checking what the app sends ----------------------------------------------------------------------------------
class Invalid(ValueError):
    pass


def _days(v) -> list[int]:
    if v in (None, ""):
        return []
    if not isinstance(v, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in v):
        raise Invalid("days must be a list of 0 (Monday) to 6 (Sunday)")
    return sorted(set(v))


def _text(v, what: str, most: int, need: bool = True) -> str:
    s = " ".join(str(v or "").split())
    if need and not s:
        raise Invalid(f"{what} is empty")
    if len(s) > most:
        raise Invalid(f"{what} is too long (most {most} characters)")
    return s


def _int(v, what: str, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        raise Invalid(f"{what} must be a number") from None
    if not lo <= n <= hi:
        raise Invalid(f"{what} must be {lo} to {hi}")
    return n


def _when(v, what: str) -> str:
    s = str(v or "").strip().lower()
    if s in ("sunrise", "sunset") or _hhmm(s) is not None:
        return s
    raise Invalid(f"{what} must be a time (HH:MM), sunrise or sunset")


def clean(data: dict, colors=(), plugs=()) -> dict:
    """A routine as the app sent it -> a checked, normalized routine (raises Invalid with what's wrong)."""
    if not isinstance(data, dict):
        raise Invalid("not a routine")
    r = {"id": str(data.get("id") or secrets.token_hex(4))[:16],
         "name": _text(data.get("name"), "the name", 60),
         "enabled": bool(data.get("enabled", True)),
         "reply": _text(data.get("reply"), "the reply", 200, need=False)}
    tr = data.get("trigger") or {}
    if not isinstance(tr, dict):
        raise Invalid("pick what starts it")
    kind = str(tr.get("type", ""))
    if kind not in TRIGGERS:
        raise Invalid("pick what starts it")
    if kind == "voice":
        raw = tr.get("phrases")
        raw = raw if isinstance(raw, list) else str(raw or "").split(",")
        phrases = []
        for p in raw:
            k = phrase_key(str(p))
            if k and k not in phrases:
                if len(k) > 60:
                    raise Invalid("a phrase is too long")
                phrases.append(k)
        if not phrases:
            raise Invalid("say what you'll say to start it")
        r["trigger"] = {"type": "voice", "phrases": phrases[:10]}
    elif kind == "time":
        at = str(tr.get("at", "")).strip()
        if _hhmm(at) is None:
            raise Invalid("pick a time")
        h, m = divmod(_hhmm(at), 60)
        r["trigger"] = {"type": "time", "at": f"{h:02d}:{m:02d}", "days": _days(tr.get("days"))}
    elif kind == "sun":
        ev = str(tr.get("event", "")).lower()
        if ev not in ("sunrise", "sunset"):
            raise Invalid("pick sunrise or sunset")
        r["trigger"] = {"type": "sun", "event": ev, "offset": _int(tr.get("offset", 0) or 0, "the offset", -240, 240),
                        "days": _days(tr.get("days"))}
    elif kind == "presence":
        r["trigger"] = {"type": "presence", "room": _text(tr.get("room"), "the room", 40, need=False).lower(),
                        "cooldown": _int(tr.get("cooldown", 10) or 10, "the wait before it can run again", 1, 1440)}
    else:
        r["trigger"] = {"type": "manual"}
    lim = data.get("limits") or {}
    if not isinstance(lim, dict):
        raise Invalid("the 'only' limits aren't right")
    limits = {}
    between = lim.get("between")
    if between:
        if not isinstance(between, list) or len(between) != 2:
            raise Invalid("'only between' needs a start and an end")
        limits["between"] = [_when(between[0], "the start"), _when(between[1], "the end")]
    if lim.get("days"):
        limits["days"] = _days(lim.get("days"))
    r["limits"] = limits
    acts = data.get("actions")
    if not isinstance(acts, list) or not acts:
        raise Invalid("add at least one action")
    if len(acts) > MAX_ACTIONS:
        raise Invalid(f"at most {MAX_ACTIONS} actions")
    r["actions"] = [_action(a, i + 1, colors, plugs) for i, a in enumerate(acts)]
    for k in ("last_run", "last_source"):
        if k in data:
            r[k] = data[k]
    return r


def _action(a, n: int, colors=(), plugs=()) -> dict:
    if not isinstance(a, dict) or a.get("type") not in ACTIONS:
        raise Invalid(f"action {n}: pick what it does")
    t = a["type"]
    out: dict = {"type": t}
    where = _text(a.get("where"), f"action {n}'s Echo", 40, need=False).lower()
    if t in ("lights", "brightness", "color"):
        out["target"] = _text(a.get("target") or "all", f"action {n}'s lights", 40).lower()
    if t == "lights":
        out["on"] = bool(a.get("on", True))
    elif t == "brightness":
        out["percent"] = _int(a.get("percent", 50), f"action {n}'s brightness", 1, 100)
    elif t == "color":
        c = str(a.get("color", "")).strip().lower()
        if colors and c not in colors:
            raise Invalid(f"action {n}: pick a color")
        out["color"] = c
    elif t == "plug":
        name = str(a.get("name", "")).strip().lower()
        if not name or (plugs and name not in plugs):
            raise Invalid(f"action {n}: pick a plug")
        out["name"], out["on"] = name, bool(a.get("on", True))
    elif t == "say":
        out["text"], out["where"] = _text(a.get("text"), f"action {n}'s words", 300), where
    elif t == "weather":
        out["when"] = "tomorrow" if str(a.get("when", "")).lower() == "tomorrow" else "today"
        out["where"] = where
    elif t in ("time", "music_stop"):
        out["where"] = where
    elif t == "music":
        out["query"], out["where"] = _text(a.get("query"), f"action {n}'s music", 100), where
    elif t == "volume":
        out["percent"], out["where"] = _int(a.get("percent", 50), f"action {n}'s volume", 0, 100), where
    elif t == "wait":
        out["seconds"] = _int(a.get("seconds", 60), f"action {n}'s wait", 1, MAX_WAIT)
    elif t == "security":
        out["arm"] = bool(a.get("arm", True))
    elif t == "ask":
        out["text"], out["where"] = _text(a.get("text"), f"action {n}'s request", 200), where
    return out


# ---- the engine -------------------------------------------------------------------------------------------------
class RoutineEngine:
    TICK = 15.0

    def __init__(self, cfg, router=None, hub=None, path=None):
        from .config import data_dir

        self.cfg = cfg
        self.router = router
        self.hub = hub
        self.path = Path(path) if path else data_dir() / "routines.json"
        self._lock = threading.RLock()
        self._fired: dict[str, object] = {}         # id -> the day+minute it last fired (time/sun) or when (presence)
        self._last_tick: dt.datetime | None = None
        self._sun_cache: tuple | None = None
        self._local = threading.local()              # (how deep "ask" actions have nested, per thread)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._routines: list[dict] = self._load()

    # ---- storage --------------------------------------------------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            rs = d.get("routines", []) if isinstance(d, dict) else []
            return [r for r in rs if isinstance(r, dict) and r.get("id")]
        except FileNotFoundError:
            return []
        except Exception:
            log.exception("routines.json didn't load; starting with none (the file is left as it is)")
            self._broken = True
            return []

    def _save(self) -> None:
        if getattr(self, "_broken", False):
            # never overwrite a file we couldn't read: keep it beside, then carry on with what's in memory
            try:
                self.path.replace(self.path.with_suffix(".unreadable.json"))
            except OSError:
                pass
            self._broken = False
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"routines": self._routines}, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._routines)

    def get(self, rid: str) -> dict | None:
        with self._lock:
            r = next((r for r in self._routines if r["id"] == rid), None)
            return copy.deepcopy(r) if r else None

    def upsert(self, data: dict) -> dict:
        """Create or replace a routine (raises Invalid with what's wrong)."""
        m = self.meta()
        r = clean(data, colors=m["colors"], plugs=m["plugs"])
        with self._lock:
            if r["trigger"]["type"] == "voice":
                for other in self._routines:
                    if other["id"] == r["id"] or other.get("trigger", {}).get("type") != "voice":
                        continue
                    clash = set(r["trigger"]["phrases"]) & set(other["trigger"].get("phrases", []))
                    if clash:
                        raise Invalid(f"\"{sorted(clash)[0]}\" already starts {other['name']}")
            old = next((x for x in self._routines if x["id"] == r["id"]), None)
            if old is not None:
                for k in ("last_run", "last_source"):
                    if k in old and k not in r:
                        r[k] = old[k]
                self._routines[self._routines.index(old)] = r
                if old.get("trigger") != r["trigger"]:      # (a rename / an on-off flip right after it ran mustn't
                    self._fired.pop(r["id"], None)           # make it run again)
            else:
                self._routines.append(r)
            self._save()
        log.info("routine saved: %s (%s)", r["name"], r["trigger"]["type"])
        return copy.deepcopy(r)

    def delete(self, rid: str) -> bool:
        with self._lock:
            n = len(self._routines)
            self._routines = [r for r in self._routines if r["id"] != rid]
            if len(self._routines) == n:
                return False
            self._fired.pop(rid, None)
            self._save()
        return True

    def meta(self) -> dict:
        """What the app's editor offers: rooms and bulbs, colors, plugs, Echoes, and today's sunrise/sunset."""
        lights = getattr(self.router, "lights", None)
        plugs = getattr(self.router, "plugs", None)
        try:
            from .lights import COLORS
            colors = sorted(COLORS)
        except Exception:
            colors = []
        try:
            rooms = sorted(lights.rooms()) if lights else []
        except Exception:
            rooms = []
        try:
            bulbs = sorted(lights.bulbs()) if lights and hasattr(lights, "bulbs") else []
        except Exception:
            bulbs = []
        echoes = [{"name": s.name, "room": s.room} for s in getattr(self.hub, "sats", [])] if self.hub else []
        rise, sets = self.sun_today()
        return {"rooms": rooms, "bulbs": bulbs, "colors": colors,
                "plugs": sorted(getattr(plugs, "devices", {}) or {}) if plugs else [],
                "echoes": echoes, "sunrise": clock(rise), "sunset": clock(sets)}

    # ---- when ---------------------------------------------------------------------------------------------------
    def _latlon(self) -> tuple[float, float] | None:
        try:
            return float(self.cfg.get("location.lat", DEFAULT_LAT)), float(self.cfg.get("location.lon", DEFAULT_LON))
        except (TypeError, ValueError):
            return None

    def sun_today(self, day: dt.date | None = None) -> tuple[int | None, int | None]:
        day = day or dt.date.today()
        where = self._latlon()
        if where is None:
            return None, None
        lat, lon = where
        key = (day, lat, lon)
        if self._sun_cache and self._sun_cache[0] == key:
            return self._sun_cache[1]
        times = sun_times(day, lat, lon)
        self._sun_cache = (key, times)
        return times

    def _minute_of(self, spec: str, day: dt.date) -> int | None:
        if spec in ("sunrise", "sunset"):
            rise, sets = self.sun_today(day)
            return rise if spec == "sunrise" else sets
        return _hhmm(spec)

    def limits_ok(self, r: dict, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now()
        lim = r.get("limits") or {}
        if lim.get("days") and now.weekday() not in lim["days"]:
            return False
        between = lim.get("between")
        if between:
            a, b = self._minute_of(between[0], now.date()), self._minute_of(between[1], now.date())
            if a is None or b is None:
                return False
            m = now.hour * 60 + now.minute
            if a == b:
                return True
            return a <= m < b if a < b else (m >= a or m < b)
        return True

    def event_time(self, r: dict, day: dt.date) -> dt.datetime | None:
        """When a time / sun routine is due for `day` - the trigger's own day: its days filter is about that day, and a
        sun offset may land it on the next or the previous date ("4 hours after sunset" on Friday = early Saturday).
        None = not on that day."""
        tr = r.get("trigger") or {}
        if tr.get("days") and day.weekday() not in tr["days"]:
            return None
        midnight = dt.datetime.combine(day, dt.time())
        if tr.get("type") == "time":
            at = _hhmm(tr.get("at"))
            return None if at is None else midnight + dt.timedelta(minutes=at)
        if tr.get("type") == "sun":
            rise, sets = self.sun_today(day)
            base = rise if tr.get("event") == "sunrise" else sets
            return None if base is None else midnight + dt.timedelta(minutes=base + int(tr.get("offset", 0)))
        return None

    @staticmethod
    def _utc_offset(t: dt.datetime):
        try:
            return t.astimezone().utcoffset()
        except (OverflowError, OSError, ValueError):
            return None

    # ---- triggers -------------------------------------------------------------------------------------------------
    CATCH_UP = dt.timedelta(minutes=3)

    def tick(self, now: dt.datetime | None = None) -> list[str]:
        """Fire the time / sun routines due since the last check. The first check (and a slow one) looks back up to 3
        minutes; across a spring-forward clock change it covers the skipped hour, so a 2:30 AM routine runs at 3:00
        (like Alexa's). Each event once - also across a hub restart (its last_run says so). -> the ids fired."""
        now = now or dt.datetime.now()
        last, self._last_tick = self._last_tick, now
        start = now - self.CATCH_UP
        if last is not None and last < now:
            if self._utc_offset(last) != self._utc_offset(now) and now - last <= dt.timedelta(hours=2):
                start = min(start, last)             # (the clock jumped forward an hour: nothing in it is skipped)
            else:
                start = max(start, last)
        fired = []
        with self._lock:
            due = [r for r in self._routines if r.get("enabled") and r.get("trigger", {}).get("type") in ("time", "sun")]
        for r in due:
            for day in (now.date() - dt.timedelta(days=1), now.date()):
                ev = self.event_time(r, day)
                if ev is None or not start < ev <= now:
                    continue
                key = f"{day}@{ev:%Y-%m-%d %H:%M}"
                if self._fired.get(r["id"]) == key:
                    continue
                if r.get("last_source") in ("time", "sun") and float(r.get("last_run") or 0) >= ev.timestamp():
                    continue                         # (it ran for this event before a restart / an edit)
                if not self.limits_ok(r, now):
                    continue
                self._fired[r["id"]] = key
                fired.append(r["id"])
                self._spawn(r, {"source": r["trigger"]["type"], "room": "", "satellite": ""})
                break
        return fired

    def on_presence(self, room: str, present: bool) -> list[str]:
        """Someone showed up on an Echo's presence sensor (called on the hub's loop: everything runs in a thread)."""
        if not present:
            return []
        now = time.time()
        fired = []
        with self._lock:
            cands = [r for r in self._routines if r.get("enabled") and r.get("trigger", {}).get("type") == "presence"
                     and r["trigger"].get("room", "") in ("", (room or "").lower())]
        for r in cands:
            last = self._fired.get(r["id"])
            if isinstance(last, float) and now - last < r["trigger"].get("cooldown", 10) * 60:
                continue
            if not self.limits_ok(r):
                continue
            self._fired[r["id"]] = now
            fired.append(r["id"])
            self._spawn(r, {"source": "presence", "room": room or "", "satellite": ""})
        return fired

    def match_voice(self, text: str, room: str = "", satellite: str = "") -> dict | None:
        """A spoken (or typed) request that starts a routine -> her reply; None = not a routine. Its actions up to the
        first wait run now and what they say is the reply."""
        if getattr(self._local, "depth", 0) > 0:     # (an "ask" action inside a routine never starts another one)
            return None
        key = phrase_key(text)
        if not key:
            return None
        with self._lock:
            live = [r for r in self._routines if r.get("enabled")]
            hit = next((r for r in live if r.get("trigger", {}).get("type") == "voice"
                        and key in {phrase_key(p) for p in r["trigger"].get("phrases", [])}), None)
            if hit is None:
                m = _RUN.match(key)
                if m:
                    want = name_key(m.group("name"))
                    hit = next((r for r in live if want and name_key(r.get("name", "")) == want), None)
            hit = copy.deepcopy(hit) if hit else None
        if hit is None:
            return None
        said = self.run_now(hit, {"source": "voice", "room": room or "", "satellite": satellite or ""})
        return {"ok": True, "say": said or hit.get("reply") or "Okay.", "routine": hit["id"]}

    def run(self, rid: str, source: str = "app") -> dict:
        """The app's Run button: everything in the background, speaking on every Echo (or each action's own)."""
        r = self.get(rid)
        if r is None:
            return {"ok": False, "say": "There's no such routine."}
        self._spawn(r, {"source": source, "room": "", "satellite": ""})
        return {"ok": True, "say": f"Running {r['name']}."}

    # ---- running ---------------------------------------------------------------------------------------------------
    def _spawn(self, r: dict, ctx: dict) -> None:
        threading.Thread(target=self._run_all, args=(r, ctx), daemon=True, name=f"routine-{r['id']}").start()

    def _run_all(self, r: dict, ctx: dict) -> None:
        log.info("routine %s: started by %s", r["name"], ctx.get("source"))
        self._mark(r, ctx)
        self._run_steps(r, r.get("actions", []), ctx, speak=True)

    def run_now(self, r: dict, ctx: dict) -> str:
        """Voice: actions up to the first wait now, returning what they said; the rest in the background."""
        log.info("routine %s: started by voice", r["name"])
        self._mark(r, ctx)
        acts = r.get("actions", [])
        spoken = []
        here = (ctx.get("room") or "").lower()
        i = 0
        while i < len(acts) and acts[i]["type"] != "wait":
            a = acts[i]
            out = (self._step(a, ctx, speak=False, name=r["name"]) or "").strip()
            i += 1
            if not out:
                continue
            w = (a.get("where") or "").lower()
            if w in ("", here):                      # for the Echo it was said to: part of her reply
                spoken.append(out)
            elif w == "all":                         # every Echo: in the reply here, announced on the others
                spoken.append(out)
                for room in {(s.room or "").lower() for s in getattr(self.hub, "sats", []) or []} - {here, ""}:
                    self._announce(out, room, wait=False)
            else:                                    # another room's Echo: announced there (not over this reply)
                self._announce(out, w, wait=False)
        if i < len(acts):
            threading.Thread(target=self._run_steps, args=(r, acts[i:], ctx, True), daemon=True,
                             name=f"routine-{r['id']}").start()
        return " ".join(s if s[-1:] in ".!?" else s + "." for s in spoken if s)

    def _run_steps(self, r: dict, acts: list, ctx: dict, speak: bool) -> None:
        for a in acts:
            if self._stop.is_set():
                return
            if a["type"] == "wait":
                if self._stop.wait(a["seconds"]):
                    return
                continue
            self._step(a, ctx, speak=speak, name=r["name"])

    def _mark(self, r: dict, ctx: dict) -> None:
        with self._lock:
            live = next((x for x in self._routines if x["id"] == r["id"]), None)
            if live is not None:
                live["last_run"], live["last_source"] = time.time(), ctx.get("source", "")
                try:
                    self._save()
                except Exception:
                    log.exception("couldn't save the routines")

    def _where(self, a: dict, ctx: dict) -> str:
        """The room an action speaks / plays in: its own, else the one it was said to / is about, else every Echo."""
        w = (a.get("where") or "").lower()
        if w == "all":
            return ""
        return w or (ctx.get("room") or "")

    def _sats_for(self, a: dict, ctx: dict) -> list:
        """The Echoes an action plays on / sets: its own room's (none if that room has no Echo), else the one it was
        said to, else the room it's about, else every Echo."""
        sats = list(getattr(self.hub, "sats", []) or [])
        w = (a.get("where") or "").lower()
        if w == "all":
            return sats
        if w:
            return [s for s in sats if (s.room or "").lower() == w]
        if ctx.get("satellite"):
            hit = [s for s in sats if s.name == ctx["satellite"]]
            if hit:
                return hit
        room = (ctx.get("room") or "").lower()
        hit = [s for s in sats if room and (s.room or "").lower() == room]
        return hit or sats

    def _say(self, text: str, a: dict, ctx: dict, speak: bool) -> str | None:
        if not text:
            return None
        if not speak:
            return text
        self._announce(text, self._where(a, ctx), wait=True)
        return None

    def _announce(self, text: str, room: str, wait: bool) -> None:
        """Speak on the Echo(es) of `room` ("" = every Echo). wait = until it has played (the hub's announce awaits the
        Echo), so a routine's announcements come one after another, in order, never on top of each other."""
        ann = getattr(self.router, "announce", None)
        if ann is None:
            return
        try:
            fut = ann(text, room)
            if wait and fut is not None and hasattr(fut, "result"):
                fut.result(timeout=120)
        except Exception:
            log.exception("routine: announcing failed")

    def _step(self, a: dict, ctx: dict, speak: bool, name: str = "") -> str | None:
        """One action. -> what it says (for a voice reply) when speak is False; spoken as an announcement otherwise."""
        t = a["type"]
        rt = self.router
        try:
            if t == "lights":
                rt.lights.set_state(a["target"], a["on"], "")
            elif t == "brightness":
                rt.lights.set_brightness(a["target"], a["percent"], "")
            elif t == "color":
                rt.lights.set_color(a["target"], a["color"], "")
            elif t == "plug":
                rt.plugs.set_state(a["name"], a["on"])
            elif t == "say":
                return self._say(a["text"], a, ctx, speak)
            elif t == "time":
                return self._say("It's " + time.strftime("%I:%M %p").lstrip("0") + ".", a, ctx, speak)
            elif t == "weather":
                from . import web

                return self._say(str(web.weather(self.cfg, when=a.get("when", "today")).get("say", "")), a, ctx, speak)
            elif t in ("music", "music_stop"):
                sats = self._sats_for(a, ctx)
                for s in sats:
                    cmd = f"play {a['query'].lower()}" if t == "music" else "stop"
                    rt._music_fast(cmd, s.room, s.name, said_music=True)
            elif t == "volume":
                for s in self._sats_for(a, ctx):
                    s.speaker.set_volume(a["percent"] / 100.0)
            elif t == "security":
                sec = getattr(rt, "security", None)
                if sec is not None:
                    res = sec.arm() if a["arm"] else sec.disarm()
                    return self._say(str((res or {}).get("say", "")), a, ctx, speak)
            elif t == "ask":
                # as if said in the step's room ("all" = no room: house-wide), else to the Echo it was said to, else in
                # the room it's about; only an Echo that is the one in that room is named (music plays there)
                w = (a.get("where") or "").lower()
                sats = list(getattr(self.hub, "sats", []) or [])
                if w == "all":
                    room, sat = "", None
                elif w:
                    there = [s for s in sats if (s.room or "").lower() == w]
                    room, sat = w, (there[0] if len(there) == 1 else None)
                else:
                    sat = next((s for s in sats if s.name == ctx.get("satellite")), None) if ctx.get("satellite") else None
                    room = sat.room if sat else (ctx.get("room") or "")
                self._local.depth = getattr(self._local, "depth", 0) + 1
                try:
                    res = rt.handle(a["text"], room, **({"satellite": sat.name} if sat else {}))
                finally:
                    self._local.depth -= 1
                return self._say(str((res or {}).get("say", "")), a, ctx, speak)
        except Exception:
            log.exception("routine %s: the %s action failed", name, t)
        return None

    # ---- the clock ------------------------------------------------------------------------------------------------
    def start(self) -> "RoutineEngine":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="houseava-routines")
            self._thread.start()
            rise, sets = self.sun_today()
            log.info("routines: %d loaded; today's sunrise %s, sunset %s", len(self._routines), clock(rise), clock(sets))
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.TICK):
            try:
                self.tick()
            except Exception:
                log.exception("routines: the clock check failed")
