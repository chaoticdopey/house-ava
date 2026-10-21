"""Light commands, every way people say them (the goal: "light commands must work 100%", every variation).

Drives the real router (Brain._fast -> lightcmd -> Lights) from the kitchen Echo (no bulbs), the bedroom Echo and the
app's chat (no room), against a fake house of 6 WiZ bulbs that keeps state. Each generated sentence has an answer key
built HERE from the sentence's parts (not from the parser) - which bulbs must change and how.

    .venv/Scripts/python.exe tests/test_lights_every_way.py          (-v prints every failure)
"""
import itertools
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="avalw-")

import houseava.lights as L                      # noqa: E402
from houseava.app import build_router            # noqa: E402
from houseava.config import Config               # noqa: E402

VERBOSE = "-v" in sys.argv
BULB = {"living room 1": "192.0.2.101", "living room 2": "192.0.2.102", "dining room 1": "192.0.2.103",
        "dining room 2": "192.0.2.104", "bedroom 1": "192.0.2.105", "bedroom 2": "192.0.2.106"}
ROOM = {"living room": ["living room 1", "living room 2"], "dining room": ["dining room 1", "dining room 2"],
        "bedroom": ["bedroom 1", "bedroom 2"]}
ALL = set(BULB.values())
IP = {ip: n for n, ip in BULB.items()}

state: dict[str, dict] = {}
sent: list = []


def reset(on=False, dimming=100):
    for ip in BULB.values():
        state[ip] = {"state": on, "dimming": dimming}
    sent.clear()
    getattr(r.lights, "_recent", {}).clear()    # each case starts fresh: no "it/them" left over from the case before


def fake_send(ip, payload, timeout=1.2):
    sent.append((ip, payload))
    if payload.get("method") == "getPilot":
        return {"method": "getPilot", "result": dict(state[ip])}
    p = payload.get("params", {})
    if "state" in p:
        state[ip]["state"] = bool(p["state"])
    if "dimming" in p:
        state[ip]["dimming"] = p["dimming"]
    for k in ("r", "g", "b", "temp"):
        if k in p:
            state[ip][k] = p[k]
    return {"method": "setPilot", "result": {"success": True}}


L._send = fake_send
r = build_router(Config())
r.lights.cfg.data.setdefault("lights", {})["bulbs"] = dict(BULB)
r.lights.cfg.data["lights"]["rooms"] = {k: list(v) for k, v in ROOM.items()}

fails: list = []
count = 0


def changed(kind="set"):
    return {ip for ip, p in sent if p.get("method") == "setPilot"}


def say(text, room):
    sent.clear()
    try:
        res = r.handle(text, room, satellite=("Kitchen Echo" if room == "kitchen" else "Echo Show"), heard="wake")
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "say": f"CRASH {type(e).__name__}: {e}"}
    return res


def check(text, room, cond, why):
    global count
    count += 1
    if not cond:
        fails.append((room or "(app)", text, why))


def here(room):
    return {BULB[n] for n in ROOM[room]} if room in ROOM else set(ALL)


# ---------------------------------------------------------------- vocabulary
ON_V = ["turn on", "switch on", "flip on", "power on", "put on", "turn-on", "Turn On"]
OFF_V = ["turn off", "switch off", "shut off", "flip off", "power off", "turn out", "put out", "kill", "cut",
         "shut", "turn of", "Turn Off", "turnoff"]
SPLIT = {"on": ["turn {t} on", "switch {t} on", "flip {t} on", "put {t} on", "get {t} on"],
         "off": ["turn {t} off", "switch {t} off", "shut {t} off", "flip {t} off", "put {t} out", "turn {t} out",
                 "get {t} off"]}
TAIL = {"on": ["{t} on", "{t} on please", "{t} on now"], "off": ["{t} off", "{t} out", "{t} off please", "{t} off now"]}
PRE = ["", "alexa ", "Alexa, ", "hey ava ", "ava ", "can you ", "could you ", "would you ", "please ", "can you please ",
       "hey ava, can you please ", "i need you to ", "go ahead and ", "um ", "okay ", "just "]
