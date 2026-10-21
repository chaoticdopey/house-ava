"""House Ava tests — the router's fast-path parsing and dispatch, with stubs (no network, no bulbs).

    python tests/run_tests.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from houseava.brain import Router          # noqa: E402
from houseava.config import Config         # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


class FakeLights:
    def __init__(self):
        self.calls = []

    def rooms(self):
        return {"living room": ["living room 1", "living room 2"], "bedroom": ["bedroom 1", "bedroom 2"],
                "dining room": ["dining room 1", "dining room 2"]}

    def set_state(self, which, on, room=""):
        self.calls.append(("state", which, on, room))
        return {"ok": True, "say": f"Turned {which or room or 'all'} {'on' if on else 'off'}."}

    def set_brightness(self, which, pct, room=""):
        self.calls.append(("dim", which, pct, room))
        return {"ok": True, "say": f"dim {pct}"}

    def set_color(self, which, color, room=""):
        self.calls.append(("color", which, color, room))
        return {"ok": True, "say": f"color {color}"}


class FakePlugs:
    def __init__(self):
        self.devices = {"porch": "Porch Light"}
        self.calls = []

    def set_state(self, friendly, on):
        self.calls.append((friendly, on))
        return {"ok": True, "say": f"Turned the {friendly} {'on' if on else 'off'}."}


class FakeBrain:
    """Returns a scripted intent for _think; records that it was called."""
    def __init__(self, intent=None, text="a chatty answer"):
        self.intent = intent or {"action": "answer", "say": "Ha, nice try. The sky's blue because physics."}
        self.text = text
        self.json_calls = 0
        self.chat_calls = 0

    def status(self):
        return True, "ready"

    def chat_json(self, system, user, temperature=None):
        self.json_calls += 1
        return self.intent

    def chat(self, system, user, temperature=None, history=None):
        self.chat_calls += 1
        return self.text


def make(brain=None):
    cfg = Config(path=os.path.join(os.path.dirname(__file__), "__no_such_config__.json"))  # defaults only
    L, P = FakeLights(), FakePlugs()

    class Mem:
        def __init__(self):
            self.turns = []

        def add_turn(self, *a, **k):
            pass

        def recent(self, *a, **k):
            return []

        def recall(self, *a, **k):
            return []

        def remember(self, w):
            return {"ok": True, "say": "Got it, I'll remember that."}

        def forget(self, w):
            return {"ok": True, "say": "Forgotten."}

    r = Router(cfg, brain or FakeBrain(), L, P, Mem(), lambda: "sys", announce=None)
    return r, L, P


# ---- fast-path: lights, room-aware, never hit the brain ----
r, L, _ = make()
r.handle("turn the lights off", "bedroom")
check("'turn the lights off' in the bedroom -> bedroom, off", L.calls[-1] == ("state", "", False, "bedroom"), str(L.calls[-1]))
r.handle("turn on the living room lights", "bedroom")
check("explicit room overrides the caller's room", L.calls[-1] == ("state", "living room", True, "bedroom"), str(L.calls[-1]))
r.handle("lights on", "living room")
check("'lights on' -> on", L.calls[-1][0] == "state" and L.calls[-1][2] is True, str(L.calls[-1]))
r.handle("turn off all the lights", "")
check("'all the lights off' -> all, off", L.calls[-1] == ("state", "all", False, ""), str(L.calls[-1]))
r.handle("set the bedroom to 30 percent", "living room")
check("'set the bedroom to 30 percent' -> dim 30", L.calls[-1] == ("dim", "bedroom", 30, "living room"), str(L.calls[-1]))
r.handle("make the living room blue", "bedroom")
check("'make the living room blue' -> color blue", L.calls[-1] == ("color", "living room", "blue", "bedroom"), str(L.calls[-1]))

# the brain is never consulted for these
r2, L2, _ = make(FakeBrain())
for line in ("turn the lights off", "lights on", "set bedroom to 50 percent"):
    r2.handle(line, "bedroom")
check("fast-path lights never call the brain", r2.brain.json_calls == 0 and r2.brain.chat_calls == 0)

# ---- fast-path: plug by known name ----
r3, _, P3 = make()
r3.handle("turn the porch on", "living room")
check("'turn the porch on' -> plug porch on", P3.calls[-1] == ("porch", True), str(P3.calls))
r3.handle("turn off the porch light", "")
check("'turn off the porch light' -> plug porch off", P3.calls[-1] == ("porch", False), str(P3.calls))

# a plug name is NOT a light
r3b, L3b, _ = make()
r3b.handle("turn off the kitchen lights", "")
check("an unknown room still routes to lights, not plugs", L3b.calls[-1][0] == "state")

# ---- fast-path: time / remember / timer ----
r4, _, _ = make()
out = r4.handle("what time is it", "")
check("'what time is it' answered locally, no brain", ":" in out["say"] and r4.brain.json_calls == 0, out["say"])
out = r4.handle("remember that bedtime is ten", "")
check("'remember that ...' -> saved", "remember" in out["say"].lower(), out["say"])

# ---- the brain: chit-chat routes to answer ----
rb, _, _ = make(FakeBrain({"action": "answer", "say": "Because Rayleigh scattering, show-off."}))
out = rb.handle("why is the sky blue", "")
check("a question with a known answer -> her reply, one brain call", out["say"].startswith("Because") and rb.brain.json_calls == 1, out["say"])

# ---- the brain: a loose device phrase maps to an action ----
rc, Lc, _ = make(FakeBrain({"action": "light", "target": "bedroom", "on": False, "say": "Lights out, night owl."}))
out = rc.handle("ugh, my eyes, do something about the glare", "bedroom")
check("the brain can map a loose phrase to a light action", Lc.calls[-1][0] == "state" and Lc.calls[-1][2] is False, str(Lc.calls))
out = rc.handle("it's too bright in here", "bedroom")     # 10-16: lightcmd reads this one itself - it DIMS (no brain)
check("'it's too bright in here' -> dim this room", Lc.calls[-1][0] == "dim" and Lc.calls[-1][1] == "", str(Lc.calls))

# ---- the brain: 'search' hits the web path (stub web.answer) ----
import houseava.web as W   # noqa: E402
W.answer = lambda q, cfg, brain, sysp="": {"ok": True, "say": "Per the web: 1,083 feet."}
rd, _, _ = make(FakeBrain({"action": "search", "query": "height of the eiffel tower", "say": ""}))
out = rd.handle("how tall is the eiffel tower exactly", "")
check("'search' intent -> web answer", "1,083" in out["say"], out["say"])

print("\n==== RESULT:", "ALL PASSED" if not fails else f"FAILURES: {fails}", "====")
sys.exit(1 if fails else 0)
