"""Routines (houseava/routines.py): checking what the app sends, the clock, motion, voice, running, and the app's API.
Fake lights / plugs / Echoes - nothing in the house is touched.

    .venv\\Scripts\\python.exe tests\\test_routines.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="houseava-test-")   # never the real routines / log / memory
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test
if hasattr(time, "tzset"):                 # the sun tests are New York times, even on a server set to UTC
    os.environ["TZ"] = "America/New_York"      # (Windows has no tzset: run this one on Linux / a Mac)
    time.tzset()

from houseava import routines as R  # noqa: E402
from houseava.brain import Router  # noqa: E402
from houseava.config import Config  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


# ---- fakes ----
class Lights:
    def __init__(self):
        self.calls = []

    def rooms(self):
        return {"living room": ["living room 1"], "bedroom": ["bedroom 1"], "dining room": ["dining room 1"]}

    def bulbs(self):
        return {"living room 1": "1", "bedroom 1": "2", "dining room 1": "3"}

    def set_state(self, which, on, room=""):
        self.calls.append(("state", which, on, room))
        return {"ok": True, "say": "lights"}

    def set_brightness(self, which, pct, room=""):
        self.calls.append(("dim", which, pct, room))
        return {"ok": True, "say": "dim"}

    def set_color(self, which, color, room=""):
        self.calls.append(("color", which, color, room))
        return {"ok": True, "say": "color"}


class Plugs:
    devices = {"porch": "Porch Light"}

    def __init__(self):
        self.calls = []

    def set_state(self, name, on):
        self.calls.append((name, on))
        return {"ok": True, "say": "plug"}


class Speaker:
    def __init__(self):
        self.vol = []

    def set_volume(self, v):
        self.vol.append(v)


class Sat:
    def __init__(self, name, room):
        self.name, self.room, self.speaker = name, room, Speaker()


class Hub:
    def __init__(self):
        self.sats = [Sat("kitchen-echo", "kitchen"), Sat("bedroom-echo", "bedroom")]


class NoBrain:
    def chat_json(self, *a, **k):
        return {"action": "answer", "say": "brain answer"}

    def chat(self, *a, **k):
        return "brain answer"


class Mem:
    def __init__(self):
        self.turns, self.facts = [], []

    def add_turn(self, *a):
        self.turns.append(a)
        return a

    def drop_turn(self, e):
        return False

    def recent(self, *a, **k):
        return []


NY_LAT, NY_LON = 40.7128, -74.0060                      # the sun tests run in New York
cfg = Config(path=os.path.join(os.path.dirname(__file__), "__no_such_config__.json"))
cfg.set("location.lat", NY_LAT)
cfg.set("location.lon", NY_LON)
said = []


def make(path=None):
    lights, plugs = Lights(), Plugs()
    router = Router(cfg, NoBrain(), lights, plugs, Mem(), lambda: "persona", announce=lambda t, r="": said.append((t, r)))
    router.music_calls = []
    router._music_fast = lambda low, room, sat, said_music=False: router.music_calls.append((low, room, sat)) or {"ok": True}
    hub = Hub()
    eng = R.RoutineEngine(cfg, router, hub=hub, path=path or os.path.join(tempfile.mkdtemp(), "routines.json"))
    router.routines = eng
    return eng, router, lights, plugs, hub


# ---- phrases ----
for raw, want in (("Alexa, good night!", "good night"), ("good night please", "good night"),
                  ("Hey Ava, can you good night", "good night"), ("Good-night.", "good night"),
                  ("okay movie time now", "movie time"), ("I'm home.", "im home"), ("I’m home", "im home")):
    check(f"phrase_key({raw!r}) = {want!r}", R.phrase_key(raw) == want, R.phrase_key(raw))
for raw, want in (("The Morning Routine", "morning"), ("morning routine", "morning"), ("My Party", "party"),
                  ("The Works", "works")):
    check(f"name_key({raw!r}) = {want!r}", R.name_key(raw) == want, R.name_key(raw))

# ---- checking what the app sends ----
GOOD = {"name": "Good night", "trigger": {"type": "voice", "phrases": "good night, Goodnight!"},
        "actions": [{"type": "lights", "target": "All", "on": False}, {"type": "say", "text": "Night."}]}
r = R.clean(GOOD, colors=["red", "warm white"], plugs=["porch"])
check("clean: phrases split, normalized, de-duplicated", r["trigger"]["phrases"] == ["good night", "goodnight"], r)
check("clean: an id is made, it's enabled, actions normalized",
      r["id"] and r["enabled"] and r["actions"][0] == {"type": "lights", "target": "all", "on": False}, r)
BAD = [
    ({"trigger": {"type": "voice", "phrases": ["x"]}, "actions": [{"type": "say", "text": "x"}]}, "the name is empty"),
    ({"name": "a", "trigger": {"type": "voice", "phrases": ["x"]}, "actions": []}, "at least one action"),
    ({"name": "a", "trigger": {"type": "time", "at": "25:00"}, "actions": [{"type": "time"}]}, "pick a time"),
    ({"name": "a", "trigger": {"type": "nope"}, "actions": [{"type": "time"}]}, "what starts it"),
    ({"name": "a", "trigger": {"type": "manual"}, "actions": [{"type": "color", "color": "plaid"}]}, "pick a color"),
    ({"name": "a", "trigger": {"type": "manual"}, "actions": [{"type": "plug", "name": "garage"}]}, "pick a plug"),
    ({"name": "a", "trigger": {"type": "manual"}, "actions": [{"type": "wait", "seconds": 999999}]}, "wait must be"),
    ({"name": "a", "trigger": {"type": "time", "at": "07:00", "days": [7]}, "actions": [{"type": "time"}]}, "days"),
    ({"name": "a", "trigger": {"type": "voice", "phrases": ", ,"}, "actions": [{"type": "time"}]}, "say what"),
    ({"name": "a", "trigger": {"type": "presence"}, "limits": {"between": ["noon", "06:00"]},
      "actions": [{"type": "time"}]}, "the start must be"),
]
for data, msg in BAD:
    try:
        R.clean(data, colors=["red"], plugs=["porch"])
        got = "no error"
    except R.Invalid as exc:
        got = str(exc)
    check(f"clean rejects: {msg}", msg in got, got)

# ---- sunrise / sunset (New York, 12 Oct 2026: about 7:04 AM / 6:19 PM EDT) ----
rise, sets = R.sun_times(dt.date(2026, 10, 12), NY_LAT, NY_LON)
utc_off = dt.datetime(2026, 10, 12, 12).astimezone().utcoffset().total_seconds() / 60
edt = -240
check("sunrise in New York on Oct 12 is ~7:04 AM local (within 8 min)", abs(rise - (424 + (utc_off - edt))) % 1440 <= 8,
      (R.clock(rise), utc_off))
check("sunset in New York on Oct 12 is ~6:19 PM local (within 8 min)", abs(sets - (1099 + (utc_off - edt))) % 1440 <= 8,
      (R.clock(sets), utc_off))
check("no sunrise at the North Pole in December", R.sun_times(dt.date(2026, 12, 21), 89.0, 0.0) == (None, None))

# ---- the clock ----
eng, router, lights, plugs, hub = make()
eng.upsert({"name": "Porch off", "trigger": {"type": "time", "at": "23:30", "days": []},
            "actions": [{"type": "plug", "name": "porch", "on": False}]})
eng.upsert({"name": "Weekday lights", "trigger": {"type": "time", "at": "07:00", "days": [0, 1, 2, 3, 4]},
            "actions": [{"type": "lights", "target": "living room", "on": True}]})
mon, sat_day = dt.date(2026, 10, 12), dt.date(2026, 10, 17)     # (a Monday, a Saturday)
check("clock: nothing at 23:29", eng.tick(dt.datetime.combine(mon, dt.time(23, 29, 50))) == [])
first = eng.tick(dt.datetime.combine(mon, dt.time(23, 30, 5)))
time.sleep(0.2)
check("clock: fires at 23:30", len(first) == 1 and plugs.calls == [("porch", False)], (first, plugs.calls))
check("clock: ... once (the next tick in the same minute doesn't)", eng.tick(dt.datetime.combine(mon, dt.time(23, 30, 35))) == [])
check("clock: ... and not again 2 minutes later", eng.tick(dt.datetime.combine(mon, dt.time(23, 32, 0))) == [])
eng2, _, l2, _, _ = make()
eng2.upsert({"name": "Weekday lights", "trigger": {"type": "time", "at": "07:00", "days": [0, 1, 2, 3, 4]},
             "actions": [{"type": "lights", "target": "living room", "on": True}]})
check("clock: a slow tick still catches it (07:02 for 07:00)", len(eng2.tick(dt.datetime.combine(mon, dt.time(7, 2)))) == 1)
check("clock: weekdays-only doesn't run on Saturday", eng2.tick(dt.datetime.combine(sat_day, dt.time(7, 0, 10))) == [])
eng3, _, _, p3, _ = make()
eng3.upsert({"name": "Porch at sunset", "trigger": {"type": "sun", "event": "sunset", "offset": -15},
             "actions": [{"type": "plug", "name": "porch", "on": True}]})
_, sunset = eng3.sun_today(mon)
at = (sunset - 15) % 1440
check("clock: 15 min before sunset", len(eng3.tick(dt.datetime.combine(mon, dt.time(at // 60, at % 60, 20)))) == 1)
time.sleep(0.2)
check("clock: ... turned the porch on", p3.calls == [("porch", True)], p3.calls)

eng10, _, _, p10, _ = make()
eng10.upsert({"name": "Wake", "trigger": {"type": "time", "at": "07:00", "days": []},
              "actions": [{"type": "plug", "name": "porch", "on": True}]})
rid10 = eng10.list()[0]["id"]
eng10.tick(dt.datetime.combine(mon, dt.time(6, 59, 50)))
check("clock: (fires at 07:00)", len(eng10.tick(dt.datetime.combine(mon, dt.time(7, 0, 10)))) == 1)
time.sleep(0.2)
r10 = eng10.get(rid10)
r10["enabled"] = False
eng10.upsert(r10)
r10["enabled"] = True
eng10.upsert(r10)                                           # (switched off and on again from the app, a minute later)
check("clock: an edit / on-off flip right after it ran doesn't make it run again",
      eng10.tick(dt.datetime.combine(mon, dt.time(7, 1, 30))) == [] and len(p10.calls) == 1, p10.calls)
e10b, *_ = make(eng10.path)                                  # (the hub restarts at 07:01)
check("clock: ... nor does a hub restart (its last run is remembered)",
      e10b.tick(dt.datetime.combine(mon, dt.time(7, 1, 40))) == [])
eng11, *_ = make()
eng11.upsert({"name": "Late Friday", "trigger": {"type": "sun", "event": "sunset", "offset": 240, "days": [4]},
              "actions": [{"type": "time"}]})
r11 = eng11.list()[0]
fri = dt.date(2026, 6, 19)                                   # (a Friday; New York sunset ~8:30 PM -> +4 h = after midnight)
ev = eng11.event_time(r11, fri)
check("sun: 4 h after a summer sunset lands on the NEXT date (Saturday ~00:30)",
      ev is not None and ev.date() == dt.date(2026, 6, 20) and ev.hour == 0, ev)
check("sun: ... and runs then (it's Friday's, by the Friday filter)", len(eng11.tick(ev + dt.timedelta(seconds=20))) == 1)
check("sun: ... Saturday's own sunset isn't a Friday one", eng11.event_time(r11, dt.date(2026, 6, 20)) is None)
eng12, *_ = make()
eng12.upsert({"name": "Two thirty", "trigger": {"type": "time", "at": "02:30", "days": []}, "actions": [{"type": "time"}]})
before, after = dt.datetime(2027, 3, 14, 1, 59, 50), dt.datetime(2027, 3, 14, 3, 0, 5)
if before.astimezone().utcoffset() != after.astimezone().utcoffset():  # (only where the clocks spring forward)
    eng12.tick(before)
    check("clock: the spring-forward night, a 2:30 AM routine runs at 3:00 (the hour that never happened)",
          len(eng12.tick(after)) == 1)

# ---- motion (presence) ----
eng4, _, l4, _, _ = make()
eng4.upsert({"name": "Kitchen night light", "trigger": {"type": "presence", "room": "kitchen", "cooldown": 10},
             "actions": [{"type": "brightness", "target": "dining room", "percent": 25}]})
check("motion: someone leaving doesn't start it", eng4.on_presence("kitchen", False) == [])
check("motion: another room doesn't start it", eng4.on_presence("bedroom", True) == [])
check("motion: the kitchen does", len(eng4.on_presence("kitchen", True)) == 1)
check("motion: ... not again within its 10 minutes", eng4.on_presence("kitchen", True) == [])
time.sleep(0.2)
check("motion: ... dimmed the dining room", ("dim", "dining room", 25, "") in l4.calls, l4.calls)
night = {"limits": {"between": ["sunset", "sunrise"]}}
rise, sets = eng4.sun_today(mon)
check("limits: sunset-to-sunrise holds at 11 PM", eng4.limits_ok(night, dt.datetime.combine(mon, dt.time(23, 0))))
check("limits: ... and at 3 AM", eng4.limits_ok(night, dt.datetime.combine(mon, dt.time(3, 0))))
check("limits: ... not at noon", not eng4.limits_ok(night, dt.datetime.combine(mon, dt.time(12, 0))))
check("limits: weekends only isn't Monday", not eng4.limits_ok({"limits": {"days": [5, 6]}}, dt.datetime.combine(mon, dt.time(9))))

# ---- voice ----
said.clear()
eng5, router5, l5, p5, hub5 = make()
eng5.upsert({"name": "Good night", "trigger": {"type": "voice", "phrases": ["good night"]},
             "actions": [{"type": "lights", "target": "all", "on": False}, {"type": "say", "text": "Sleep tight"},
                         {"type": "volume", "percent": 30}, {"type": "wait", "seconds": 1},
                         {"type": "say", "text": "Porch is off too"}, {"type": "plug", "name": "porch", "on": False}]})
res = router5.handle("Alexa, good night!", "kitchen", satellite="kitchen-echo", heard="wake")
check("voice: 'good night' runs it; what it says before the wait IS her reply", res.get("say") == "Sleep tight.", res)
check("voice: ... the lights went off right away", ("state", "all", False, "") in l5.calls, l5.calls)
check("voice: ... volume only on the Echo it was said to", hub5.sats[0].speaker.vol == [0.3] and not hub5.sats[1].speaker.vol,
      [s.speaker.vol for s in hub5.sats])
check("voice: ... nothing after the wait yet", not p5.calls and not said)
time.sleep(1.4)
check("voice: ... after the wait: the porch, and the rest spoken as an announcement in the kitchen",
      p5.calls == [("porch", False)] and said == [("Porch is off too", "kitchen")], (p5.calls, said))
res = router5.handle("good night", "kitchen", satellite="kitchen-echo", heard="follow_up")
check("voice: never from a follow-up (no wake word)", res.get("routine") is None, res)
res = router5.handle("run good night", "", heard=None)
check("voice: 'run <name>' works (and typed in the app's chat)", res.get("routine"), res)
eng5.upsert({"name": "Quiet", "trigger": {"type": "voice", "phrases": ["shh"]}, "reply": "Fine.",
             "actions": [{"type": "lights", "target": "bedroom", "on": False}]})
res = router5.handle("shh", "kitchen", satellite="kitchen-echo", heard="wake")
check("voice: no step speaks -> her reply is the routine's own", res.get("say") == "Fine.", res)
try:
    eng5.upsert({"name": "Dupe", "trigger": {"type": "voice", "phrases": ["Good night"]}, "actions": [{"type": "time"}]})
    clash = "no error"
except R.Invalid as exc:
    clash = str(exc)
check("voice: two routines can't share a phrase", "already starts Good night" in clash, clash)
eng5.upsert({"name": "Loop", "trigger": {"type": "voice", "phrases": ["loop"]},
             "actions": [{"type": "ask", "text": "loop"}]})
done = []
t = threading.Thread(target=lambda: done.append(router5.handle("loop", "kitchen", satellite="kitchen-echo", heard="wake")))
t.start()
t.join(5)
check("voice: an 'ask' that names its own routine doesn't loop forever", done and not t.is_alive(), done)
eng5.upsert({"name": "The Morning Routine", "trigger": {"type": "manual"}, "actions": [{"type": "time"}]})
for say_it in ("run the morning routine", "run morning routine", "start morning"):
    res = router5.handle(say_it, "kitchen", satellite="kitchen-echo", heard="wake")
    check(f"voice: {say_it!r} starts 'The Morning Routine'", res.get("routine"), res)
eng5.upsert({"name": "Christmas", "trigger": {"type": "manual"}, "actions": [{"type": "time"}]})
router5.music_calls.clear()
res = router5.handle("play christmas", "kitchen", satellite="kitchen-echo", heard="wake")
check("voice: 'play christmas' is music, not the routine called Christmas",
      not res.get("routine") and router5.music_calls and router5.music_calls[0][0] == "play christmas", res)
said.clear()
eng5.upsert({"name": "Dinner", "trigger": {"type": "voice", "phrases": ["dinner time"]},
             "actions": [{"type": "say", "text": "Dinner's ready", "where": "all"},
                         {"type": "say", "text": "Bedroom only", "where": "bedroom"}]})
res = router5.handle("dinner time", "kitchen", satellite="kitchen-echo", heard="wake")
check("voice: an 'every Echo' step is in the reply here AND announced on the other Echoes; another room's step is "
      "announced there, not in this reply", res.get("say") == "Dinner's ready." and
      sorted(said) == [("Bedroom only", "bedroom"), ("Dinner's ready", "bedroom")], (res, said))

# background announcements play one after another, in order
order = []


class Played:
    def __init__(self, t):
        self.t = t

    def result(self, timeout=None):
        time.sleep(0.15)
        order.append(("done", self.t))


eng13, router13, *_ = make()
router13.announce = lambda t, r="": order.append(("start", t)) or Played(t)
eng13.upsert({"name": "Morning", "trigger": {"type": "manual"},
              "actions": [{"type": "say", "text": "one"}, {"type": "say", "text": "two"}, {"type": "say", "text": "three"}]})
eng13.run(eng13.list()[0]["id"])
time.sleep(0.8)
check("run: announcements wait for each other (start one, done one, start two...)",
      order == [("start", "one"), ("done", "one"), ("start", "two"), ("done", "two"), ("start", "three"),
                ("done", "three")], order)

# "ask" steps: in the step's room / house-wide / the Echo it was said to
asked = []
eng14, router14, *_ = make()
real_handle = router14.handle
router14.handle = lambda text, room="", **kw: asked.append((text, room, kw.get("satellite"))) or {"say": ""}
eng14.upsert({"name": "Night", "trigger": {"type": "manual"},
              "actions": [{"type": "ask", "text": "turn off the lights"},
                          {"type": "ask", "text": "play jazz", "where": "bedroom"},
                          {"type": "ask", "text": "lights off", "where": "all"},
                          {"type": "ask", "text": "garage lights", "where": "garage"}]})
eng14.run(eng14.list()[0]["id"])
time.sleep(0.4)
check("ask: from the app/clock = house-wide (no room, no Echo); a room's step = that room (+ its Echo); 'all' = house-wide;"
      " a room with no Echo keeps its room (never another room)",
      asked == [("turn off the lights", "", None), ("play jazz", "bedroom", "bedroom-echo"), ("lights off", "", None),
                ("garage lights", "garage", None)], asked)
router14.handle = real_handle

router5.music_calls.clear()
eng5.upsert({"name": "Music", "trigger": {"type": "manual"}, "actions": [{"type": "music", "query": "Some JAZZ",
                                                                          "where": "bedroom"}]})
rid = next(r["id"] for r in eng5.list() if r["name"] == "Music")
eng5.run(rid)
time.sleep(0.3)
check("run: music on the room it names (the bedroom Echo), query lowercased",
      router5.music_calls == [("play some jazz", "bedroom", "bedroom-echo")], router5.music_calls)
router5.music_calls.clear()
eng5.upsert({"id": rid, "name": "Music", "trigger": {"type": "manual"},
             "actions": [{"type": "music", "query": "x", "where": "garage"}]})
eng5.run(rid)
time.sleep(0.3)
check("run: a room with no Echo plays nowhere (never another room)", router5.music_calls == [], router5.music_calls)

# ---- saved to disk, and read back ----
path = os.path.join(tempfile.mkdtemp(), "routines.json")
e6, *_ = make(path)
e6.upsert(GOOD)
e7, *_ = make(path)
check("storage: routines survive a restart", [r["name"] for r in e7.list()] == ["Good night"], e7.list())
check("storage: delete", e7.delete(e7.list()[0]["id"]) and e7.list() == [])
with open(path, "w", encoding="utf-8") as f:
    f.write("{ not json")
e8, *_ = make(path)
check("storage: an unreadable file -> no routines, and it's kept aside on the next save (never overwritten)",
      e8.list() == [] and (e8.upsert(GOOD) and os.path.exists(path.replace(".json", ".unreadable.json"))))

# ---- the app's API ----
from houseava.admin import AdminServer  # noqa: E402


class ApiHub:
    def __init__(self, routines):
        self.routines, self.sats, self.router = routines, [], None


acfg = Config(path=os.path.join(os.environ["HOUSEAVA_DIR"], "api_cfg.json"))
acfg.set("admin.port", 0)
acfg.set("admin.pin", "2468")
acfg.set("location.lat", NY_LAT)
acfg.set("location.lon", NY_LON)
e9, *_ = make()
srv = AdminServer(acfg, hub=ApiHub(e9))
srv.start()
base = f"http://127.0.0.1:{srv.port}"


def call(method, path, body=None, pin="2468"):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Pin": pin, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


st, d = call("GET", "/api/routines", pin="0000")
check("api: wrong PIN -> 401", st == 401, st)
st, d = call("POST", "/api/routines", GOOD)
check("api: create", st == 200 and d["routine"]["name"] == "Good night", (st, d))
rid = d["routine"]["id"]
st, d = call("GET", "/api/routines")
check("api: list + what the editor needs (rooms, colors, plugs, sunrise/sunset)",
      st == 200 and len(d["routines"]) == 1 and "living room" in d["meta"]["rooms"] and "porch" in d["meta"]["plugs"]
      and d["meta"]["sunset"].endswith("PM"), d.get("meta"))
st, d = call("POST", "/api/routines", {"name": "", "trigger": {"type": "manual"}, "actions": [{"type": "time"}]})
check("api: a bad routine -> 400 with what's wrong", st == 400 and "name" in d.get("error", ""), (st, d))
st, d = call("POST", "/api/routines/run", {"id": rid})
check("api: run", st == 200 and d.get("ok"), (st, d))
st, d = call("DELETE", f"/api/routines?id={rid}")
check("api: delete", st == 200 and d.get("ok") and e9.list() == [], (st, d))
st, d = call("DELETE", "/api/routines?id=nope")
check("api: delete something that isn't there -> 404", st == 404, st)
for bad in ({"name": "x", "trigger": "voice", "actions": [{"type": "time"}]},
            {"name": "x", "trigger": {"type": "manual"}, "limits": [1], "actions": [{"type": "time"}]},
            {"name": "x", "trigger": {"type": "sun", "event": "sunset", "offset": 1e400}, "actions": [{"type": "time"}]},
            ["not", "a", "routine"]):
    st, d = call("POST", "/api/routines", bad)
    check(f"api: a wrongly shaped routine gets a 400 answer (never a dropped connection): {str(bad)[:40]}",
          st == 400 and d.get("error"), (st, d))
st, d = call("POST", "/api/routines/run", ["x"])
check("api: run with a wrongly shaped body -> 400", st == 400, (st, d))
srv.stop()

# a phrase saved before the lead/tail words grew ("dinner time everyone" was its key then) still matches
_old = os.path.join(tempfile.mkdtemp(), "routines.json")
with open(_old, "w", encoding="utf-8") as f:
    json.dump({"routines": [{"id": "old1", "name": "Dinner", "enabled": True,
                             "trigger": {"type": "voice", "phrases": ["dinner time everyone"]},
                             "actions": [{"type": "say", "text": "Dinner!"}]}]}, f)
eng, router, lights, plugs, hub = make(_old)
for said_ in ("Dinner time everyone", "dinner time"):
    check(f"an old saved phrase still matches {said_!r}", eng.match_voice(said_, "kitchen", "Kitchen Echo") is not None)

print(f"\n==== RESULT: {'ALL PASSED' if not fails else 'FAILURES: ' + str(fails)} ====")
sys.exit(1 if fails else 0)