POST = ["", " please", ", please", " for me", " thanks", " thank you", " now", " right now", " real quick"]
PUNCT = ["{s}", "{S}.", "{S}!", "{S}?", "{S}, please.", "{s}."]

ALL_T = ["all lights", "all the lights", "all of the lights", "every light", "every single light", "all lights in the house",
         "the lights in every room", "all the lights in the house", "every light in the house", "everything",
         "the whole house", "the lights everywhere", "all the lights everywhere", "the house lights",
         "the lights in the whole house", "all the lamps", "all bulbs", "every lamp", "all the bulbs", "all lighting"]
HERE_T = ["the lights", "lights", "the light", "light", "the lights in here", "the lights in this room", "my lights",
          "our lights", "these lights", "the lamps", "the lamp"]
ROOM_FORMS = ["the {r} lights", "{r} lights", "the {r} light", "the lights in the {r}", "all the {r} lights",
              "the {r} lamps", "the {r}", "both {r} lights", "the lights in {r}", "{r} light", "the {r} lighting"]
ALIASES = {"living room": ["living room", "lounge", "front room", "family room", "livingroom"],
           "dining room": ["dining room", "dining", "dinning room"],
           "bedroom": ["bedroom", "bed room", "master bedroom"]}
BULB_FORMS = ["{r} light {n}", "{r} light number {n}", "the {r} light {n}", "light {n} in the {r}", "{r} lamp {n}",
              "the {ord} {r} light", "{r} bulb {n}", "{r} light, number {n}", "{r} light #{n}"]
NUMW = {1: ["one", "1"], 2: ["two", "2"]}
ORD = {1: "first", 2: "second"}
SATS = ["kitchen", "bedroom", ""]


def variants(core, n_pre=3, n_post=2, n_punct=2, seed=0):
    rnd = random.Random(hash((core, seed)) & 0xffffffff)
    out = {core}
    for _ in range(n_pre):
        p = rnd.choice(PRE)
        for _ in range(n_post):
            q = rnd.choice(POST)
            s = f"{p}{core}{q}".strip()
            for f in rnd.sample(PUNCT, n_punct):
                out.add(f.format(s=s, S=s[:1].upper() + s[1:]))
    return sorted(out)


def expect_onoff(text, room, on, want):
    reset(on=not on)
    res = say(text, room)
    got = changed()
    ok = got == want and all(state[ip]["state"] == on for ip in want)
    check(text, room, ok, f"want {sorted(IP[i] for i in want)} {'on' if on else 'off'}, changed {sorted(IP[i] for i in got)}; said {res.get('say')!r}")


# ---------------------------------------------------------------- on / off, every target, every order
for on in (True, False):
    word = "on" if on else "off"
    verbs = ON_V if on else OFF_V
    for room in SATS:
        groups = [(t, ALL) for t in ALL_T] + [(t, here(room)) for t in HERE_T]
        for rname, al in ALIASES.items():
            for a in al:
                for f in ROOM_FORMS:
                    groups.append((f.format(r=a), {BULB[n] for n in ROOM[rname]}))
        for tgt, want in groups:
            cores = [f"{v} {tgt}" for v in verbs] + [s.format(t=tgt) for s in SPLIT[word]] + \
                    [s.format(t=tgt) for s in TAIL[word]]
            if not tgt.startswith("the ") and tgt not in ("everything",):
                cores.append(f"{word} {tgt}")                     # first word clipped by the Echo: "off all lights"
            for core in cores:
                if core.startswith(("kill", "cut", "shut ", "turn out", "put out")) and on:
                    continue
                for v in variants(core, n_pre=1, n_post=1, n_punct=1):
                    expect_onoff(v, room, on, want)

# single bulbs
for on in (True, False):
    for room in SATS:
        for rname in ROOM:
            for n in (1, 2):
                for f in BULB_FORMS:
                    for nw in NUMW[n]:
                        tgt = f.format(r=rname, n=nw, ord=ORD[n])
                        for core in (f"turn {'on' if on else 'off'} {tgt}", f"turn {tgt} {'on' if on else 'off'}",
                                     f"{tgt} {'on' if on else 'off'}"):
                            expect_onoff(core, room, on, {BULB[f'{rname} {n}']})
