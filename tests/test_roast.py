"""10-20 fixes: roast a NAMED person (longer / harder / another one / roast her more), recent conversation for the
intent step + web answers, the 'Enya' -> 'eñe' mishear, silent intruder alert / back to normal, volume button stops
the alarm music.

    python tests/test_roast.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from houseava import brain as brain_mod    # noqa: E402
from houseava import persona, web          # noqa: E402
from houseava.brain import Router, _fix_heard   # noqa: E402
from houseava.config import Config         # noqa: E402
from houseava.memory import Memory         # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


class FakeLights:
    def __init__(self):
        self.calls, self._saved = [], None

    def rooms(self):
        return {"living room": ["living room 1"], "bedroom": ["bedroom 1"], "kitchen": [], "dining room": ["dining room 1"]}

    def set_state(self, which, on, room=""):
        self.calls.append(("state", which, on, room))
        return {"ok": True, "say": f"Turned {which or room or 'all'} {'on' if on else 'off'}."}

    def set_brightness(self, which, pct, room=""):
        return {"ok": True, "say": f"dim {pct}"}

    def set_color(self, which, color, room=""):
        return {"ok": True, "say": f"color {color}"}

    def panic_red(self):
        self._saved = {"x": 1}
        self.calls.append(("panic",))

    def restore(self):
        self._saved = None
        self.calls.append(("restore",))
        return {"ok": True}


class FakePlugs:
    devices = {"porch": "Porch Light"}

    def set_state(self, friendly, on):
        return {"ok": True, "say": "plug"}


class Brain:
    def __init__(self):
        self.systems, self.users, self.json_users, self.n = [], [], [], 0
        self.intent = {"action": "answer", "say": "An answer."}

    def chat_json(self, system, user, temperature=None):
        self.json_users.append(user)
        return dict(self.intent)

    def chat(self, system, user, temperature=None, history=None):
        self.n += 1
        self.systems.append(system)
        self.users.append(user)
        return f"Roast line number {self.n}, you magnificent disaster."


def make():
    cfg = Config(path=os.path.join(os.path.dirname(__file__), "__no_such_config__.json"))
    mem = Memory(Path(tempfile.mkdtemp(prefix="ha_roast_")) / "memory.json")
    b = Brain()
    r = Router(cfg, b, FakeLights(), FakePlugs(), mem, lambda: "PERSONA.", announce=None)
    return r, b


def roasted(b, before):
    return len(b.systems) > before and "comedy roast" in b.systems[-1] or (
        len(b.systems) > before and "RIGHT NOW: they asked you to roast" in b.systems[-1]) or (
        len(b.systems) > before and "asked you to roast THEM" in b.systems[-1])


print("== roast someone by name")
r, b = make()
k = len(b.systems)
res = r.handle("Roast Jordan", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Roast Jordan' roasts JORDAN (not the speaker)", "comedy roast of Jordan" in b.systems[-1] and
      "insulted you" not in b.systems[-1], b.systems[-1][-400:])
check("  ...a normal-length roast", "two or three" in b.systems[-1])
check("  ...nothing sexual about a third person", "Nothing sexual about Jordan" in b.systems[-1])
check("  ...her reply is the roast", res.get("say", "").startswith("Roast line number"), res)
check("  ...no intent step", not b.json_users)
r.handle("Roast Jordan longer.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Roast Jordan longer.' = a LONG roast, round 2", "five to seven" in b.systems[-1] and "round 2" in b.systems[-1],
      b.systems[-1][-300:])
res = r.handle("Another one.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Another one.' right after = another roast of Jordan (not ignored)", not res.get("ignored") and
      "comedy roast of Jordan" in b.systems[-1] and "round 3" in b.systems[-1], res)
r.handle("another one", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'another one' again = round 4", "round 4" in b.systems[-1])
r.handle("Roast for more.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Roast for more.' (= roast her more, misheard) = Jordan, long", "comedy roast of Jordan" in b.systems[-1] and
      "five to seven" in b.systems[-1], b.systems[-1][-300:])
r.handle("Roast her harder", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Roast her harder' = Jordan, long", "comedy roast of Jordan" in b.systems[-1] and "five to seven" in b.systems[-1])
r.handle("Roast Jordan hard.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Roast Jordan hard.' = long", "five to seven" in b.systems[-1])
res = r.handle("Why is Jordan a hooker?", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Why is Jordan a hooker?' = a roast of Jordan (exactly) that dodges the crude bit",
      "comedy roast of Jordan - " in b.systems[-1] and "OFF LIMITS" in b.systems[-1], b.systems[-1][-300:])
r.handle("Alexa, can you roast my sister please", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'roast my sister' = 'your Sister'", "comedy roast of your Sister" in b.systems[-1], b.systems[-1][-200:])
r.handle("Roast me", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Roast me' = roast the speaker", "asked you to roast THEM" in b.systems[-1])
r.handle("roast yourself", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'roast yourself' = self-roast", "roast YOURSELF" in b.systems[-1])

r2, b2 = make()
res = r2.handle("Roast her", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Roast her' with nobody roasted yet asks who", res.get("say") == "Roast who? Give me a name.", res)
for phrase in ("roast a chicken", "How long do I roast a turkey?", "roast the marshmallows", "roast coffee beans",
               "burn a CD", "I want to roast marshmallows tonight", "Another one."):
    k = len(b2.systems)
    r2.handle(phrase, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"not a roast: {phrase!r}", not any("comedy roast" in s or "roast THEM" in s or "roast YOURSELF" in s
                                               for s in b2.systems[k:]), b2.systems[k:])
r3, b3 = make()
r3.handle("Roast Jordan", "kitchen", satellite="Kitchen Echo", heard="wake")
r3.handle("turn on the living room lights", "kitchen", satellite="Kitchen Echo", heard="wake")
k = len(b3.systems)
r3.handle("another one", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'another one' after something ELSE was said is not a roast", not any("comedy roast" in s for s in b3.systems[k:]))
k = len(b3.systems)
r3.handle("Ava, you suck", "kitchen", satellite="Kitchen Echo", heard="wake")
check("an insult AT her is still a comeback (not a third-party roast)", not any("comedy roast of" in s for s in b3.systems[k:]))

print("== review round 2 (10-21)")


def kind_of(r, b, phrase, heard="wake"):
    k = len(b.systems)
    res = r.handle(phrase, "kitchen", satellite="Kitchen Echo", heard=heard)
    new = b.systems[k:]
    for s in new:
        m = re.search(r"comedy roast of (.+?) - ", s)
        if m:
            return "other:" + m.group(1), res
        if "asked you to roast THEM" in s:
            return "user", res
        if "roast YOURSELF" in s:
            return "self", res
    return None, res


import re  # noqa: E402
r6, b6 = make()
for phrase, want in [("Why is Jordan so annoying?", "other:Jordan"), ("Why is Jordan such a bitch?", "other:Jordan"),
                     ("why is Alex always so grumpy", "other:Alex"), ("Why's Jordan so lazy?", "other:Jordan"),
                     ("Why is Taylor Ann so annoying?", "other:Taylor Ann"), ("Why is my sister so slow?", "other:your Sister"),
                     ("Roast me instead", "user"), ("roast yourself for once", "self")]:
    got, _ = kind_of(r6, b6, phrase)
    check(f"{phrase!r} -> {want}", got == want, got)
r7, b7 = make()
for phrase in ("Why is my phone so slow?", "Why is my wifi so slow?", "Why is Netflix so slow?", "Why is the TV so loud?",
               "Why is gas so cheap?", "Why is my dog so lazy?", "Why is she so mean?", "Why is Alexa so dumb?",
               "say something funny about Mondays", "roast asparagus", "roast walnuts", "burn fat", "insult to injury",
               "roast for twenty minutes", "burn incense"):
    got, _ = kind_of(r7, b7, phrase)
    check(f"not a roast: {phrase!r}", got is None, got)
r8, b8 = make()
kind_of(r8, b8, "Roast Jordan")
for phrase in ("Roast her for longer", "Roast her a bit longer", "roast her too", "Do another one", "Another roast",
               "Make it longer", "Another one, longer", "Why is she so mean?"):
    got, _ = kind_of(r8, b8, phrase)
    check(f"after 'Roast Jordan': {phrase!r} -> Jordan again", got == "other:Jordan", got)
got, res = kind_of(r8, b8, "That's it.")
check("'That's it.' after a roast = enough (no new roast)", got is None, got)
r9, b9 = make()
got, res = kind_of(r9, b9, "roast Alex", heard="follow_up")
check("a fresh roast never fires on a follow-up (room chatter)", got is None, got)
kind_of(r9, b9, "Roast Jordan")
got, _ = kind_of(r9, b9, "another one", heard="follow_up")
check("...but 'another one' right after her roast does", got == "other:Jordan", got)
got, _ = kind_of(r9, b9, "Why is Jordan so dramatic", heard="follow_up")
check("...and a 'why is X ...' on a follow-up doesn't", got is None, got)
for q, ok_ in [("What type of music does Enya make?", False), ("Is Enya Spanish?", False), ("Who is Enya?", False),
               ("How do I type the Enya on a keyboard?", True), ("What is the Enya in Spanish?", True)]:
    got = "eñe" in _fix_heard(q, "User: what type of thing is it")
    check(f"eñe fix {'fires' if ok_ else 'leaves alone'}: {q!r}", got == ok_, _fix_heard(q))
check("unsmug keeps a one-word answer: 'Opposite of hard? Easy.'", persona.unsmug("Opposite of hard? Easy.") ==
      "Opposite of hard? Easy.", persona.unsmug("Opposite of hard? Easy."))
check("unsmug still drops a trailing insult there", persona.unsmug("Is it hot? Duh, you fucking idiot.") == "Is it hot? Duh.",
      persona.unsmug("Is it hot? Duh, you fucking idiot."))

print("== recent conversation + the eñe")
check("_fix_heard leaves a lone 'Enya' question alone", _fix_heard("What does the Enya do?") == "What does the Enya do?")
check("_fix_heard: Enya + keyboard (in the conversation) = the letter eñe",
      "eñe" in _fix_heard("What does the Enya mean?", "User: What is the Enya on a keyboard?"))
r4, b4 = make()
b4.intent = {"action": "answer", "say": "On a keyboard it's Alt plus 164."}
r4.handle("What is the Enya on a keyboard?", "kitchen", satellite="Kitchen Echo", heard="wake")
check("the intent step gets the eñe fix", "eñe (the Spanish letter ñ)" in b4.json_users[-1], b4.json_users[-1])
seen = {}
orig = web.answer
web.answer = lambda q, cfg, br, ps="", context="", said="": seen.update(q=q, context=context, said=said) or {"ok": True, "say": "It's the letter ñ."}
brain_mod.web.answer = web.answer
b4.intent = {"action": "search", "query": "eñe Spanish letter meaning", "say": ""}
r4.handle("What exactly does the Enya mean in Spanish?", "kitchen", satellite="Kitchen Echo", heard="wake")
web.answer = orig
brain_mod.web.answer = orig
check("a follow-up's intent step sees the recent conversation", "Recent conversation" in b4.json_users[-1] and
      "keyboard" in b4.json_users[-1], b4.json_users[-1])
check("the web answer gets the conversation too", "keyboard" in seen.get("context", ""), seen)
check("the persona no longer tacks on smug tags", "never tack a smug tag" in persona._CUSSING)

print("== swearing off: the roasts and comebacks keep it clean")
r5, b5 = make()
r5.cfg.set("personality.cussing", False)
r5.handle("Roast me", "kitchen", satellite="Kitchen Echo", heard="wake")
check("cussing off: a roast is asked to keep it clean", "Keep it clean" in b5.systems[-1]
      and "Swear" not in b5.systems[-1], b5.systems[-1][-300:])
r5.handle("roast yourself", "kitchen", satellite="Kitchen Echo", heard="wake")
check("cussing off: the self-roast loses the potty mouth", "potty mouth" not in b5.systems[-1]
      and "Keep it clean" in b5.systems[-1])
n = len(b5.systems)
res = r5._comeback("you're stupid", "kitchen")
check("cussing off: a comeback is always a clean roast from the model (never the sweary bank)",
      len(b5.systems) == n + 1 and "Keep it clean" in b5.systems[-1] and "filthy" not in b5.systems[-1]
      and res.get("say", "").startswith("Roast line number"), (b5.systems[-1][-200:], res))


def no_brain(*a, **k):
    raise brain_mod.BrainError("down")


b5.chat = no_brain
res = r5._comeback("you're stupid", "kitchen")
check("cussing off, model down: one of the clean lines", res.get("say") in brain_mod._CLEAN_COMEBACKS, res)
r6, b6 = make()
r6.cfg.set("personality.cussing", True)
r6.handle("Roast me", "kitchen", satellite="Kitchen Echo", heard="wake")
check("cussing on: the roast swears like before", "Swear plenty" in b6.systems[-1] and "Keep it clean" not in b6.systems[-1])
b6.chat = no_brain
res = r6._comeback("you're stupid", "kitchen")
check("cussing on, model down: the comeback bank like before", res.get("say") not in brain_mod._CLEAN_COMEBACKS
      and res.get("say"), res)

print("== intruder alert: silent, and a volume button stops the music")
r5, b5 = make()
music_calls = []
r5._music_fast = lambda low, room, sat, said_music=False: music_calls.append((low, room, sat)) or {"ok": True, "say": "Playing."}
res = r5.handle("Intruder alert.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Intruder alert.' is done quietly", res.get("silent") is True and res.get("say"), res)
check("  ...and remembered as on", bool(getattr(r5, "_alert", None)))
check("a volume button on ANOTHER Echo does nothing", r5.alert_button("bedroom", "Echo Show") is None)
res = r5.alert_button("kitchen", "Kitchen Echo")
check("a volume button on that Echo stops the alarm music (quietly)", res is not None and res.get("silent") is True, res)
check("  ...the next press is just the volume", r5.alert_button("kitchen", "Kitchen Echo") is None)
res = r5.handle("Back to normal.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Back to normal.' is done quietly", res.get("silent") is True, res)
check("  ...and the alert is over", not getattr(r5, "_alert", None) and r5.alert_button("kitchen", "Kitchen Echo") is None)
res = r5.handle("Back to normal.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Back to normal' with no alert on still says so", not res.get("silent") and "no alert" in res.get("say", ""), res)
music_calls.clear()
r5.handle("Intruder alert.", "kitchen", satellite="Kitchen Echo", heard="wake")
r5.handle("Back to normal.", "bedroom", satellite="Echo Show", heard="wake")
check("'back to normal' said to the OTHER Echo stops the alarm where it plays",
      ("stop", "kitchen", "Kitchen Echo") in music_calls, music_calls)
r5.handle("Intruder alert.", "kitchen", satellite="Kitchen Echo", heard="wake")
r5._alert["t"] -= 3600
check("a volume press an hour after the alert is just the volume", r5.alert_button("kitchen", "Kitchen Echo") is None)
r5._music_fast = lambda low, room, sat, said_music=False: {"ok": False, "say": "No music library here."}
res = r5.handle("Intruder alert.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("an intruder alert whose music didn't start says so (not silent)", not res.get("silent") and "didn't start" in
      res.get("say", ""), res)
r5.lights._saved = None
r5._alert = {"room": "kitchen", "satellite": "Kitchen Echo", "t": time.time(), "music_off": False}
music_calls.clear()
r5._music_fast = lambda low, room, sat, said_music=False: music_calls.append((low, room, sat)) or {"ok": True}
res = r5.handle("Back to normal.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("alert on but no bulb answered: 'back to normal' still kills the alarm music", ("stop", "kitchen", "Kitchen Echo")
      in music_calls and res.get("silent"), (music_calls, res))
from houseava.config import Config as _C  # noqa: E402
import json as _j  # noqa: E402
p_ = Path(tempfile.mkdtemp(prefix="ha_cfg_")) / "c.json"
p_.write_text(_j.dumps({"voice": {"stop_word": 0, "echo_canceller": "WebRTC"}}), encoding="utf-8")
c_ = _C(path=str(p_))
check("a config saved before 10-20 picks up stop_word 0.6 + the built-in canceller",
      c_.get("voice.stop_word") == 0.6 and c_.get("voice.echo_canceller") == "Built-in",
      (c_.get("voice.stop_word"), c_.get("voice.echo_canceller")))

print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
