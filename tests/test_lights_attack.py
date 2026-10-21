"""The 10-16 adversarial review's sentences (~2,000 tried; these are the ones that broke parser v2) - so they can
never come back. Same fake house as test_lights_every_way.py.

    .venv/Scripts/python.exe tests/test_lights_attack.py        (-v prints every check)
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="avala-")
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"

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
state, sent = {}, []


def reset(on=False, dimming=60):
    for ip in BULB.values():
        state[ip] = {"state": on, "dimming": dimming}
    sent.clear()


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
fails, count = [], 0


def rm(*names):
    out = set()
    for n in names:
        out |= {BULB[b] for b in ROOM[n]} if n in ROOM else {BULB[n]}
    return out


def here(room):
    return rm(room) if room in ROOM else set(ALL)


def changed():
    return {ip for ip, p in sent if p.get("method") == "setPilot"}


def say(text, room):
    sent.clear()
    try:
        return r.handle(text, room, satellite=("Kitchen Echo" if room == "kitchen" else "Echo Show"), heard="wake")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "say": f"CRASH {type(e).__name__}: {e}"}


def check(text, room, cond, why):
    global count
    count += 1
    if not cond:
        fails.append((room or "(app)", text, why))
    elif VERBOSE:
        print(f"  ok ({room or 'app'}) {text}")


SATS = ["kitchen", "bedroom", ""]
H = "HERE"      # stands for here(room)


def want_set(w, room):
    return here(room) if w == H else w


def expect(text, room, kind, w=None, start_on=None, pct=None):
    """kind: on / off / toggle / none / set / brighter / dimmer / later / status / ask / color / nolights"""
    so = start_on if start_on is not None else (kind == "off" or kind == "dimmer" or kind == "brighter")
    reset(on=so, dimming=60)
    res = say(text, room)
    got, said = changed(), (res.get("say") or "")
    w = want_set(w, room) if w is not None else None
    tag = f"said {said!r} changed {sorted(IP[i] for i in got)}"
    if kind in ("on", "off"):
        ok = got == w and all(state[i]["state"] == (kind == "on") for i in w)
    elif kind == "toggle":
        ok = got == w and all(state[i]["state"] == (not so) for i in w)
    elif kind == "none":
        ok = not got and not said.startswith("CRASH")
    elif kind == "set":
        ok = got == w and all(state[i]["dimming"] == pct for i in w)
    elif kind in ("brighter", "dimmer"):
        ok = got == w and all((state[i]["dimming"] > 60) == (kind == "brighter") and state[i]["dimming"] != 60 for i in w)
    elif kind == "later":
        ok = not got and res.get("ok") and " in " in said
    elif kind == "status":
        ok = not got and res.get("ok")
    elif kind == "ask":
        ok = not got and "living room or the dining room" in said
    elif kind == "color":
        ok = got == w and all(state[i]["state"] for i in w)
    elif kind == "nolights":
        ok = not got and "don't have any lights" in said
    else:
        ok = False
    check(text, room, ok, f"want {kind} {sorted(IP[i] for i in w) if w else ''} {pct or ''}; {tag}")
    for t_ in list(r.lights._timers):
        t_.cancel()
    r.lights._timers.clear()


# ---- two commands in one sentence (commas, full stops, "and the ... off") ----
for room in SATS:
    expect("Turn off the bedroom lights, turn on the dining room lights.", room, "two")  # handled below
TWO = [("Turn off the bedroom lights, turn on the dining room lights.", rm("bedroom"), rm("dining room")),
       ("Turn off the bedroom lights. Turn on the living room lights.", rm("bedroom"), rm("living room")),
       ("Bedroom off, living room on.", rm("bedroom"), rm("living room")),
       ("Living room lights off and bedroom lights on.", rm("living room"), rm("bedroom")),
       ("Can you turn the living room light off and the bedroom light on?", rm("living room"), rm("bedroom")),
       ("Bedroom light two off and bedroom light one on.", rm("bedroom 2"), rm("bedroom 1")),
       ("turn the dining room light off and the bedroom light on", rm("dining room"), rm("bedroom")),
       ("Living room lights off, bedroom lights on.", rm("living room"), rm("bedroom")),
       ("Turn off the dining room and on the living room.", rm("dining room"), rm("living room")),
       ("Turn off living room light and turn on dead light", rm("living room"), rm("dining room")),
       ("Switch off the living room lights, switch on the bedroom lights.", rm("living room"), rm("bedroom"))]
fails.clear()
count = 0
for text, offs, ons in TWO:
    for room in SATS:
        reset(on=True)
        for ip in ons:
            state[ip]["state"] = False
        res = say(text, room)
        ok = all(not state[i]["state"] for i in offs) and all(state[i]["state"] for i in ons) and \
            changed() == offs | ons
        check(text, room, ok, f"want off {sorted(IP[i] for i in offs)} + on {sorted(IP[i] for i in ons)}; said "
                              f"{res.get('say')!r} changed {sorted(IP[i] for i in changed())}")

# ---- bulbs by number / own name / per-room numbers ----
for room in SATS:
    expect("Turn off bedroom light one and dining room light two.", room, "off", rm("bedroom 1", "dining room 2"))
    expect("Turn on living room light 1 and bedroom light 2.", room, "on", rm("living room 1", "bedroom 2"))
    expect("Turn off bedroom light one and bedroom light two.", room, "off", rm("bedroom"))
    expect("turn off light one and light two in the bedroom", room, "off", rm("bedroom"))
    expect("Turn off bedroom light 1 & 2.", room, "off", rm("bedroom"))
    expect("turn on bedroom 2", room, "on", rm("bedroom 2"))
    expect("Turn off bedroom 1.", room, "off", rm("bedroom 1"))
    expect("turn off dining room 2", room, "off", rm("dining room 2"))
    expect("turn on the bedroom 2 light", room, "on", rm("bedroom 2"))
    expect("Turn on all the lights except bedroom light one.", room, "on", ALL - rm("bedroom 1"))
    expect("Turn off everything except bedroom light 2.", room, "off", ALL - rm("bedroom 2"))
# routines / the API use bulb names as targets
for target, want in (("bedroom 2", rm("bedroom 2")), ("living room 2", rm("living room 2")),
                     ("dining room 1", rm("dining room 1")), ("bedroom", rm("bedroom")), ("all", ALL)):
    reset(on=False)
    r.lights.set_state(target, True, "")
    check(f"set_state({target!r})", "routine", changed() == want, f"changed {sorted(IP[i] for i in changed())}")

# ---- exceptions / the other rooms ----
expect("turn off the living room lights but not the dining room", "kitchen", "off", rm("living room"))
expect("Turn off the living room lights, but not the bedroom.", "kitchen", "off", rm("living room"))
expect("turn off the bedroom lights but leave the living room on", "kitchen", "off", rm("bedroom"))
expect("Not the bedroom, the living room lights off.", "kitchen", "off", rm("living room"))
expect("Turn off the lights in the other rooms.", "bedroom", "off", ALL - rm("bedroom"))
expect("Turn off all the other lights.", "bedroom", "off", ALL - rm("bedroom"))
expect("Turn off the lights everywhere except here.", "bedroom", "off", ALL - rm("bedroom"))
expect("Turn off all the lights except the bedroom", "kitchen", "off", ALL - rm("bedroom"))
# "but" = except when nothing follows it but a place (dropping the "but" switched ONLY that room - the opposite)
for room in SATS:
    expect("turn on every light but the living room", room, "on", ALL - rm("living room"))
    expect("Turn off all the lights but the bedroom and the dining room.", room, "off", rm("living room"))

# ---- brightness ----
for room in SATS:
    expect("brighten the lights by 20 percent", room, "brighter", H)
    expect("turn the lights up 20 percent", room, "brighter", H)
    expect("dim the lights by 10 percent", room, "dimmer", H)
    expect("lights 50%", room, "set", H, pct=50)
    expect("Lights 50 percent.", room, "set", H, pct=50)
    expect("Bedroom lights 30%.", room, "set", rm("bedroom"), pct=30)
    expect("Set the bedroom lights 25%.", room, "set", rm("bedroom"), pct=25)
    expect("dim the lights 20 percent", room, "set", H, pct=20)
    expect("turn on the lights 100%", room, "set", H, pct=100)
    expect("bedroom to 10", room, "set", rm("bedroom"), pct=10)
    expect("Dining room to forty.", room, "set", rm("dining room"), pct=40)
    expect("turn the lights all the way up", room, "set", H, pct=100)
    expect("Make the lights as bright as possible.", room, "set", H, pct=100)
    expect("The lights are too dim.", room, "brighter", H)
    expect("It's too dim in here.", room, "brighter", H)
    expect("lights down", room, "dimmer", H)
    expect("brightness up", room, "brighter", H)
    expect("Brightness down.", room, "dimmer", H)
    expect("more light", room, "brighter", H)

# ---- timing ----
for room in SATS:
    for text in ("turn on the lights at 7", "turn on the lights at 10 tonight", "turn off the lights in five",
                 "Turn off the lights in ten.", "Shut the lights off in 15.", "turn off the lights at midnight",
                 "Turn the lights on at noon.", "turn off the lights after 20 minutes",
                 "Turn off the bedroom lights after an hour.", "Turn off the lights at 11.", "Lights out at 10.",
                 "Turn off the lights at ten pm.", "turn off the lights 10 minutes from now"):
        expect(text, room, "later")

# ---- phrasings that must work ----
for room in SATS:
    for text, kind, w in (("Cut on the lights.", "on", H), ("cut on the bedroom light", "on", rm("bedroom")),
                          ("Cut the lights.", "off", H), ("I want the lights off.", "off", H),
                          ("Alright, lights off.", "off", H), ("Okay cool, lights off.", "off", H),
                          ("Good night, lights off.", "off", H), ("I'm home! Lights on!", "on", H),
                          ("You're the best. Lights off please.", "off", H), ("Make sure the lights are off.", "off", H),
                          ("I need the lights on.", "on", H), ("turn off da lights", "off", H),
                          ("turn off dem lights", "off", H), ("pop the lights on", "on", H),
                          ("Turn off old lights.", "off", ALL), ("Are you able to turn off the lights?", "off", H),
                          ("Did you hear me? Turn off the lights.", "off", H),
                          ("We're going to bed, turn off the lights.", "off", H),
                          ("We're leaving, turn everything off.", "off", ALL), ("Can I have the lights on?", "on", H),
                          ("Could I get the lights off please?", "off", H), ("Why don't you turn off the lights?", "off", H),
                          ("Who left the lights on? Turn them off.", "off", H),
                          ("If you don't mind, turn off the lights.", "off", H), ("nix the lights", "off", H),
                          ("Nix the bedroom lights.", "off", rm("bedroom")), ("power down the lights", "off", H),
                          ("Power down the living room lights.", "off", rm("living room")),
                          ("fire up the lights", "on", H), ("Fire up the living room lights.", "on", rm("living room")),
                          ("open the lights", "on", H), ("close the lights", "off", H), ("Turn off delights.", "off", H),
                          ("Turn on delights.", "on", H), ("out the lights", "off", H),
                          ("make it dark in here", "off", H), ("Make the bedroom dark.", "off", rm("bedroom")),
                          ("it's dark in here", "on", H), ("Don't leave the lights on.", "off", H),
                          ("Don't keep the lights on.", "off", H), ("Turn on living room.", "on", rm("living room")),
                          ("Turn everything off.", "off", ALL), ("Everything off.", "off", ALL)):
        expect(text, room, kind, w)
    expect("lights off then the porch on", room, "off", H)
    expect("turn everything off including the music", room, "off", ALL)
    expect("Turn off the lights and the porch light.", room, "off", H)
    expect("Turn off the ring room lights", room, "ask")
    expect("Lights on or off?", room, "status")
    expect("Bedroom lights on or off?", room, "status")
    expect("Turn on the lights in the garage.", room, "nolights")
    expect("Turn on the garage lights.", room, "nolights")
    expect("make the lights warmer", room, "color", H)
    expect("Make the lights cooler.", room, "color", H)
    # 10-16 every-way run: no light word, but a room + put/get + on/off/out; "to max" / "to N" without a verb
    expect("put the bedroom on", room, "on", rm("bedroom"))
    expect("Put out the living room.", room, "off", rm("living room"))
    expect("Um, get the dining off, please.", room, "off", rm("dining room"))
    expect("Get everything off.", room, "off", ALL)
    expect("Put the whole house on.", room, "on", ALL)
    expect("turn on the lights in this room", room, "on", H)
    expect("Shut off the lights in this room.", room, "off", H)
    expect("lights to max", room, "set", H, pct=100)
    expect("Lights to 70 percent.", room, "set", H, pct=70)
    expect("bedroom light two to 40 percent", room, "set", rm("bedroom 2"), pct=40)
    expect("Set the living room to green.", room, "color", rm("living room"))
    expect("Can you tell me which lights are on?", room, "status")

# ---- "them / it" = the lights just talked about ----
for room in ("kitchen", "bedroom"):
    for first, then, kind, w in (("turn on the living room lights", "turn them off", "off", rm("living room")),
                                 ("turn on the living room lights", "turn it off", "off", rm("living room")),
                                 ("turn off the dining room lights", "turn them back on", "on", rm("dining room")),
                                 ("turn on the dining room lights", "make them blue", "color", rm("dining room"))):
        reset(on=False)
        say(first, room)
        if kind == "off":
            for ip in w:
                state[ip]["state"] = True
        sent.clear()
        res = say(then, room)
        got = changed()
        ok = got == w and (kind == "color" or all(state[i]["state"] == (kind == "on") for i in w))
        check(f"{first} -> {then}", room, ok, f"said {res.get('say')!r} changed {sorted(IP[i] for i in got)}")
    reset(on=True)                                  # within one sentence
    res = say("Turn on the bedroom lights and make them blue.", "kitchen")
    check("Turn on the bedroom lights and make them blue.", "kitchen", changed() == rm("bedroom"),
          f"changed {sorted(IP[i] for i in changed())} {res.get('say')!r}")
# with no light command before: "turn it off" is not a light command (could be the TV / music)
r.lights._recent = {}
expect("turn it off", "bedroom", "none")
expect("turn them off", "kitchen", "none")
# ... but "make it darker" / "make it blue" can only be the lights: this room's
for room in SATS:
    r.lights._recent = {}
    expect("make it darker", room, "dimmer", H)
    r.lights._recent = {}
    expect("Make it blue.", room, "color", H)
r.lights._recent = {}

# ================= the 10-16 adversarial review (C5-C12): each one switched the WRONG lights =================
for room in SATS:
    # C5 "but only / but just" = ONLY that room (was read as "except")
    expect("turn off the lights but only the bedroom", room, "off", rm("bedroom"))
    expect("Turn on the lights, but just the living room.", room, "on", rm("living room"))
    # C6 "... but leave / keep X on", "... X stays on" = X kept out
    expect("Turn off all the lights but leave the bedroom on.", room, "off", ALL - rm("bedroom"))
    expect("turn off everything but keep the bedroom on", room, "off", ALL - rm("bedroom"))
    expect("Turn off all the lights but the dining room stays on.", room, "off", ALL - rm("dining room"))
    expect("turn on all the lights but leave the bedroom off", room, "on", ALL - rm("bedroom"))
    expect("Turn off all the lights but leave bedroom light one on.", room, "off", ALL - rm("bedroom 1"))
    # C10 everyday "get / bring / kick ... it all / them all / everything ... out" is not the lights
    for text in ("Just get it all out.", "bring everything out", "kick them all out", "throw it all out",
                 "Get them all out.", "Bring it all out."):
        expect(text, room, "none")
    expect("put everything out", room, "off", ALL)
    # C12 a place she has no lights in is not HERE
    expect("Turn off the lights in the room next door.", room, "nolights")
    expect("turn off the lights upstairs", room, "nolights")
    expect("Turn on the light in the room across the hall.", room, "nolights")
# C7 "... but not in here / not this room" from the bedroom = everything else
expect("turn off all the lights but not in here", "bedroom", "off", ALL - rm("bedroom"))
expect("Turn off all the lights, not this room.", "bedroom", "off", ALL - rm("bedroom"))
expect("turn off the living room and bedroom lights but not in here", "bedroom", "off", rm("living room"))
# C12 "this room AND the living room" keeps this room
expect("turn on the lights in this room and the living room", "bedroom", "on", rm("bedroom", "living room"))
# C8 "make it warmer / cooler" is the heat, unless a light command was just said
for room in SATS:
    for text in ("Make it warmer in here.", "It's freezing, make it warmer.", "Can you make it cooler?",
                 "make it colder in here"):
        r.lights._recent = {}
        expect(text, room, "none", start_on=False)
for room in ("kitchen", "bedroom"):
    r.lights._recent = {}
    reset(on=False)
    say("turn on the bedroom lights", room)
    sent.clear()
    res = say("make it warmer", room)
    check("turn on the bedroom lights -> make it warmer", room, changed() == rm("bedroom"),
          f"said {res.get('say')!r} changed {sorted(IP[i] for i in changed())}")
r.lights._recent = {}
# C9 dimming never turns a light ON; C11 a trailing qualifier belongs to the room just named
for room, text, on_rooms, want in (("kitchen", "It's too bright in here.", ["living room"], rm("living room")),
                                   ("kitchen", "Make it darker.", ["living room"], rm("living room")),
                                   ("bedroom", "Make it darker.", [], set()),
                                   ("bedroom", "dim the lights", [], set()),
                                   ("kitchen", "turn on the bedroom lights, not too bright", [], rm("bedroom")),
                                   ("kitchen", "turn on the bedroom lights but dimmer", [], rm("bedroom"))):
    r.lights._recent = {}
    reset(on=False, dimming=100)
    for rn in on_rooms:
        for ip in rm(rn):
            state[ip]["state"] = True
    res = say(text, room)
    lit = {ip for ip in ALL if state[ip]["state"]}
    check(f"{text} (on: {on_rooms or 'none'})", room, changed() == want and lit == (want | {ip for rn in on_rooms for ip in rm(rn)}),
          f"said {res.get('say')!r} changed {sorted(IP[i] for i in changed())} lit {sorted(IP[i] for i in lit)}")
reset(on=False)
say("dim the bedroom lights", "kitchen")
check("dim the bedroom lights while they're off = a 30% night light (named room)", "kitchen",
      changed() == rm("bedroom") and all(state[i]["state"] and state[i]["dimming"] == 30 for i in rm("bedroom")),
      f"changed {sorted(IP[i] for i in changed())}")
r.lights._recent = {}

# ================= round 2 (the fixes' own review, R1-R26): end state of every bulb =================
def run_case(room, text, start, want_on, want_dim=None, label=""):
    """start = {bulb ip: (on, dimming)} (others off at 100); want_on = the set of bulbs ON afterwards."""
    r.lights._recent = {}
    reset(on=False, dimming=100)
    for ip, (on, d) in start.items():
        state[ip] = {"state": on, "dimming": d}
    res = say(text, room)
    lit = {ip for ip in ALL if state[ip]["state"]}
    ok = lit == want_on and all(state[ip]["dimming"] == v for ip, v in (want_dim or {}).items())
    check(label or text, room, ok, f"said {res.get('say')!r} lit {sorted(IP[i] for i in lit)} "
                                   f"dims { {IP[i]: state[i]['dimming'] for i in sorted(ALL)} }")
    return res


ON60 = {ip: (True, 60) for ip in ALL}
# R1 / R15: a dim / set / colour after an OFF is HERE - never the lights just switched off
run_case("bedroom", "turn off the living room and dim the lights", ON60, rm("bedroom", "dining room"),
         {ip: 35 for ip in rm("bedroom")})
run_case("bedroom", "turn off everything except the bedroom, then dim the lights", ON60, rm("bedroom"),
         {ip: 35 for ip in rm("bedroom")})
run_case("living room", "Turn off the bedroom lights, then make the lights blue.", ON60, rm("living room", "dining room"))
run_case("kitchen", "turn off the bedroom lights and set the lights to 50 percent", ON60, ALL - rm("bedroom"),
         {ip: 50 for ip in ALL - rm("bedroom")})
run_case("bedroom", "turn off the bedroom lights and make it warmer", ON60, ALL - rm("bedroom"))
# R18: "it" = lights just switched off: "make it darker" / "make it warmer" never relights them
for room in ("bedroom", "kitchen"):
    r.lights._recent = {}
    reset(on=True, dimming=60)
    say("turn off the bedroom lights", room)
    sent.clear()
    say("make it darker", room)
    check("turn off the bedroom lights -> make it darker: they stay off", room,
          not any(state[i]["state"] for i in rm("bedroom")), f"changed {sorted(IP[i] for i in changed())}")
    reset(on=True, dimming=60)
    say("turn off the bedroom lights", room)
    sent.clear()
    say("it's freezing, make it warmer", room)
    check("turn off the bedroom lights -> make it warmer: they stay off", room,
          not any(state[i]["state"] for i in rm("bedroom")), f"changed {sorted(IP[i] for i in changed())}")
# R2: a TV / fan / heater named in the "leave ... on" / "but not" clause is not the room's lights
for text in ("turn off all the lights but leave the bedroom tv on", "turn off all the lights and leave the bedroom fan on",
             "turn off all the lights, the bedroom tv stays on", "turn off all the lights but not the bedroom fan"):
    run_case("kitchen", text, ON60, set())
# R3: "but also / too / as well / in" add, "but X only" limits
run_case("kitchen", "turn off the lights but the bedroom only", ON60, ALL - rm("bedroom"))
run_case("kitchen", "turn off the living room lights but also the bedroom", ON60, rm("dining room"))
run_case("kitchen", "turn off the living room lights, but the bedroom ones too", ON60, rm("dining room"))
run_case("kitchen", "turn on the living room lights but the dining room as well", {}, rm("living room", "dining room"))
# R4: a far place in a remark isn't the target
run_case("bedroom", "turn off the lights, I'm going outside", ON60, ALL - rm("bedroom"))
run_case("bedroom", "turn on the lights its dark outside", {}, rm("bedroom"))
# R5: "turn the lights on dim" in the dark = on at a night-light level; "but keep them dim"
run_case("bedroom", "turn on the lights dim", {}, rm("bedroom"), {ip: 30 for ip in rm("bedroom")})
run_case("bedroom", "turn on the lights but keep them dim", {}, rm("bedroom"), {ip: 30 for ip in rm("bedroom")})
# R6 / R8: "but not in here" / "except the ones in here" = every OTHER light
for text in ("turn off the lights but not in here", "turn off the lights, but not this room",
             "turn off all the lights but the ones in here", "turn off all the lights except the ones in here",
             "turn off everything except in this room", "turn off all the lights except for in here",
             "turn off all the lights except for this room", "turn off all the lights, except the ones in here",
             "turn off all the lights except the lights in here"):
    run_case("bedroom", text, ON60, rm("bedroom"))
# R7: an exclusion on "them" survives filling "them" in
for text in ("turn them off but not the bedroom", "turn them off but leave the bedroom on", "now turn them off, except the bedroom"):
    r.lights._recent = {}
    reset(on=False)
    say("turn on all the lights", "kitchen")
    sent.clear()
    say(text, "kitchen")
    lit = {ip for ip in ALL if state[ip]["state"]}
    check(f"turn on all the lights -> {text}", "kitchen", lit == rm("bedroom"), f"lit {sorted(IP[i] for i in lit)}")
# R9: other ways to say "leave X on"
for text in ("turn off all the lights but leave on the bedroom light", "turn off all the lights but leave the bedroom light on for now",
             "turn off all the lights but leave the bedroom light on tonight", "turn off every light but keep the bedroom lit"):
    run_case("kitchen", text, ON60, rm("bedroom"))
run_case("bedroom", "turn off all the lights but leave the light on in here", ON60, rm("bedroom"))
run_case("bedroom", "turn off all the lights but leave the ones in here on", ON60, rm("bedroom"))
# R10: "but not X" after an inherited clause edits only the last command
run_case("kitchen", "turn on the bedroom and living room lights and make them blue, but not the living room", {},
         rm("bedroom", "living room"))
# R11 / R12
run_case("bedroom", "turn the lights in the room on", {}, rm("bedroom"))
run_case("living room", "turn the lights in this room on and the bedroom", {}, rm("living room", "bedroom"))
run_case("bedroom", "turn on the light in the room beside the living room", {}, set())
# R13: "turn the X and Y lights on" with X already on -> both on (it was a toggle of X)
run_case("kitchen", "turn the living room and dining room lights on", {ip: (True, 100) for ip in rm("living room")},
         rm("living room", "dining room"))
run_case("kitchen", "Turn the bedroom and the living room on.", {ip: (True, 100) for ip in rm("bedroom")},
         rm("bedroom", "living room"))
run_case("kitchen", "turn the bedroom and the living room lights off",
         {ip: (True, 100) for ip in rm("living room")}, set())
# R14 / R17: "warm white" / "cool white" ARE colours; heat talk with a room is not the lights
run_case("bedroom", "make everything warm white", {}, ALL)
run_case("bedroom", "make it warm white in here", {}, rm("bedroom"))
for text in ("make the bedroom warmer", "bedroom warmer", "cool the bedroom down", "cool down the living room",
             "make it warmer in the bedroom", "warm up the bedroom", "make the living room cooler"):
    run_case("kitchen", text, ON60, ALL, label=f"{text} (heat, lights untouched)")
# R16: flick / throw with no light word
run_case("kitchen", "flick the bedroom on", {}, rm("bedroom"))
run_case("kitchen", "flick off the dining room", ON60, ALL - rm("dining room"))
run_case("kitchen", "throw on the living room", {}, rm("living room"))
run_case("kitchen", "flick everything off", ON60, set())
# R19: brighter with one bulb at full and the other off lights the other one
run_case("bedroom", "brighter", {BULB["bedroom 1"]: (True, 100)}, rm("bedroom"))
# R21: "dim everything"
run_case("kitchen", "dim everything", ON60, ALL, {ip: 35 for ip in ALL})
# R22: "crank / light" never turn lights OFF
run_case("bedroom", "crank the lights", {ip: (True, 100) for ip in rm("bedroom")}, rm("bedroom"))
run_case("kitchen", "light the bedroom", {ip: (True, 100) for ip in rm("bedroom")}, rm("bedroom"))
# ================= round 3 (T1-T31) =================
B60 = {ip: (True, 60) for ip in rm("bedroom")}
# T1: "turn them back on" / "make them blue" after an off = the SAME lights
run_case("kitchen", "Turn off the bedroom lights, then turn them back on.", B60, rm("bedroom"))
run_case("bedroom", "turn off the bedroom light and turn it back on", B60, rm("bedroom"))
run_case("kitchen", "turn off the bedroom lights and make them blue", B60, rm("bedroom"))
# T2/T16/T19/T22: the "X and Y" merge only when the first half is just verb + room
run_case("kitchen", "turn the bedroom lights blue and the living room off", ON60, rm("bedroom", "dining room"))
run_case("kitchen", "turn the bedroom lights to 20 and the living room off", ON60, rm("bedroom", "dining room"),
         {ip: 20 for ip in rm("bedroom")})
run_case("kitchen", "Turn the bedroom lights to 30% and the dining room lights off.", ON60, rm("bedroom", "living room"),
         {ip: 30 for ip in rm("bedroom")})
run_case("kitchen", "turn the bedroom and living room lights to 50 percent", ON60, ALL,
         {ip: 50 for ip in rm("bedroom", "living room")})
run_case("kitchen", "turn the bedroom and the living room blue", ON60, ALL)
# T3/T21: "in the dining room by 10 percent" / "living room by the couch" are the room itself
run_case("kitchen", "brighten the lights in the dining room by 10 percent", ON60, ALL, {ip: 70 for ip in rm("dining room")})
run_case("kitchen", "dim the lights in the living room by 20 percent", ON60, ALL, {ip: 40 for ip in rm("living room")})
run_case("kitchen", "turn on the lamp in the living room by the couch", {}, rm("living room"))
# T4/T23: excluding your OWN room by name
for text in ("turn off the lights but leave the bedroom on", "turn off the lights but not the bedroom",
             "Turn off the lights, but not the bedroom.", "turn off the lights, except the bedroom"):
    run_case("bedroom", text, ON60, rm("bedroom"))
# T5: more ways to keep a room on
run_case("kitchen", "turn off all the lights but leave the light on in the bedroom", ON60, rm("bedroom"))
run_case("bedroom", "turn off all the lights but keep the lights on in the living room", ON60, rm("living room"))
run_case("kitchen", "Turn off all the lights, but the bedroom's staying on.", ON60, rm("bedroom"))
run_case("kitchen", "turn off the lights but leave the living room on for the dog", ON60, rm("living room"))
# T6: "the tv room" IS the living room
run_case("kitchen", "turn off all the lights but leave the tv room on", ON60, rm("living room"))
run_case("kitchen", "Turn off all the lights, but not the tv room.", ON60, rm("living room"))
# T7 / T9: whisper's comma before "too" and sentence break before an afterthought
run_case("kitchen", "Turn off the bedroom lights, but the living room, too.", ON60, rm("dining room"))
run_case("kitchen", "Turn off the bedroom lights. And the living room.", B60, set())
run_case("kitchen", "Turn off the bedroom lights. The living room too.", B60, set())
# T8: off and back on
run_case("kitchen", "turn the bedroom lights off and on", B60, rm("bedroom"))
run_case("kitchen", "turn the bedroom lights off and back on again", B60, rm("bedroom"))
# T10/T28: timed, no light word
for room in SATS:
    for text in ("turn off the bedroom in 10 minutes", "turn everything off in 10 minutes", "turn the bedroom on for 10 minutes"):
        expect(text, room, "later") if "for 10" not in text else None
r.lights._recent = {}
reset(on=True)
res = say("turn off the bedroom lights and turn them back on in 10 minutes", "kitchen")
check("off now + 'turn them back on in 10 minutes' scheduled", "kitchen",
      not any(state[i]["state"] for i in rm("bedroom")) and " in 10 minutes" in res.get("say", ""), res.get("say"))
for t_ in list(r.lights._timers):
    t_.cancel()
r.lights._timers.clear()
# T11: "set the bedroom to 50" (no light word), also inside a sentence
run_case("kitchen", "set the bedroom to 50", ON60, ALL, {ip: 50 for ip in rm("bedroom")})
run_case("kitchen", "turn off the bedroom and set the living room to 50", ON60, rm("living room", "dining room"),
         {ip: 50 for ip in rm("living room")})
# T12: "the rest" / "everything else"
run_case("kitchen", "turn on the bedroom lights and turn off the rest", {ip: (True, 60) for ip in rm("living room", "dining room")},
         rm("bedroom"))
run_case("kitchen", "turn on the bedroom and turn everything else off", ON60, rm("bedroom"))
run_case("kitchen", "turn on the bedroom lights and turn off the other lights", ON60, rm("bedroom"))
# T14: "all the lights upstairs" is not the whole house
run_case("bedroom", "turn off all the lights upstairs", ON60, ALL)
run_case("bedroom", "turn off all the lights, I'm going outside", ON60, set())
# T15/T31: "cool" as a filler vs a colour
run_case("bedroom", "okay cool the lights off", B60, set())
reset(on=False)
say("cool white in the bedroom", "kitchen")
check("cool white in the bedroom = the cool-white temperature", "kitchen",
      all(state[i].get("temp") == 6000 for i in rm("bedroom")), {IP[i]: state[i] for i in rm("bedroom")})
# T18: an exception inside a "them" clause
for text in ("turn them off except the bedroom", "turn them off besides the bedroom", "turn them off except for the bedroom"):
    r.lights._recent = {}
    reset(on=False)
    say("turn on all the lights", "kitchen")
    sent.clear()
    say(text, "kitchen")
    lit = {ip for ip in ALL if state[ip]["state"]}
    check(f"turn on all the lights -> {text}", "kitchen", lit == rm("bedroom"), f"lit {sorted(IP[i] for i in lit)}")
# T20: a whole room plus another room's numbered bulb
run_case("kitchen", "turn off the living room lights and bedroom light two", ON60, rm("dining room", "bedroom 1"))
run_case("kitchen", "Turn off the living room lights and the bedroom light too.", ON60, rm("dining room"))
run_case("kitchen", "turn off the living room and bedroom 2", ON60, rm("dining room", "bedroom 1"))
# T24: "except the lights in the bedroom"
run_case("kitchen", "turn off all the lights except the lights in the bedroom", ON60, rm("bedroom"))
run_case("kitchen", "turn off everything except the lamp in the dining room", ON60, rm("dining room"))
# T25: "turn everything up / down" is not the lights (volume); "dim everything" is
for text in ("turn everything up", "turn everything down", "turn up everything"):
    run_case("kitchen", text, {}, set(), label=f"{text} (not the lights)")
# T26: "turn on the lights dim" with one lamp already on
run_case("bedroom", "turn on the lights dim", {BULB["bedroom 1"]: (True, 100)}, rm("bedroom"), {ip: 30 for ip in rm("bedroom")})
# T27: "... down by 20 percent" dims
run_case("bedroom", "turn the lights in the bedroom down by 20 percent", ON60, ALL, {ip: 40 for ip in rm("bedroom")})
# T29: a reason clause with a warm/cool word is not a colour
run_case("bedroom", "turn off the lights because it's warm in here", B60, set())
run_case("bedroom", "turn off the lights so it's cooler", B60, set())
# T30: "make it brighter" with one lamp at full lights the other
run_case("bedroom", "make it brighter", {BULB["bedroom 1"]: (True, 100)}, rm("bedroom"))
# T13: an absolute time is confirmed as hours/minutes, at the right minute
r.lights._recent = {}
res = say("turn on the lights at 7", "kitchen")
check("'at 7' confirmed without raw seconds, at :00", "kitchen",
      "second" not in res.get("say", "") and ":00" in res.get("say", ""), res.get("say"))
for t_ in list(r.lights._timers):
    t_.cancel()
r.lights._timers.clear()
# ================= round 4 (Q1-Q12) =================
# Q1: two levels in one sentence
run_case("kitchen", "set the bedroom to 50 and the living room to 20", ON60, ALL,
         {**{ip: 50 for ip in rm("bedroom")}, **{ip: 20 for ip in rm("living room")}})
run_case("kitchen", "set the bedroom to 50, the living room to 20", ON60, ALL,
         {**{ip: 50 for ip in rm("bedroom")}, **{ip: 20 for ip in rm("living room")}})
run_case("kitchen", "set the bedroom to 50 and the rest to 20", ON60, ALL,
         {**{ip: 50 for ip in rm("bedroom")}, **{ip: 20 for ip in rm("living room", "dining room")}})
# Q2: a leading "leave X on" keeps X out of the command that follows - from any room
for room in ("kitchen", "living room", "dining room"):
    run_case(room, "leave the bedroom on and turn everything else off", ON60, rm("bedroom"))
    run_case(room, "keep the bedroom on and turn off the rest", ON60, rm("bedroom"))
run_case("kitchen", "leave the bedroom on, turn off the rest", ON60, rm("bedroom"))
# Q3: the merge with a time / qualifier after it
r.lights._recent = {}
reset(on=False)
for ip in rm("living room"):
    state[ip]["state"] = True
res = say("turn the bedroom and the living room off in 10 minutes", "kitchen")
check("turn the bedroom and the living room off in 10 minutes -> nothing now, both later", "kitchen",
      not changed() and " in 10 minutes" in res.get("say", ""), f"{res.get('say')!r} changed {sorted(IP[i] for i in changed())}")
for t_ in list(r.lights._timers):
    t_.cancel()
r.lights._timers.clear()
# Q4: "X and the rest / everything else" = all of it
run_case("bedroom", "turn off the lights in here and the rest of the house", ON60, set())
run_case("bedroom", "turn off the bedroom and everything else", ON60, set())
run_case("living room", "turn off the living room and all the other lights", ON60, set())
# Q5: "... off and back on in 5 minutes" schedules the relight
r.lights._recent = {}
reset(on=True)
res = say("turn the bedroom off and back on in 5 minutes", "kitchen")
check("turn the bedroom off and back on in 5 minutes -> off now, on scheduled", "kitchen",
      not any(state[i]["state"] for i in rm("bedroom")) and " in 5 minutes" in res.get("say", ""), res.get("say"))
for t_ in list(r.lights._timers):
    t_.cancel()
r.lights._timers.clear()
# Q6: "turn off the lights and turn it up" = the volume, never relights
run_case("bedroom", "turn off the lights and turn it up", ON60, ALL - rm("bedroom"))
# Q7: "dim the lights that are on" only dims those
two_on = {BULB["bedroom 1"]: (True, 100), BULB["living room 1"]: (True, 100)}
for text in ("dim the lights that are on", "dim the lights while the movie is on", "turn down the lights that are on"):
    run_case("kitchen", text, two_on, {BULB["bedroom 1"], BULB["living room 1"]})
run_case("bedroom", "dim the lights that are on", {BULB["bedroom 1"]: (True, 100)}, {BULB["bedroom 1"]})
run_case("bedroom", "turn the lights on dim", {}, rm("bedroom"), {ip: 30 for ip in rm("bedroom")})
# Q8: "except the lights" + a time / tonight / for now = not the lights
for text in ("turn off everything except the lights tonight", "turn off everything except the lights for now",
             "turn off everything except the lights in 10 minutes", "shut everything off except for the lights for a bit"):
    run_case("kitchen", text, ON60, ALL, label=f"{text} (lights untouched)")
# Q9: "except light one" = that bulb is LEFT ON
run_case("kitchen", "turn off the bedroom lights except light one", ON60, ALL - rm("bedroom 2"))
run_case("kitchen", "turn off the bedroom lights except for light two", ON60, ALL - rm("bedroom 1"))
run_case("kitchen", "set the dining room lights to 50 except light two", ON60, ALL, {BULB["dining room 1"]: 50,
                                                                                    BULB["dining room 2"]: 60})
run_case("bedroom", "turn off the lights except light two", ON60, ALL - rm("bedroom 1"))
run_case("kitchen", "turn off the bedroom lights except bedroom light one", ON60, ALL - rm("bedroom 2"))
# Q10: "turn everything off but leave the lights on" is not about the lights; "but leave the bedroom on" from
# another room = the whole house minus the bedroom
run_case("living room", "turn everything off but leave the lights on", ON60, ALL, label="leave the lights on (untouched)")
run_case("living room", "turn off the lights but leave the bedroom on", ON60, rm("bedroom"))
run_case("living room", "turn off the lights but not the bedroom", ON60, rm("bedroom"))
# Q11: "the other lamp / one" = the room's other bulb
run_case("kitchen", "turn off bedroom light one and turn on the other light", {BULB["bedroom 1"]: (True, 60)}, rm("bedroom 2"))
run_case("kitchen", "turn on bedroom light one and turn off the other one", {BULB["bedroom 2"]: (True, 60)}, rm("bedroom 1"))
r.lights._recent = {}
reset(on=False)
for ip in rm("bedroom 1"):
    state[ip]["state"] = True
res = say("turn on the other lamp", "bedroom")
check("'turn on the other lamp' with nothing named: asks which, switches nothing", "bedroom",
      not changed() and "bedroom 2" in res.get("say", ""), res.get("say"))
# Q12: "in about 10 minutes" / "in 10 more minutes" / "in an hour and a half" are later, not now
for text in ("turn off the lights in about 10 minutes", "turn off the lights in 10 more minutes",
             "turn the lights off in another 10 minutes", "turn off the lights in like 10 minutes",
             "turn off the lights in 10 to 15 minutes", "turn off the lights in an hour and a half"):
    expect(text, "bedroom", "later")
r.lights._recent = {}
reset(on=False)
say("turn on all the lights", "kitchen")
sent.clear()
res = say("turn them off except the bedroom in 10 minutes", "kitchen")
check("turn them off except the bedroom in 10 minutes -> later, not now", "kitchen",
      not changed() and " in 10 minutes" in res.get("say", ""), res.get("say"))
for t_ in list(r.lights._timers):
    t_.cancel()
r.lights._timers.clear()
# ================= round 5 (P1-P18) =================
# P1: one level shared by two rooms
for text in ("dim the bedroom and the living room to 20", "dim the bedroom and living room lights to 20",
             "turn down the bedroom and living room to 20"):
    run_case("kitchen", text, ON60, ALL, {ip: 20 for ip in rm("bedroom", "living room")})
run_case("kitchen", "turn on the bedroom and living room lights to 50", {}, rm("bedroom", "living room"),
         {ip: 50 for ip in rm("bedroom", "living room")})
run_case("kitchen", "turn off the bedroom and the living room to 20", ON60, rm("living room", "dining room"),
         {ip: 20 for ip in rm("living room")})
# P2: a leading "keep X on" followed by a non-opposite command binds them / the rest to X
run_case("kitchen", "keep the bedroom lights on and dim them", ON60, ALL, {**{ip: 35 for ip in rm("bedroom")},
                                                                          **{ip: 60 for ip in rm("living room", "dining room")}})
for room in ("kitchen", "living room"):
    run_case(room, "keep the bedroom lights on and set the rest to 20", ON60, ALL,
             {**{ip: 60 for ip in rm("bedroom")}, **{ip: 20 for ip in rm("living room", "dining room")}})
run_case("kitchen", "leave the bedroom light on and set it to 20", ON60, ALL, {ip: 20 for ip in rm("bedroom")})
run_case("kitchen", "keep the bedroom on and make it blue", ON60, ALL)
# P3: "but not light two" / "but leave light one on" = that bulb kept
run_case("bedroom", "turn off the lights but not light two", {BULB["bedroom 1"]: (True, 60)}, set())
run_case("bedroom", "turn off the lights but not light two", ON60, ALL - rm("bedroom 1"))
run_case("living room", "turn off the bedroom lights, but not light one", ON60, ALL - rm("bedroom 2"))
run_case("kitchen", "turn off the bedroom lights but leave light one on", ON60, ALL - rm("bedroom 2"))
run_case("kitchen", "turn off the bedroom lights but keep number two on", ON60, ALL - rm("bedroom 1"))
# P4 / P14 / P17: times
for room, text in (("bedroom", "turn off the lights at around 10"), ("bedroom", "turn off the lights at about 11 pm"),
                   ("bedroom", "In 20 minutes, turn off the lights."), ("kitchen", "In ten minutes, turn the lights off."),
                   ("bedroom", "At 10 pm, turn off the lights."), ("kitchen", "Okay, in 15 minutes, turn the living room lights off."),
                   ("bedroom", "Turn off the lights. In 10 minutes.")):
    expect(text, room, "later")
expect("At 7, turn on the lights.", "bedroom", "later", start_on=False)
run_case("bedroom", "Lights at 10.", ON60, rm("bedroom") | (ALL - rm("bedroom")), {ip: 10 for ip in rm("bedroom")})
run_case("kitchen", "Set the living room lights at 20.", ON60, ALL, {ip: 20 for ip in rm("living room")})
run_case("kitchen", "Bedroom lights at 20.", ON60, ALL, {ip: 20 for ip in rm("bedroom")})
# P5: an explicit "in here" is never widened to the house
for text in ("turn off the lights in here but leave the living room on", "turn off the lights in this room but not the living room"):
    run_case("bedroom", text, ON60, ALL - rm("bedroom"))
# P6: "the lights and everything else" = all
for text in ("turn off the lights and everything else", "turn off the lights and all the rest", "turn off this light and everything else"):
    run_case("bedroom", text, ON60, set())
# P13: "turn the brightness down on the lights" dims only what's on
run_case("kitchen", "Turn the brightness down on the lights.", two_on, {BULB["bedroom 1"], BULB["living room 1"]})
run_case("bedroom", "Turn down the lamp on the nightstand.", {BULB["bedroom 1"]: (True, 100)}, {BULB["bedroom 1"]})
run_case("kitchen", "Turn down the brightness on the living room lights.", {BULB["living room 1"]: (True, 100)}, {BULB["living room 1"]})
# P15: somebody's room she has no bulbs for
for text in ("Turn off the light in my son's room.", "Turn off the light in my daughter's room.",
             "Turn off the light in the master bathroom.", "Turn off the lights in the little ones' room."):
    run_case("kitchen", text, ON60, ALL, label=f"{text} (no lights there)")
run_case("kitchen", "Turn on the light in mommy and daddy's room.", {}, rm("bedroom"),
         label="mommy and daddy's room = the (parents') bedroom")   # (round 6: the house's one bedroom)
run_case("kitchen", "Turn on the light in my brother's room.", {}, set(), label="my brother's room (no lights)")
# P16: a room said after a full stop goes WITH the command
run_case("kitchen", "Turn off the lights. In the bedroom.", ON60, rm("living room", "dining room"))
run_case("kitchen", "Turn on the lights. Dining room, please.", {}, rm("dining room"))
run_case("bedroom", "Turn off the lights. Living room.", ON60, rm("bedroom", "dining room"))
run_case("bedroom", "Turn on the lights. All of them.", {}, ALL)
# P18: "change the lights back to normal" (no alert) = their normal colour, music untouched
r.lights._recent = {}
reset(on=True)
for ip in ALL:
    state[ip].update({"r": 128, "g": 0, "b": 128})
res = say("Change the lights back to normal.", "bedroom")
check("change the lights back to normal (no alert) -> warm white, no 'music's off'", "bedroom",
      "music" not in res.get("say", "").lower() and all(state[i].get("temp") == 2700 for i in rm("bedroom")), res.get("say"))
# ================= round 6 (S1-S12) =================
# S1: a remark after the command that names a room in passing is NOT a target
run_case("living room", "Turn off the lights. I'm going to the bedroom.", ON60, ALL - rm("living room"))
run_case("kitchen", "Turn off the living room lights. I'm in the bedroom.", ON60, ALL - rm("living room"))
run_case("kitchen", "Turn off the lights. I'm leaving the kitchen.", ON60, set())
# S2: a time between two commands belongs to the one AFTER it
r.lights._recent = {}
reset(on=False)
res = say("Turn on the bedroom lights. In 10 minutes, turn them off.", "kitchen")
check("on now, off in 10 minutes (not the other way round)", "kitchen",
      all(state[i]["state"] for i in rm("bedroom")) and " in 10 minutes" in res.get("say", ""), res.get("say"))
for t_ in list(r.lights._timers):
    t_.cancel()
r.lights._timers.clear()
# S4: a clock time with a dim / colour / level verb stays a time
for text in ("dim the lights at 9", "make the lights blue at 7", "set the bedroom lights to 30 percent at 10"):
    expect(text, "bedroom", "later")
# S5: colour / on first half + "the living room to 20" = two commands
run_case("kitchen", "turn the bedroom lights blue and the living room to 20", ON60, ALL, {ip: 20 for ip in rm("living room")})
run_case("kitchen", "turn the bedroom lights on and the living room to 20", {ip: (True, 60) for ip in rm("living room")},
         rm("bedroom", "living room"), {ip: 20 for ip in rm("living room")})
# S7: a light named by what it's near
run_case("kitchen", "turn off the lamp by the couch", ON60, ALL - rm("living room"))
run_case("kitchen", "turn off the lamp next to the bed", ON60, ALL - rm("bedroom"))
run_case("kitchen", "turn on the light over the sink", {}, set(), label="over the sink (no lights there)")
run_case("kitchen", "turn off the light above the stove", ON60, ALL, label="above the stove (no lights there)")
# S8: a time she can't keep is refused, nothing switched now
for text in ("Turn off the lights when the movie ends.", "turn off the lights once the kids are asleep",
             "Turn on the lights at sunset.", "turn on the lights tomorrow morning", "turn off the lights later"):
    run_case("kitchen", text, ON60, ALL, label=f"{text} (refused, untouched)")
run_case("bedroom", "Turn the lights on when I wake up.", {}, set(), label="when I wake up (refused)")
for text in ("Wait 10 minutes, then turn off the living room lights.", "Give me five minutes and then turn off all the lights."):
    expect(text, "kitchen", "later")
# S9: verbless up / down / half
run_case("kitchen", "living room lights back up", {ip: (True, 20) for ip in rm("living room")}, rm("living room"),
         {ip: 45 for ip in rm("living room")})
run_case("kitchen", "living room down", ON60, ALL, {ip: 35 for ip in rm("living room")})
run_case("bedroom", "lights back down", ON60, ALL, {ip: 35 for ip in rm("bedroom")})
run_case("kitchen", "bedroom lights half", ON60, ALL, {ip: 50 for ip in rm("bedroom")})
run_case("bedroom", "shut the lights down", ON60, ALL - rm("bedroom"))
# S10: "<room> to N" after a full stop is its own level
run_case("kitchen", "Turn off the dining room. Living room to 20.", ON60, rm("living room", "bedroom"),
         {ip: 20 for ip in rm("living room")})
run_case("kitchen", "Living room off. Bedroom to 20.", ON60, rm("bedroom", "dining room"), {ip: 20 for ip in rm("bedroom")})
# S11: "my room" = the bedroom
run_case("bedroom", "turn off every light except my room", ON60, rm("bedroom"))
run_case("bedroom", "um turn on the lights in my room", {}, rm("bedroom"))
# S12: a question-looking start with a command after it
run_case("kitchen", "we're going to bed turn off all the lights", ON60, set())
run_case("bedroom", "do me a favor turn off the lights", ON60, ALL - rm("bedroom"))
run_case("kitchen", "are the living room lights on turn them off", ON60, ALL - rm("living room"))
expect("are the lights on in the bedroom", "kitchen", "status")
# ================= round 7 (U1-U12) =================
# U1: a question about someone else never switches anything
for room, text in (("kitchen", "Did the kids turn the lights on again?"), ("kitchen", "Did Mom turn the lights on?"),
                   ("kitchen", "Does anyone ever turn the lights off in this house?"), ("bedroom", "Did someone turn off the lights?"),
                   ("kitchen", "did somebody turn the bedroom lights on"), ("bedroom", "what was that, did the kids turn on the lights")):
    run_case(room, text, ON60 if "off" in text else {}, ALL if "off" in text else set(), label=f"{text} (a question)")
run_case("kitchen", "we're going to bed turn off all the lights", ON60, set())
run_case("bedroom", "do me a favor turn off the lights", ON60, ALL - rm("bedroom"))
# U2: "the light on the nightstand" is a noun - "turn off" stays OFF
run_case("kitchen", "turn off the light on the nightstand", ON60, ALL - rm("bedroom"))
run_case("kitchen", "kill the light on the table", ON60, ALL - rm("dining room"))
run_case("bedroom", "turn the lights on the baby is awake", {}, rm("bedroom"))
run_case("bedroom", "turn on the lamp by the window", {}, rm("bedroom"))
run_case("kitchen", "turn on the lamp by the window", {}, set(), label="by the window from the kitchen (asked)")
# U3: afterthoughts / politeness / "until" are not deferrals
for text in ("turn on the lights, I'll be back later", "can you turn off the lights when you get a sec",
             "turn the lights on when you get a chance", "leave the lights on until I get home"):
    run_case("bedroom", text, {} if " on" in text else ON60,
             (rm("bedroom") if " on" in text else ALL - rm("bedroom")), label=f"{text} (acts now)")
res = say("turn off the lights at 10 tomorrow", "bedroom")       # (tomorrow is refused: "at 6 tomorrow morning" was
check("'at 10 tomorrow' is refused, nothing now", "bedroom",     #  timed for 6 PM, and a timer that far is lost on a restart)
      not changed() and "only time the lights" in (res.get("say") or ""), res.get("say"))
# U5: a trailing down/up that isn't about the lights
for text in ("turn on the lights so I can sit down", "turn on the lights I'm coming down", "turn the lights on time to get up",
             "turn on the lights so I can wake up"):
    run_case("bedroom", text, {}, rm("bedroom"), {ip: 100 for ip in rm("bedroom")})
# U7: a remark after a comma / "and" / "because" that names a room is not the target
run_case("bedroom", "Turn off the lights, I'm going to the living room.", ON60, ALL - rm("bedroom"))
run_case("kitchen", "Turn off the lights, I'm heading to the bedroom.", ON60, set())
run_case("bedroom", "Turn off the lights because I'm going to the bedroom", ON60, ALL - rm("bedroom"))
run_case("kitchen", "turn off the living room, the dining room and bedroom lights", ON60, set())
# U8: whisper's "light to"
run_case("bedroom", "Turn off bedroom light to.", ON60, ALL - rm("bedroom 2"))
run_case("bedroom", "Turn bedroom light to off.", ON60, ALL - rm("bedroom 2"))
# U9: lamp-type words
run_case("kitchen", "Turn off the bedside lamp.", ON60, ALL - rm("bedroom"))
run_case("kitchen", "Turn off the couch lamp.", ON60, ALL - rm("living room"))
run_case("kitchen", "Turn off the table lamp.", ON60, ALL, label="table lamp from the kitchen (asked)")
run_case("bedroom", "Turn on the night light.", {}, rm("bedroom"), {ip: 30 for ip in rm("bedroom")})
# U12: whisper's room slips in "the lights in the X"
run_case("kitchen", "Turn off the lights in the bad room.", ON60, ALL - rm("bedroom"))
run_case("kitchen", "Turn on the lights in the dying room.", {}, rm("dining room"))
run_case("kitchen", "Turn off the bedroom lies.", ON60, ALL - rm("bedroom"))
# ================= round 7 pre-deploy check (regressions of round 7) =================
# P1: a list item with a time / "completely" / "a little" is still the same command (split: the first room went off
# NOW and the second got a delayed TOGGLE)
for text in ("turn off the living room and the bedroom in 10 minutes", "turn off the living room and bedroom lights in 10 minutes",
             "turn on the living room and the bedroom in an hour"):
    r.lights._recent = {}
    reset(on="off" in text, dimming=60)
    res = say(text, "kitchen")
    s_ = res.get("say") or ""
    check(f"{text} (one delayed command)", "kitchen", not changed() and "switch" not in s_ and " in " in s_,
          f"said {s_!r} changed {sorted(IP[i] for i in changed())}")
    for t_ in list(r.lights._timers):
        t_.cancel()
    r.lights._timers.clear()
run_case("kitchen", "turn off the living room and bedroom lights for 20 minutes", ON60, rm("dining room"))
for t_ in list(r.lights._timers):
    t_.cancel()
r.lights._timers.clear()
expect("dim the living room and dining room a little", "kitchen", "dimmer", rm("living room", "dining room"))
run_case("kitchen", "turn off the bedroom and the living room lights completely", ON60, rm("dining room"))
# P2: "all the way down" / "in here down" / "on low" / "to low" are levels (they fell to a toggle / plain on)
run_case("living room", "put the lights all the way down", ON60, ALL, {ip: 10 for ip in rm("living room")})
run_case("kitchen", "put the lights all the way down", {}, set(), label="all the way down with everything off (nothing lit)")
run_case("living room", "set the lights way down", ON60, ALL, {ip: 10 for ip in rm("living room")})
run_case("living room", "set the lights in here down", ON60, ALL, {ip: 35 for ip in rm("living room")})
run_case("living room", "the lights in here up", ON60, ALL, {ip: 85 for ip in rm("living room")})
for text in ("turn the lights on low", "put the lights on low", "lights on low"):
    run_case("bedroom", text, {}, rm("bedroom"), {ip: 30 for ip in rm("bedroom")})
for text in ("set the lights to low", "turn the lights to low"):
    run_case("bedroom", text, ON60, ALL, {ip: 35 for ip in rm("bedroom")})
run_case("bedroom", "turn on the lights I'm on my way down", {}, rm("bedroom"), {ip: 100 for ip in rm("bedroom")})
# P3: a deferral attached to a room / "here" / "later tonight" is still not NOW
for text in ("turn off the lights in here when I leave", "turn off the lights in the bedroom when I fall asleep",
             "turn off the light in the bedroom when I go to sleep", "turn off the lights later tonight",
             "turn off the lights a little later", "turn off the lights later on"):
    run_case("bedroom", text, ON60, ALL, label=f"{text} (refused, nothing now)")
run_case("bedroom", "turn on the lights in the living room when I get home", {}, set(), label="... when I get home (refused)")
# P4: "on the patio / deck" = no lights there; "on the counter / desk" is a landmark (asked from a room without lights)
run_case("kitchen", "turn on the lights on the patio", {}, set())
run_case("living room", "turn on the lights on the patio", {}, set())
run_case("kitchen", "turn off the lights on the deck", ON60, ALL)
run_case("kitchen", "Ava, turn off the light on the counter", ON60, ALL)
run_case("kitchen", "turn the light on the desk off", ON60, ALL)
run_case("bedroom", "turn the light on the desk off", ON60, ALL - rm("bedroom"))
run_case("kitchen", "turn the light on the nightstand off", ON60, ALL - rm("bedroom"))
# P5: a leading reason goes only up to its comma; a trailing one up to the next command
run_case("bedroom", "Since I'm going to bed, turn off the lights", ON60, ALL - rm("bedroom"))
run_case("kitchen", "Because I'm going to bed, turn off the lights.", ON60, set())
run_case("kitchen", "Cause we're leaving, turn off all the lights", ON60, set())
run_case("kitchen", "Since everyone's asleep, turn off the living room lights", ON60, ALL - rm("living room"))
run_case("kitchen", "turn off the living room lights cause I'm going to bed and turn on the bedroom",
         {ip: (True, 60) for ip in rm("living room")}, rm("bedroom"))
# ---- round 7, second pre-deploy check ----
# Q1: a remark / deferral AFTER the second room is the tail of ONE command (split: only the first room switched)
for text in ("turn off the living room and dining room lights when we go to bed",
             "turn off the dining room and the living room before we go to bed",
             "turn off the living room and the dining room after we eat"):
    run_case("bedroom", text, ON60, ALL, label=f"{text} (refused, nothing now)")
for text in ("turn on the living room and the dining room lights so I can see", "turn on the living room and dining room lights its dark",
             "turn on the living room and dining room lights I can't see", "turn on the living room and dining room lights we have company"):
    run_case("kitchen", text, {}, rm("living room", "dining room"))
run_case("kitchen", "turn off the living room and the bedroom lights we're going to bed", ON60, rm("dining room"))
run_case("kitchen", "turn off the lights and the kids are in the living room", ON60, set(), label="... and the kids are in the living room (a remark)")
# Q2: "later" with more after it, or a word between, is still later
for room, text in (("bedroom", "turn off the lights later when I go to bed"), ("kitchen", "turn off all the lights later on tonight"),
                   ("bedroom", "turn off the lights later this evening"), ("kitchen", "turn off the living room lights later after the movie"),
                   ("kitchen", "turn the living room lights off later tonight when we go to bed"),
                   ("kitchen", "turn off all the lights in the house when we leave"), ("bedroom", "turn off the lights as soon as I leave"),
                   ("kitchen", "make the bedroom dark when the baby falls asleep"), ("kitchen", "make the living room dark after dinner"),
                   ("kitchen", "make the living room dark in a bit"), ("kitchen", "make the bedroom dark tomorrow morning"),
                   ("kitchen", "make the living room dark later")):
    run_case(room, text, ON60, ALL, label=f"{text} (refused, nothing now)")
run_case("bedroom", "can you turn the lights on for me later", {}, set(), label="lights on for me later (refused)")
# Q3: the afterthought after a reason is kept
run_case("bedroom", "turn off the bedroom lights since I'm going to sleep, and the living room", ON60, rm("dining room"))
# Q4: really / very / nice and low, "go down", "way down low"
for text in ("turn the lights on really low", "turn the lights on very low", "turn the lights on nice and low"):
    run_case("bedroom", text, {}, rm("bedroom"), {ip: 30 for ip in rm("bedroom")})
for text in ("set the bedroom lights really low", "turn the bedroom lights pretty low", "make the lights go down",
             "put the lights really low"):
    run_case("bedroom", text, ON60, ALL, {ip: 35 for ip in rm("bedroom")})
run_case("bedroom", "put the lights way down low", ON60, ALL, {ip: 10 for ip in rm("bedroom")})
run_case("bedroom", "put the lights way down low", {}, set(), label="way down low with the lights off (nothing lit)")
# Q5: "on the stairs / second floor / front door" = no lights there (it switched the speaker's room)
for text in ("turn on the light on the stairs", "turn on the light on the staircase", "turn off the lights on the second floor",
             "turn on the light on the front door", "turn on the lights on the christmas tree"):
    run_case("bedroom", text, {} if text.startswith("turn on") else ON60,
             set() if text.startswith("turn on") else ALL, label=f"{text} (no lights there)")
# Q6: a sentence after "on" - "on" was the command, the "out" at its end isn't a place's off
run_case("kitchen", "turn the lights on the dog needs to go out", {}, ALL)
run_case("living room", "turn the lights on the dog needs to go out", {}, rm("living room"))
# ---- r6-vs-now differential corpus (1,499 everyday phrases) ----
run_case("dining room", "Turn on the dining room and make it bright, I'm doing homework.", {}, rm("dining room"),
         {ip: 100 for ip in rm("dining room")})
run_case("kitchen", "Turn on the light in the kitchen, it's dark in here.", {}, set())
run_case("kitchen", "Turn on the kitchen lights, I mean the dining room.", {}, rm("dining room"))
run_case("dining room", "Turn off dining room two, it's flickering.", ON60, ALL - rm("dining room 2"))
run_case("living room", "Turn on living room one.", {}, rm("living room 1"))
run_case("dining room", "Turn on the light out on the patio.", {}, set())
run_case("bedroom", "Turn the lamp on her side down.", ON60, ALL, {ip: 35 for ip in rm("bedroom")})
run_case("kitchen", "Turn off the light on my side of the bed.", ON60, ALL - rm("bedroom"))
run_case("dining room", "Make the dining room lights half as bright.", ON60, ALL, {ip: 35 for ip in rm("dining room")})
for text in ("Turn the lights on at 6:30 tomorrow morning.", "Turn all the lights on at 6 tomorrow morning."):
    run_case("bedroom", text, {}, set(), label=f"{text} (refused)")
# 10-17 real Echo mishears (kept turn audio): "dining" -> "denim", "dining room lights" -> "dining with lights"
run_case("bedroom", "Turn off denim light, please", ON60, ALL - rm("dining room"))
run_case("kitchen", "We're not dining with lights.", ON60, ALL - rm("dining room"))
run_case("bedroom", "We're not living on light.", {}, rm("living room"))
for text in ("We're dining in the living room tonight.", "The denim jacket is in the bedroom."):
    expect(text, "kitchen", "none")
# 10-19 real Echo mishears + status
run_case("bedroom", "You're not dining room light.", {}, rm("dining room"))
run_case("bedroom", "Turn on Daniel Light, please.", {}, rm("dining room"))
run_case("kitchen", "Turn on bedroom mic one.", {}, rm("bedroom 1"))
for text in ("turn on the denim room", "Turn on the denim.", "turn off denim"):
    run_case("kitchen", text, {} if " on" in text.lower() else ON60,
             rm("dining room") if " on" in text.lower() else ALL - rm("dining room"))
run_case("kitchen", "set the denim room to 50 percent", ON60, ALL, {ip: 50 for ip in rm("dining room")})
for text in ("My denim jacket is in the bedroom.", "I love denim.", "Wash my denim."):
    expect(text, "kitchen", "none")
res = say("Are the lights on in here?", "bedroom")
check("'are the lights on in here' = this room only", "bedroom", "dining" not in (res.get("say") or "").lower()
      and "living" not in (res.get("say") or "").lower(), res.get("say"))
reset(on=True, dimming=60)
res = say("How bright is the living room?", "kitchen")
check("'how bright is the living room' -> 60 percent", "kitchen", "60 percent" in (res.get("say") or ""), res.get("say"))
res = say("What color is the bedroom light?", "kitchen")
check("'what color is the bedroom light' -> a colour", "kitchen", res.get("ok") and re.search(
      r"\b(?:is|are) (?:white|warm white|soft white|daylight white|red|blue|green|purple|a scene|\w+)\.", res.get("say") or ""),
      res.get("say"))
# R20: bulbs that don't answer -> "I couldn't reach", not "already off"
_real_send = L._send
L._send = lambda ip, payload, timeout=1.2: None
r.lights._recent = {}
res = say("dim the lights", "bedroom")
check("dim with no bulb answering -> 'couldn't reach'", "bedroom", "couldn't reach" in res.get("say", ""), res.get("say"))
L._send = _real_send
r.lights._recent = {}

# ---- NOT light commands: nothing may change ----
NOT = ["All right, come on.", "All bets are off.", "Not at all, go on.", "Turn it all the way up.", "Turn it all the way down.",
       "The lights went out.", "All of a sudden the lights went off.", "turn it up in here", "turn it up to 80 percent",
       "turn it down to 20", "turn off the bedroom echo", "turn the bedroom echo off", "Turn the bedroom echo up.",
       "turn on lights by ellie goulding", "throw on lights by ellie goulding", "note that the bedroom light is off",
       "note the dining room bulb is out", "forget that the bedroom lights are on", "forget the living room lights",
       "keep in mind the dining room lights are off", "make a note that the bedroom lights are off",
       "don't let me forget to turn off the lights", "ask the bedroom if the lights are on",
       "And then the lights went out.", "The lights are on but nobody's home.", "Dad turned off the lights.",
       "Sam turned off the lights.", "My wife left the lights on.", "The power went out and turned off the lights.",
       "She needs to turn off the lights.", "Someone keeps turning the bedroom light on.",
       "I think the bedroom light is on.", "There's a light on in the bedroom.", "How long has the bedroom light been on?",
       "How much does it cost to leave the lights on?", "Why'd you turn off the lights?", "Why'd you turn the lights on?",
       "This little light of mine.", "The lights of the city are beautiful.", "In light of the news, we're staying home.",
       "It's light out.", "I'll knock your lights out.", "You light up my life.", "Light up the grill.",
       "Come on, let's go to the bedroom.", "Hold on, I'm in the bedroom.", "The bedroom is on the right.",
       "The bedroom is off limits.", "The remote is on the dining room table.", "I put the laundry on the bedroom floor.",
       "Get out of my bedroom.", "Get in the bedroom.", "We're off to the dining room.", "I'm on my way to the bedroom.",
       "Turn on Hulu in the bedroom.", "Turn on ESPN in the living room.", "Put on some jazz in the dining room.",
       "Turn on the bedroom humidifier.", "Turn off do not disturb in the bedroom.", "Turn on sleep mode in the bedroom.",
       "Turn off all the TVs.", "Turn off all the speakers.", "Turn off all notifications.", "Turn on all the burners.",
       "Turn off everything except the lights.", "Turn off the TV but leave the lights on.",
       "Turn off the music but leave the lights on.", "Put on Turn On the Lights by Future.",
       "Queue up Turn Off the Lights by Teddy Pendergrass.", "It's half past ten.", "It's a quarter to five.",
       "I'll do it at 9.", "It's 100 percent cotton.", "Charge it to 80 percent.", "Set the bedroom to 68 degrees.",
       "Lower the bedroom temperature.", "Stop turning off the lights.", "The kids keep turning on the lights.",
       "The lights keep turning off by themselves.", "turn on purple rain", "play blinding lights",
       "turn off the music in the bedroom", "turn the volume down in the living room", "who turned off the lights?",
       "what color are the lights", "why is the bedroom light flickering", "do the lights use much power",
       "I left the light on in the car", "the traffic light was red", "light beer please", "this bag is light",
       "I'm off to bed", "are you on or off", "turn on the charm", "go to the living room",
       "Lights, camera, action!", "Get everything off the table.", "Put the kids in the bedroom.",
       "Tell me when the lights go off.", "Put your shoes on in the bedroom.", "Get your stuff out of the living room."]
for text in NOT:
    for room in SATS:
        expect(text, room, "none")

print(f"{count - len(fails)}/{count} passed")
for room, text, why in fails:
    print(f"  [FAIL] ({room}) {text!r}: {why}")
sys.exit(1 if fails else 0)