# "light two" said in a room with bulbs = that room's bulb; from the kitchen = ask which room
for on in (True, False):
    w = "on" if on else "off"
    expect_onoff(f"turn {w} light two", "bedroom", on, {BULB["bedroom 2"]})
    expect_onoff(f"turn {w} light number one", "bedroom", on, {BULB["bedroom 1"]})
    reset(on=not on)
    res = say(f"turn {w} light two", "kitchen")
    check(f"turn {w} light two", "kitchen", not changed() and "which room" in res.get("say", "").lower(), res.get("say"))
# several rooms / bulbs
MULTI = [("the living room and dining room lights", ["living room", "dining room"]),
         ("the living room and the bedroom lights", ["living room", "bedroom"]),
         ("the lights in the living room and the dining room", ["living room", "dining room"]),
         ("living room, dining room and bedroom lights", ["living room", "dining room", "bedroom"]),
         ("the dining and living room lights", ["dining room", "living room"]),
         ("bedroom and living room", ["bedroom", "living room"])]
for on in (True, False):
    w = "on" if on else "off"
    for tgt, rs in MULTI:
        want = {BULB[n] for x in rs for n in ROOM[x]}
        for core in (f"turn {w} {tgt}", f"turn {tgt} {w}", f"{tgt} {w}"):
            for room in SATS:
                expect_onoff(core, room, on, want)
    for core, want in ((f"turn {w} bedroom lights one and two", {BULB["bedroom 1"], BULB["bedroom 2"]}),
                       (f"turn {w} bedroom light 1 and 2", {BULB["bedroom 1"], BULB["bedroom 2"]}),
                       (f"turn {w} one of the bedroom lights", None), (f"turn {w} just one bedroom light", None)):
        for room in SATS:
            if want is None:
                reset(on=not on)
                res = say(core, room)
                got = changed()
                check(core, room, len(got) == 1 and got <= here("bedroom"), f"want one bedroom bulb, got {got} {res.get('say')!r}")
            else:
                expect_onoff(core, room, on, want)
# all except
for on in (True, False):
    w = "on" if on else "off"
    for core, ex in ((f"turn {w} all the lights except the bedroom", "bedroom"),
                     (f"turn {w} every light but the living room", "living room"),
                     (f"turn {w} all lights except in the dining room", "dining room"),
                     (f"turn all the lights {w} except the bedroom", "bedroom")):
        for room in SATS:
            expect_onoff(core, room, on, ALL - {BULB[n] for n in ROOM[ex]})

# ---------------------------------------------------------------- the 10-16 transcript, exactly as whisper wrote it
for text, room, on, want in (("Turn off all lights.", "kitchen", False, ALL), ("Turn every light off.", "kitchen", False, ALL),
                             ("Shut off the lights.", "kitchen", False, ALL), ("Turn off all lights", "", False, ALL),
                             ("Turn off all lights.", "bedroom", False, ALL), ("Shut off the lights.", "bedroom", False, here("bedroom")),
                             ("Lights out.", "kitchen", False, ALL), ("Lights out.", "bedroom", False, here("bedroom")),
                             ("Kill the lights.", "kitchen", False, ALL), ("Lights on.", "kitchen", True, ALL),
                             ("Off all lights.", "kitchen", False, ALL), ("All lights off.", "kitchen", False, ALL),
                             ("Everything off.", "kitchen", False, ALL), ("Turn everything off.", "bedroom", False, ALL),
                             ("Turn all the lights out.", "kitchen", False, ALL), ("Leave the lights on.", "kitchen", True, ALL),
                             ("Keep the bedroom lights on.", "kitchen", True, here("bedroom"))):
    expect_onoff(text, room, on, want)

# ---------------------------------------------------------------- no on/off heard -> toggle (flip the way they are)
TOGGLES = ["all lights.", "All lights.", "the lights", "lights", "Lights.", "hit the lights", "toggle the lights",
           "get the lights", "flip the lights", "toggle all lights", "the bedroom lights", "Bedroom lights.",
           "switch the lights", "toggle the living room lights", "all the lights"]
for text in TOGGLES:
    for room in SATS:
        low = text.lower()
        want = here("bedroom") if "bedroom" in low else ({BULB[n] for n in ROOM["living room"]} if "living" in low else
                                                         (ALL if "all" in low else here(room)))
        for start_on in (True, False):
            reset(on=start_on)
            res = say(text, room)
            got = changed()
            ok = got == want and all(state[ip]["state"] == (not start_on) for ip in want)
            check(text, room, ok, f"toggle from {'on' if start_on else 'off'}: want {sorted(IP[i] for i in want)} flipped, "
                                  f"got {sorted(IP[i] for i in got)} {res.get('say')!r}")

# ---------------------------------------------------------------- brightness
for room in SATS:
    for text, want_pct, tgt in (("set the lights to 50 percent", 50, None), ("Set the bedroom lights to 40%.", 40, "bedroom"),
                                ("dim the lights to 20", 20, None), ("lights to 70 percent", 70, None),
                                ("make the living room 30 percent", 30, "living room"), ("bedroom lights at 60%", 60, "bedroom"),
                                ("set all the lights to 100 percent", 100, "all"), ("set every light to fifty percent", 50, "all"),
                                ("put the lights at half brightness", 50, None), ("full brightness", 100, None),
                                ("set the lights to full brightness", 100, None), ("lights to max", 100, None),
                                ("set the dining room lights to twenty five percent", 25, "dining room"),
                                ("bedroom light two to 40 percent", 40, "b2"), ("Dim bedroom light, number two to 40%.", 40, "b2"),
                                ("set the lights to the lowest", 10, None), ("brightness 80 percent", 80, None)):
        want = (ALL if tgt == "all" else {BULB["bedroom 2"]} if tgt == "b2" else
                {BULB[n] for n in ROOM[tgt]} if tgt else here(room))
        reset(on=False)
        res = say(text, room)
        got = changed()
        ok = got == want and all(state[ip]["state"] and state[ip]["dimming"] == want_pct for ip in want)
        check(text, room, ok, f"want {sorted(IP[i] for i in want)} at {want_pct}%, got {sorted(IP[i] for i in got)} "
                              f"{[state[i]['dimming'] for i in sorted(got)]} {res.get('say')!r}")
    for text, up, tgt in (("dim the lights", False, None), ("Dim the bedroom lights.", False, "bedroom"),
                          ("make it darker", False, None), ("turn the lights down", False, None),
                          ("turn down the lights", False, None), ("lower the lights", False, None),
                          ("it's too bright in here", False, None), ("brighten the lights", True, None),
                          ("make the lights brighter", True, None), ("turn the lights up", True, None),
                          ("turn up the living room lights", True, "living room"), ("it's too dark in here", True, None),
                          ("bring the lights down a bit", False, None), ("brighter please", True, None)):
        want = {BULB[n] for n in ROOM[tgt]} if tgt else here(room)
        reset(on=True, dimming=60)
        res = say(text, room)
        got = changed()
        ok = got == want and all(state[ip]["state"] and (state[ip]["dimming"] > 60 if up else state[ip]["dimming"] < 60)
                                 for ip in want)
        check(text, room, ok, f"want {sorted(IP[i] for i in want)} {'up' if up else 'down'} from 60, got "
                              f"{[(IP[i], state[i]['dimming']) for i in sorted(got)]} {res.get('say')!r}")

# ---------------------------------------------------------------- colors
for room in SATS:
    for text, tgt, key in (("make the lights blue", None, "b"), ("Turn the bedroom lights red.", "bedroom", "r"),
                           ("set the living room to green", "living room", "g"), ("change all the lights to purple", "all", "r"),
                           ("make every light pink", "all", "r"), ("set the lights to warm white", None, "temp"),
                           ("make the dining room lights orange", "dining room", "r"), ("lights blue", None, "b"),
                           ("change the bedroom light to daylight", "bedroom", "temp"), ("make it red in here", None, "r")):
        want = ALL if tgt == "all" else ({BULB[n] for n in ROOM[tgt]} if tgt else here(room))
        reset(on=False)
        res = say(text, room)
        got = changed()
        ok = got == want and all(state[ip]["state"] and key in state[ip] for ip in want)
        check(text, room, ok, f"want {sorted(IP[i] for i in want)} colored, got {sorted(IP[i] for i in got)} {res.get('say')!r}")

# ---------------------------------------------------------------- status questions (read only, nothing changes)
for room in SATS:
    for text in ("what lights are on?", "Are the lights on?", "which lights are on", "are any lights on",
                 "is the bedroom light on?", "did I leave any lights on?", "What lights are on right now?",
                 "what lights are still on", "is anything on", "are all the lights off?", "light status",
                 "can you tell me which lights are on", "are the bedroom lights on or off", "which lights did I leave on"):
        reset(on=False)
        state[BULB["bedroom 1"]]["state"] = True
        res = say(text, room)
        said = (res.get("say") or "").lower()
        check(text, room, not changed() and "bedroom" in said and res.get("ok"),
              f"status must not change anything and must mention bedroom 1: {res.get('say')!r} changed {changed()}")
    for text, rname in (("Are the living room lights off?", "living room"), ("is the dining room light on",
                                                                          "dining room")):
        reset(on=False)
        state[BULB["bedroom 1"]]["state"] = True
        res = say(text, room)
        said = (res.get("say") or "").lower()
        check(text, room, not changed() and rname in said and "off" in said and res.get("ok"),
              f"must answer about the {rname} (off): {res.get('say')!r}")

# ---------------------------------------------------------------- real-world phrasings from real use
REAL = [  # (text as whisper wrote it, room, expect)  expect = ("on"|"off", set) / ("toggle", set) / ("ask",) / None
    ("Turn on bedroom light 1", "", ("on", {BULB["bedroom 1"]})),
    ("Turn off bedroom light 2", "", ("off", {BULB["bedroom 2"]})),
    ("Turn on bedroom light number 1", "", ("on", {BULB["bedroom 1"]})),
    ("Turn on dining room light", "kitchen", ("on", here("dining room"))),
    ("So, turn on living room lights too.", "kitchen", ("on", here("living room"))),
    ("Alexa turn off dining room lights, you know, let it just kind of do a thing and then you're just gonna start",
     "kitchen", ("off", here("dining room"))),
    ("Turn off turn on living room light", "kitchen", ("on", here("living room"))),
    ("Turn on living room.", "kitchen", ("on", here("living room"))),
    ("Turn off the ring room lights", "kitchen", ("ask",)),
    ("Turn off living room light", "kitchen", ("off", here("living room"))),
    ("turn off dining room lights.", "kitchen", ("off", here("dining room"))),
    ("Turn off living light", "kitchen", ("off", here("living room"))),
    ("Turn off living room light and turn on dead light", "kitchen", ("both", here("living room"), here("dining room"))),
    ("Turn on deadlight.", "kitchen", ("on", here("dining room"))),
    ("Turn all living room light and turn off dining room light", "kitchen",
     ("both_toggle", here("living room"), here("dining room"))),
    ("Turn on victim light, number one.", "kitchen", ("on", {BULB["bedroom 1"]})),
    ("turn on bedroom light one.", "kitchen", ("on", {BULB["bedroom 1"]})),
    ("Turn off bedroom light number two.", "kitchen", ("off", {BULB["bedroom 2"]})),
    ("turn on bit the light one", "kitchen", ("on", {BULB["bedroom 1"]})),
    ("We're on better light one.", "kitchen", ("on", {BULB["bedroom 1"]})),
    ("Turn on Living Room Life.", "bedroom", ("on", here("living room"))),
    ("all lights.", "kitchen", ("toggle", ALL)),
    ("Turn off all lights.", "kitchen", ("off", ALL)),
    ("Turn every light off.", "kitchen", ("off", ALL)),
    ("Shut off the lights.", "kitchen", ("off", ALL)),
    ("can you turn my bedroom light off please", "kitchen", ("off", here("bedroom"))),
    ("Would you turn on bedroom light number one?", "kitchen", ("on", {BULB["bedroom 1"]})),
    ("on bedroom light number one.", "kitchen", ("on", {BULB["bedroom 1"]})),
    ("on bedroom light one.", "kitchen", ("on", {BULB["bedroom 1"]})),
    ("and they're off bedroom light number two.", "kitchen", ("off", {BULB["bedroom 2"]})),
    ("Ava, well, hey Ava, turn off bedroom lights.", "kitchen", ("off", here("bedroom"))),
    ("second bedroom light.", "kitchen", ("toggle", {BULB["bedroom 2"]})),
    ("I turn on bedroom light one.", "kitchen", ("on", {BULB["bedroom 1"]})),
    ("They're not bedroom light ones.", "kitchen", ("toggle", {BULB["bedroom 1"]})),
    ("Turn off living room and dining room lights.", "kitchen", ("off", here("living room") | here("dining room"))),
    ("Turn it off by shining room lights.", "kitchen", ("off", here("dining room"))),
    ("turn of bedroom light", "kitchen", ("off", here("bedroom"))),
    ("turn off bedroom light too.", "kitchen", ("off", {BULB["bedroom 2"]})),
    ("Flash bedroom light 1", "kitchen", ("flash", {BULB["bedroom 1"]})),
    # (was "too garbled - must not guess"; the kept audio showed it IS "turn <on/off> living room light" - the speaker said
    # "Turn on Living Room Life" 20 s later - and 10-17's "We're not dining with lights." was "turn off dining room
    # lights": "we're not" = turn (either way) -> a toggle, like "They're not bedroom light ones" above)
    ("We're not living on light.", "kitchen", ("toggle", here("living room"))),
    ("Let's go!", "kitchen", None), ("Does that mean it's going by?", "kitchen", None),
    ("but it is not off.", "kitchen", None), ("Don't follow your name.", "kitchen", None),
]
for text, room, exp in REAL:
    if exp is None:
        reset(on=True)
        res = say(text, room)
        check(text, room, not changed(), f"must not touch a bulb: changed {sorted(IP[i] for i in changed())} {res.get('say')!r}")
        continue
    kind = exp[0]
    if kind in ("on", "off"):
        expect_onoff(text, room, kind == "on", exp[1])
    elif kind == "toggle":
        for start_on in (True, False):
            reset(on=start_on)
            res = say(text, room)
            ok = changed() == exp[1] and all(state[ip]["state"] == (not start_on) for ip in exp[1])
            check(text, room, ok, f"toggle: want {sorted(IP[i] for i in exp[1])}, got {sorted(IP[i] for i in changed())} {res.get('say')!r}")
    elif kind == "ask":
        reset(on=True)
        res = say(text, room)
        check(text, room, not changed() and "living room or the dining room" in (res.get("say") or ""),
              f"must ask which room: {res.get('say')!r} changed {changed()}")
    elif kind == "both":
        reset(on=True)
        for ip in exp[2]:
            state[ip]["state"] = False
        res = say(text, room)
        ok = all(not state[ip]["state"] for ip in exp[1]) and all(state[ip]["state"] for ip in exp[2])
        check(text, room, ok, f"both clauses: {res.get('say')!r} state {[(IP[i], state[i]['state']) for i in sorted(exp[1] | exp[2])]}")
    elif kind == "both_toggle":
        reset(on=True)
        for ip in exp[1]:
            state[ip]["state"] = False
        res = say(text, room)
        ok = all(state[ip]["state"] for ip in exp[1]) and all(not state[ip]["state"] for ip in exp[2])
        check(text, room, ok, f"toggle clause + off clause: {res.get('say')!r}")
    elif kind == "flash":
        reset(on=False)
        res = say(text, room)
        import time as _t
        _t.sleep(3.2)
        got = {ip for ip, p in sent if p.get("method") == "setPilot"}
        check(text, room, res.get("ok") and got == exp[1] and not state[BULB["bedroom 1"]]["state"],
              f"flash must blink only bedroom 1 and put it back: {res.get('say')!r} {sorted(IP[i] for i in got)}")

# ---------------------------------------------------------------- the model can't fake a light result
orig_chat_json = r.brain.chat_json
r.brain.chat_json = lambda sys_, ctx: {"action": "answer", "say": "All lights are now on, brightening up the place."}
reset(on=False)
res = say("the lights look a bit weird", "kitchen")
check("the lights look a bit weird", "kitchen", not changed() and "now on" not in (res.get("say") or ""),
      f"a made-up 'now on' must not be spoken: {res.get('say')!r}")
r.brain.chat_json = lambda sys_, ctx: {"action": "light", "target": "kitchen", "on": "false", "say": "Done!"}
reset(on=True)
res = say("lights please kitchen thing", "kitchen")
check("model: light/kitchen/'false'", "kitchen", changed() == ALL and all(not state[i]["state"] for i in ALL),
      f"model target = the lightless room -> all; 'false' = off: {res.get('say')!r} {sorted(IP[i] for i in changed())}")
r.brain.chat_json = lambda sys_, ctx: {"action": "brightness", "target": "bedroom", "percent": "40%", "say": ""}
reset(on=False)
res = say("make the bedroom kinda medium", "kitchen")
check("model: brightness '40%'", "kitchen", all(state[i]["dimming"] == 40 for i in here("bedroom")),
      f"'40%' must not crash: {res.get('say')!r}")
r.brain.chat_json = orig_chat_json

# ---------------------------------------------------------------- later / for a while
for room in SATS:
    for text, secs in (("turn off the lights in 10 minutes", 600), ("Turn off all lights in five minutes.", 300),
                       ("turn the bedroom lights off in an hour", 3600), ("lights off in 30 seconds", 30),
                       ("turn off the lights in half an hour", 1800)):
        reset(on=True)
        res = say(text, room)
        check(text, room, not changed() and "in " in (res.get("say") or "") and res.get("ok"),
              f"must schedule, not act now: {res.get('say')!r} changed {changed()}")
    for t_ in list(r.lights._timers):
        t_.cancel()
    r.lights._timers.clear()

# ---------------------------------------------------------------- NOT light commands: must change no bulb
NOT = ["turn off the music", "turn the music off", "turn off the tv", "turn the tv on", "turn up the volume",
       "turn down the volume", "turn off the alarm", "turn off your mic", "turn the mic up", "pause the music",
       "play lights by ellie goulding", "play the song lights", "don't turn off the lights", "do not turn on the lights",
       "who turned off the lights?", "why are the lights off?", "I turned off the lights", "remind me to turn off the lights",
       "remember to turn off the lights", "set a timer for 10 minutes", "what time is it", "tell me a joke",
       "the lights look nice", "I love these lights", "turn off the porch light", "turn on the porch",
       "turn off bluetooth", "stop", "turn it off", "shut up", "turn off the fan", "turn on the heater",
       "turn the heat up", "lights, camera, action", "the light is green", "what's the weather",
       "never turn off the bedroom lights", "how do I turn off the lights?", "who left the lights on?"]
for text in NOT:
    for room in SATS:
        reset(on=True)
        res = say(text, room)
        check(text, room, not changed(), f"must not touch any bulb: changed {sorted(IP[i] for i in changed())} {res.get('say')!r}")

print(f"{count - len(fails)}/{count} passed")
if fails:
    by = {}
    for room, text, why in fails:
        by.setdefault(why.split(':')[0][:40], []).append((room, text, why))
    shown = fails if VERBOSE else fails[:60]
    for room, text, why in shown:
        print(f"  [FAIL] ({room}) {text!r}: {why}")
    if not VERBOSE and len(fails) > 60:
        print(f"  ... {len(fails) - 60} more (-v)")
    sys.exit(1)
