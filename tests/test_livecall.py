"""Live two-way calls between the Echoes (houseava/livecall.py + SatelliteHub.call_cmd + the router's phrases), with
fake Echoes that record what House Ava sends and replay the firmware's phone sensor / events.

    .venv/Scripts/python.exe tests/test_livecall.py
"""
import asyncio
import re
import os
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="avalc-")
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"

from houseava.livecall import LiveCalls                     # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


K = {"phone": 11, "phone_peer": 12, "phone_answer": 13, "phone_hangup": 14, "intercom_drop_in": 15,
     "intercom_do_not_disturb": 16, "setup_page": 17}


class FakeClient:
    def __init__(self, ec_name, reply=None):
        self.name = ec_name
        self.reply = reply            # what intercom_call answers: None (timeout) / SimpleNamespace(success, error_message)
        self.calls, self.buttons, self.switches = [], [], []
        self.on_call_started = None   # set phone sensor to Calling when dialing works

    async def execute_service(self, svc, data, return_response=None, timeout=30.0):
        self.calls.append((svc.name, dict(data), return_response))
        if svc.name == "intercom_call" and self.on_call_started and (self.reply is None or self.reply.success):
            self.on_call_started()
        if self.reply == "timeout":
            raise asyncio.TimeoutError()
        return self.reply

    def button_command(self, key, device_id=0):
        self.buttons.append(key)

    def switch_command(self, key, state, device_id=0):
        self.switches.append((key, state))


class Ent:
    def __init__(self, oid, key):
        self.object_id, self.key = oid, key


class Svc:
    def __init__(self, name):
        self.name = name


class St:
    def __init__(self, key, state):
        self.key, self.state = key, state


class Call:
    def __init__(self, data):
        self.service, self.is_event, self.data = "esphome.techo5_phone", True, data


class FakeSat:
    def __init__(self, name, room, client):
        self.name, self.room, self.ip, self.client, self.connected = name, room, "192.0.2.9", client, True


class FakeHub:
    def __init__(self):
        self.sats, self.said = [], []
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

        self.spans = []                       # (text, start, end) - announcements must not overlap on one Echo
        self.on_announce = None               # hook: called with the text as each one starts PLAYING
        self.on_synth = None                  # hook: called with the text while it's being synthesized

    async def _announce_on(self, sat, text, start_conversation=False, force=False, requeue=False):
        # the same order as SatelliteHub._announce_on: (queued check unless force) -> synth -> check again -> send
        if not force and self.calls.queued(sat):
            if start_conversation:
                return "busy"
            self.calls.hold(sat, text, start_conversation)
            return True
        if self.on_synth:
            self.on_synth(text)
        await asyncio.sleep(0.02)             # synthesis
        if self.calls.busy(sat) if requeue else self.calls.queued(sat):
            if start_conversation:
                return "busy"
            self.calls.hold(sat, text, start_conversation, front=requeue)
            return "held" if requeue else True
        t0 = time.monotonic()
        self.said.append((sat.name, text, force))
        if self.on_announce:
            self.on_announce(text)
        await asyncio.sleep(0.05)             # echod plays it; the hub awaits AnnounceFinished
        self.spans.append((text, t0, time.monotonic()))
        return True


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, hub._loop).result(10)


def settle():
    time.sleep(0.3)


ENTS = [Ent(o, k) for o, k in K.items()]
SVCS = [Svc("intercom_call"), Svc("phone_answer"), Svc("phone_hangup")]
hub = FakeHub()
calls = LiveCalls(hub)
hub.calls = calls
kc = FakeClient("Kitchen Echo", reply=types.SimpleNamespace(success=True, error_message=""))
bc = FakeClient("Echo Show", reply=types.SimpleNamespace(success=True, error_message=""))
kit, bed = FakeSat("Kitchen Echo", "kitchen", kc), FakeSat("Echo Show", "bedroom", bc)
hub.sats = [kit, bed]
for s in hub.sats:
    calls.learn(s, ENTS, SVCS, s.name)
kc.on_call_started = lambda: calls.on_state(kit, St(K["phone"], "Calling"))
bc.on_call_started = lambda: calls.on_state(bed, St(K["phone"], "Calling"))

# ---- placing a call ----
res = run(calls.call(kit, bed))
check("call kitchen -> bedroom runs intercom_call ON THE KITCHEN with the bedroom's name",
      kc.calls and kc.calls[-1][:2] == ("intercom_call", {"device": "Echo Show"}) and kc.calls[-1][2] is True, kc.calls)
check("... and says 'Calling the bedroom.'", res == {"ok": True, "say": "Calling the bedroom."}, res)
check("the bedroom's client was not used to dial", not bc.calls, bc.calls)
calls.on_state(kit, St(K["phone"], "Not set up"))

# ---- refusals before dialing ----
check("self-call refused", run(calls.call(kit, kit))["ok"] is False)
calls.on_state(bed, St(K["intercom_do_not_disturb"], True))
r = run(calls.call(kit, bed))
check("target with do-not-disturb on -> refused, not dialed", r["ok"] is False and "do not disturb" in r["say"], r)
calls.on_state(bed, St(K["intercom_do_not_disturb"], False))
calls.on_state(bed, St(K["phone"], "In call"))
n = len(kc.calls)
r = run(calls.call(kit, bed))
check("target already on a call -> refused", r["ok"] is False and "already on a call" in r["say"] and len(kc.calls) == n, r)
calls.on_state(bed, St(K["phone"], "Ready"))
bed.connected = False
check("target not connected -> refused", run(calls.call(kit, bed))["ok"] is False)
bed.connected = True

# ---- no house word -> fallback ----
kc.reply = types.SimpleNamespace(success=False, error_message="intercom: this device has no house word; set one on the setup page")
r = run(calls.call(kit, bed))
check("'no house word' -> fallback (walkie-talkie)", r.get("fallback") is True, r)
check("... remembered as not set up", calls.status(kit)["live_intercom"] == "not set up", calls.status(kit))
kc.reply = "timeout"
kc.on_call_started = None                       # nothing happens on the device either
r = run(calls.call(kit, bed))
check("no reply and the phone sensor never says Calling -> fallback", r.get("fallback") is True, r)
kc.reply = types.SimpleNamespace(success=True, error_message="")
kc.on_call_started = lambda: calls.on_state(kit, St(K["phone"], "Calling"))
calls.on_state(bed, St(K["intercom_drop_in"], True))
r = run(calls.call(kit, bed))
check("Drop In on at the target -> 'Dropping in on the bedroom.'", r.get("say") == "Dropping in on the bedroom.", r)
calls.on_state(bed, St(K["intercom_drop_in"], False))

# ---- why it didn't go through: told on the caller AFTER it's idle again ----
for reason, data_callee, want in (("busy", None, "on another call"), ("do not disturb", None, "do not disturb"),
                                  ("declined", "missed", "Nobody answered"), ("declined", "declined", "didn't pick up"),
                                  ("failed", None, "house word")):
    hub.said.clear()
    calls.on_state(kit, St(K["phone"], "Calling"))
    if data_callee:
        calls.on_service_call(bed, Call({"event": data_callee, "direction": "incoming", "peer": "Kitchen Echo", "kind": "intercom"}))
    calls.on_service_call(kit, Call({"event": "not_answered", "direction": "outgoing", "peer": "Echo Show",
                                     "kind": "intercom", "reason": reason}))
    settle()
    held_now = not hub.said
    calls.on_state(kit, St(K["phone"], "Not set up"))
    settle()
    said = " ".join(t for _, t, _ in hub.said)
    check(f"not_answered/{reason}{'/' + data_callee if data_callee else ''} -> said after idle: {want!r}",
          held_now and want in said and all(n == "Kitchen Echo" for n, _, _ in hub.said), (held_now, hub.said))

# ---- holding sound during a call ----
hub.said.clear()
calls.on_state(bed, St(K["phone"], "Ringing"))
check("announcement to a RINGING Echo is held", calls.hold(bed, "Dinner is ready.") is True and not hub.said)
calls.on_state(bed, St(K["phone"], "In call"))
calls.hold(bed, "Timer done.")
calls.on_state(bed, St(K["phone"], "Ready"))
settle()
check("... both said once it's idle, in order", [t for _, t, _ in hub.said] == ["Dinner is ready.", "Timer done."], hub.said)
hub.said.clear()
check("idle Echo: announcement goes straight out", calls.hold(bed, "Hi.") is False)
settle()
check("... and was sent", [t for _, t, _ in hub.said] == ["Hi."], hub.said)

# ---- the firmware never says "Idle" between calls (phone.go statusText: "Not set up" / "Ready" / ...) ----
calls.on_state(kit, St(K["phone"], "Ready"))
st = calls.status(kit)
check("sensor 'Ready' -> call_phase Idle (the app's Call buttons show), line 'Ready'",
      st["call_phase"] == "Idle" and st["line"] == "Ready", st)
calls.on_state(kit, St(K["phone"], "Connecting"))
check("'Connecting' / 'Login refused' are idle too", calls.status(kit)["call_phase"] == "Idle" and not calls.busy(kit))

# ---- held lines go out ONE AFTER ANOTHER (each announcement takes the speaker from the one playing) ----
hub.said.clear()
hub.spans.clear()
calls.on_state(bed, St(K["phone"], "In call"))
for line in ("Timer done.", "Reminder: call mom.", "From the kitchen: dinner."):
    calls.hold(bed, line)
calls.on_state(bed, St(K["phone"], "Not set up"))
time.sleep(0.5)
sp = sorted(hub.spans, key=lambda s: s[1])
check("three held lines all said, in order", [s[0] for s in sp] == ["Timer done.", "Reminder: call mom.", "From the kitchen: dinner."], sp)
check("... and never overlapping", all(sp[i][1] >= sp[i - 1][2] - 0.001 for i in range(1, len(sp))), sp)
# a new call starting while they drain: the rest wait for that one too
hub.said.clear()
hub.spans.clear()
calls.on_state(bed, St(K["phone"], "In call"))
for line in ("one", "two", "three"):
    calls.hold(bed, line)
hub.on_announce = lambda text: text == "one" and calls.on_state(bed, St(K["phone"], "Ringing"))
calls.on_state(bed, St(K["phone"], "Ready"))
time.sleep(0.4)
check("a call starts after the 1st line -> the other two are held again", [t for _, t, _ in hub.said] == ["one"]
      and [h[0] for h in calls.of(bed).held] == ["two", "three"], (hub.said, calls.of(bed).held))
hub.on_announce = None
calls.on_state(bed, St(K["phone"], "Ready"))
time.sleep(0.4)
check("... and said when that call ends", [t for _, t, _ in hub.said] == ["one", "two", "three"], hub.said)
# a call that starts WHILE a held line is being synthesized: that line waits too (sent, it would silence the ring)
hub.said.clear()
calls.on_state(bed, St(K["phone"], "In call"))
for line in ("A", "B"):
    calls.hold(bed, line)
hub.on_synth = lambda text: text == "A" and calls.on_state(bed, St(K["phone"], "Ringing"))
calls.on_state(bed, St(K["phone"], "Ready"))
time.sleep(0.4)
check("ring starts during synthesis -> the line is NOT sent, both wait in order",
      not hub.said and [h[0] for h in calls.of(bed).held] == ["A", "B"], (hub.said, calls.of(bed).held))
hub.on_synth = None
calls.on_state(bed, St(K["phone"], "Ready"))
time.sleep(0.4)
check("... and both said after it", [t for _, t, _ in hub.said] == ["A", "B"], hub.said)
# a whole call that comes and goes INSIDE one held line (a quick decline): one drain, no overlap, order kept
hub.said.clear()
hub.spans.clear()
calls.on_state(kit, St(K["phone"], "In call"))
for line in ("L1", "L2", "L3"):
    calls.hold(kit, line)


def quick_call(text):
    if text == "L1":
        calls.on_state(kit, St(K["phone"], "Calling"))
        calls.on_service_call(kit, Call({"event": "not_answered", "direction": "outgoing", "peer": "Echo Show",
                                         "kind": "intercom", "reason": "busy"}))
        calls.on_state(kit, St(K["phone"], "Ready"))


hub.on_announce = quick_call
calls.on_state(kit, St(K["phone"], "Ready"))
time.sleep(0.8)
hub.on_announce = None
sp = sorted(hub.spans, key=lambda s: s[1])
check("call in and out during L1 -> L1, L2, L3, then the reason - one at a time",
      [s[0] for s in sp][:3] == ["L1", "L2", "L3"] and len(sp) == 4 and "another call" in sp[3][0]
      and all(sp[i][1] >= sp[i - 1][2] - 0.001 for i in range(1, len(sp))), sp)
check("... nothing left queued, no drain stuck", not calls.of(kit).held and not calls.of(kit).draining,
      (calls.of(kit).held, calls.of(kit).draining))
# round 3: a NEW announcement while held lines are still being said waits behind them (sent at once it cut them off)
hub.said.clear()
hub.spans.clear()
calls.on_state(bed, St(K["phone"], "In call"))
for line in ("H1", "H2"):
    calls.hold(bed, line)
hub.on_announce = lambda text: text == "H1" and asyncio.ensure_future(hub._announce_on(bed, "NEW"))
calls.on_state(bed, St(K["phone"], "Ready"))
time.sleep(0.6)
hub.on_announce = None
sp = sorted(hub.spans, key=lambda s: s[1])
check("an announcement during the drain goes AFTER the held lines, no overlap",
      [s[0] for s in sp] == ["H1", "H2", "NEW"] and all(sp[i][1] >= sp[i - 1][2] - 0.001 for i in range(1, len(sp))), sp)
# round 3: an open-the-mic line ("Go ahead.") is never queued for after a call
calls.on_state(bed, St(K["phone"], "In call"))
r3 = run(hub._announce_on(bed, "Go ahead.", start_conversation=True))
check("'Go ahead.' to an Echo in a call -> 'busy', not queued", r3 == "busy" and not calls.of(bed).held, (r3, calls.of(bed).held))
calls.hold(bed, "plain line", start_conversation=True)
check("... and hold() never stores an open-the-mic line", calls.of(bed).held[-1] == ("plain line", False), calls.of(bed).held)
calls.on_state(bed, St(K["phone"], "Ready"))
time.sleep(0.3)

# ---- answer / hang up / switches ----
calls.on_state(bed, St(K["phone"], "Ringing"))
run(calls.press(bed, "phone_answer"))
check("answer presses phone_answer on that Echo", bc.buttons[-1] == K["phone_answer"], bc.buttons)
run(calls.press(bed, "phone_hangup"))
check("hang up presses phone_hangup", bc.buttons[-1] == K["phone_hangup"], bc.buttons)
calls.set_switch(bed, "intercom_drop_in", False)
check("drop-in off -> switch_command(key, False)", bc.switches[-1] == (K["intercom_drop_in"], False), bc.switches)
calls.on_state(bed, St(K["phone"], "Not set up"))

# ---- the router's phrases -> the hub ----
from houseava.app import build_router              # noqa: E402
from houseava.config import Config                 # noqa: E402

asked = []


def fake_calls(action, satellite="", room="", target="", source="", on=None):
    asked.append((action, target, source, on))
    if action in ("answer", "hangup"):
        return {"ok": True, "say": "ok"}
    if target in ("mom", "grandma"):
        return {"ok": False, "unknown_room": True, "say": "no echo"}
    return {"ok": True, "say": "ok"}


cfg = Config()
cfg.set("personality.adult", True)      # the dirty-joke tests below need the adult bank (off by default)
cfg.set("personality.cussing", True)
r = build_router(cfg)
r.live_call = fake_calls
r.intercom = lambda action, **kw: (asked.append(("walkie:" + action, kw.get("target", ""), kw.get("msg", ""), None))
                                   or {"ok": True, "say": "walkie"})
for text, want in (("call the bedroom", ("call", "bedroom", "", None)), ("Call the kitchen.", ("call", "kitchen", "", None)),
                   ("drop in on the kitchen", ("call", "kitchen", "", None)), ("talk to the bedroom", ("call", "bedroom", "", None)),
                   ("Alexa, call the bedroom please", ("call", "bedroom", "", None)),
                   ("call the kitchen from the bedroom", ("call", "kitchen", "bedroom", None)),
                   ("answer", ("answer", "", "", None)), ("hang up", ("hangup", "", "", None)),
                   ("end the call", ("hangup", "", "", None)),
                   ("turn off drop in for the bedroom", ("drop_in", "bedroom", "", False)),
                   ("turn on do not disturb for calls", ("dnd", "", "", True)),
                   # 10-16 review: double leads, a trailing name/okay, other wordings, the gate's missing words
                   ("Can you please call the bedroom?", ("call", "bedroom", "", None)),
                   ("Could you please call the bedroom", ("call", "bedroom", "", None)),
                   ("Call the bedroom, Ava.", ("call", "bedroom", "", None)),
                   ("Call the bedroom, okay?", ("call", "bedroom", "", None)),
                   ("Give the bedroom a call.", ("call", "bedroom", "", None)),
                   ("Call up the bedroom.", ("call", "bedroom", "", None)),
                   ("I want to call the bedroom.", ("call", "bedroom", "", None)),
                   ("I wanna call the bedroom.", ("call", "bedroom", "", None)),
                   ("I need to talk to the kitchen.", ("call", "kitchen", "", None)),
                   ("accept", ("answer", "", "", None)), ("accept in the bedroom", ("answer", "bedroom", "", None)),
                   ("hang it up", ("hangup", "", "", None)), ("hang it up in the bedroom", ("hangup", "bedroom", "", None))):
    asked.clear()
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> {want}", asked and asked[-1] == want, (asked, out))
asked.clear()
out = r.handle("turn on drop in for the bedroom", "kitchen", satellite="Kitchen Echo", heard="wake")
check("turning Drop In ON by voice is refused (privacy: the app only)", not asked and "app" in out["say"], (asked, out))
asked.clear()
r.handle("intercom the bedroom", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'intercom the bedroom' stays the walkie-talkie", asked and asked[-1][0] == "walkie:intercom", asked)
asked.clear()
r.handle("talk to the kitchen, dinner's ready", "bedroom", satellite="Echo Show", heard="wake")
check("'talk to the kitchen, dinner's ready' = a message (walkie), not a call", asked and asked[-1][0] == "walkie:intercom", asked)
asked.clear()
out = r.handle("call mom", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'call mom' -> not an Echo: back to the brain", not any(a[0] == "call" and a[1] == "mom" and "say" in out
                                                              and out["say"] == "no echo" for a in asked), (asked, out))

for text in ("call me a cab", "what do you call a fish with no eyes", "call it a night", "answer the question",
             "hang up the picture", "talk to me", "give me a call later"):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} is not a call", not any(a[0] in ("call", "answer", "hangup") for a in asked), asked)
# round 3 (T33/T37): double leads and whisper's commas after a lead word, for announce / intercom / calls alike
for text, want in (("Can you please announce dinner's ready?", "walkie:announce"),
                   ("Could you please tell the kitchen dinner's ready.", "walkie:announce"),
                   ("Can you please intercom the kitchen?", "walkie:intercom"),
                   ("Please, announce dinner's ready.", "walkie:announce"),
                   ("Um, call the kitchen.", "call"), ("So, can you call the bedroom?", "call"),
                   ("Can you, please, call the bedroom?", "call")):
    asked.clear()
    out = r.handle(text, "bedroom" if "kitchen" in text else "kitchen",
                   satellite="Echo Show" if "kitchen" in text else "Kitchen Echo", heard="wake")
    check(f"{text!r} -> {want}", asked and asked[-1][0] == want, (asked, out))
# T32: asking her to STOP dirty jokes never tells one
from houseava import jokes as _jokes     # noqa: E402
for text in ("Can you please stop telling dirty jokes?", "Okay, stop the dirty jokes.", "So, no more dirty jokes.",
             "I said no more dirty jokes.", "That's enough dirty jokes.", "Please, no dirty jokes.",
             "Hey, don't tell dirty jokes."):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} does not tell a joke", not _jokes.in_pool(out.get("say", "")), out.get("say", "")[:80])
out = r.handle("tell me a dirty joke", "kitchen", satellite="Kitchen Echo", heard="wake")
check("(control) 'tell me a dirty joke' still tells one", _jokes.in_pool(out.get("say", ""), "dirty"), out.get("say", "")[:80])
# round 4 (Q13): a trailing "... to eat / on the table / in five minutes" is not a room - the whole thing goes out
for text, msg in (("announce it's time to eat", "it's time to eat"), ("announce dinner is on the table", "dinner is on the table"),
                  ("announce we're leaving in five minutes", "we're leaving in five minutes")):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> announced to everyone, whole message", asked and asked[-1] == ("walkie:announce", "", msg, None), asked)
asked.clear()
r.handle("announce dinner's ready, come to the kitchen", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'... come to the kitchen' said IN the kitchen -> everyone else, not itself",
      asked and asked[-1][:2] == ("walkie:announce", ""), asked)
# Q15: "reply thanks" IS the message
for text, msg in (("reply thank you", "thank you"), ("reply thanks", "thanks")):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} sends {msg!r} (not a recording)", asked and asked[-1] == ("walkie:reply", "", msg, None), asked)
# Q16: a filler after the request word
asked.clear()
r.handle("can you, um, tell the kitchen dinner's ready", "bedroom", satellite="Echo Show", heard="wake")
check("'can you, um, tell the kitchen ...' -> announced to the kitchen", asked and asked[-1][:2] == ("walkie:announce", "kitchen"), asked)
asked.clear()
r.handle("can you uh call the kitchen", "bedroom", satellite="Echo Show", heard="wake")
check("'can you uh call the kitchen' -> call", asked and asked[-1][:2] == ("call", "kitchen"), asked)
# Q17: corrections / asides are still joke requests
for text in ("tell me another dirty joke, not that one", "tell me a dirty joke, no more dad jokes", "no, tell me a dirty one",
             "the kids are not home, tell me a dirty joke"):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} tells a dirty joke", _jokes.in_pool(out.get("say", ""), "dirty"), out.get("say", "")[:60])
for text in ("I said no more dirty jokes.", "I don't want any dirty jokes.", "stop telling dirty jokes"):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} still tells none", not _jokes.in_pool(out.get("say", "")), out.get("say", "")[:60])
# round 5 (P7/P8): a dirty joke is never told when the clause with the dirty words objects
for text in ("tell me a joke, not a dirty one", "tell us a joke, but no dirty ones, the kids are here", "keep it clean, no dirty ones",
             "I don't want a dirty one", "I don't want to hear any dirty jokes", "okay, that's enough of your dirty jokes",
             "I said don't tell me any more dirty jokes", "I don't like dirty jokes", "no, give me a break with the dirty jokes",
             "no, a dirty one? absolutely not", "give me something other than dirty jokes"):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} tells no dirty joke", not _jokes.in_pool(out.get("say", ""), "dirty"), out.get("say", "")[:60])
# P9: everyone-words never end up in the message
for text in ("announce to the whole house, dinner's ready", "announce to the whole family dinner's ready",
             "announce to the family dinner's ready", "announce to the other rooms dinner's ready"):
    asked.clear()
    r.handle(text, "bedroom", satellite="Echo Show", heard="wake")
    check(f"{text!r} -> everyone, clean message", asked and asked[-1] == ("walkie:announce", "", "dinner's ready", None), asked)
# P12: "I said thank you" keeps the thanks; "please and thank you" goes
asked.clear()
r.handle("tell the kitchen I said thank you", "bedroom", satellite="Echo Show", heard="wake")
check("'tell the kitchen I said thank you' keeps 'thank you'", asked and asked[-1][2] == "i said thank you", asked)
asked.clear()
r.handle("tell the kitchen dinner's ready please and thank you", "bedroom", satellite="Echo Show", heard="wake")
check("'... please and thank you' -> 'dinner's ready'", asked and asked[-1][2] == "dinner's ready", asked)
# round 6: refusals said in another clause / as contractions / as a retraction; encouragement is a request;
# complaints about the jokes are not requests
for text in ("No more jokes, especially the dirty ones.", "Stop with the jokes, especially the dirty ones.",
             "Tell me a joke that isn't a dirty one.", "Tell me a non-dirty joke.", "Tell me a dirty joke. Actually, never mind.",
             "Tell me a dirty joke, wait, the kids are here.", "That was a dirty joke.", "Turn off dirty jokes.",
             "Why did you tell my kid a dirty joke?", "Dirty jokes aren't funny."):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} tells no dirty joke", not _jokes.in_pool(out.get("say", ""), "dirty"), out.get("say", "")[:60])
for text in ("why don't you tell me a dirty joke", "tell me a dirty joke and don't hold back", "dirty joke please",
             "got any dirty ones"):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} tells a dirty joke", _jokes.in_pool(out.get("say", ""), "dirty"), out.get("say", "")[:60])
# S13: a relayed message with an alarm word is sent, not obeyed
for text in ("Tell the bedroom code red, the baby needs a diaper.", "Tell the bedroom to turn off the alarm.",
             "Announce all clear."):
    asked.clear()
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} is sent as a message", asked and asked[-1][0] == "walkie:announce", (asked, out.get("say")))
# S16 / S17 / S18: the place stays in the message; people; let/ask/make; bare infinitive
for text, room_, want in (("Announce your phone is in the kitchen.", "bedroom", ("walkie:announce", "", "your phone is in the kitchen")),
                          ("Tell the bedroom to come eat.", "kitchen", ("walkie:announce", "bedroom", "come eat")),
                          ("Let everyone know dinner is ready.", "kitchen", ("walkie:announce", "", "dinner is ready")),
                          ("Make an announcement: dinner is ready.", "kitchen", ("walkie:announce", "", "dinner is ready")),
                          ("Ask the bedroom if they want pizza.", "kitchen", ("walkie:announce", "bedroom", "Do you want pizza?")),
                          ("Reply back I am coming.", "bedroom", ("walkie:reply", "", "i am coming"))):
    asked.clear()
    r.handle(text, room_, satellite="Echo Show" if room_ == "bedroom" else "Kitchen Echo", heard="wake")
    check(f"{text!r} -> {want}", asked and asked[-1][:3] == want, asked)
# round 7: "let me know" / "ask Alexa" / "ask him" are not messages; "tell the kids a joke" is told HERE
_ic_ok = r.intercom


def _ic_real(action, **kw):
    """Like the hub: unknown rooms are unknown, and nobody has intercommed this Echo yet."""
    asked.append(("walkie:" + action, kw.get("target", ""), kw.get("msg", ""), None))
    if action == "reply":
        return {"ok": False, "say": "Nobody's intercommed you yet."}
    if kw.get("target", "") not in ("", "bedroom", "kitchen"):
        return {"ok": False, "unknown_room": True, "say": f"There's no Echo in the {kw.get('target')}."}
    return {"ok": True, "say": "walkie"}


r.intercom = _ic_real
for text in ("let me know when the timer is done", "let us know when dinner is ready", "ask Alexa to turn off the lights",
             "ask him what he wants", "ask them if they want pizza", "tell him a joke", "tell her that"):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} is not sent to another Echo", not any(a[0] == "walkie:announce" for a in asked), asked)
# pre-deploy check: a one-word name is a person ("let Sam know ..."); "ask them ..." after a message = a reply
for text, want in (("let Sam know dinner's ready", ("walkie:announce", "", "Sam, dinner's ready")),
                   ("ask Sam if she wants pizza", ("walkie:announce", "", "Sam, do you want pizza?"))):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> {want}", asked and asked[-1][:3] == want, asked)
for text, want in (("ask mom if she needs her glasses", ("walkie:announce", "", "Mom, do you need your glasses?")),
                   ("ask dad if he has his keys", ("walkie:announce", "", "Dad, do you have your keys?")),
                   ("ask mom what she wants for her birthday", ("walkie:announce", "", "Mom, what do you want for your birthday?")),
                   ("ask the bedroom if he wants his dinner", ("walkie:announce", "bedroom", "Do you want your dinner?"))):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> {want}", asked and asked[-1][:3] == want, asked)
for text in ("let my boss know I'm running late", "let my teacher know I'm sick", "ask my boss if I can leave early",
             "let our neighbor know the dog got out"):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} is not announced", not any(a[0] == "walkie:announce" and a[1] == "" for a in asked), asked)
# 10-19: a cut-off message asks for the rest; a garbled "turn on ..." asks again (it used to end in silence); timers
# said the way whisper writes them; "thank you" right after she did something gets an answer
for text, want in (("Tell Jordan to", "Tell Jordan what?"), ("Ask Sam if", "Ask Sam what?")):
    asked.clear()
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> {want!r}, nothing sent", out.get("say") == want and not asked, (out.get("say"), asked))
out = r.handle("Turn on most messages.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Turn on most messages.' after the wake word is never ignored (silence)", not out.get("ignored")
      and out.get("say"), out)
for text, want in (("We've got a 15 minute timer.", "15 minutes"), ("Set a 20 minute timer", "20 minutes"),
                   ("give me a 90 second timer", "90 seconds")):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> a {want} timer", want in out.get("say", ""), out.get("say"))
for tm in list(getattr(r, "_timers", [])):
    tm.cancel()
out = r.handle("The 15 minute timer is annoying.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'The 15 minute timer is annoying.' sets no timer", "shout when" not in out.get("say", ""), out.get("say"))
r._replied_at = {}
out = r.handle("Thank you.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Thank you.' out of the blue (the TV) -> no thank-you reply",
      out.get("say", "") not in ("Anytime.", "You got it.", "Don't mention it.", "Sure thing.", "Anytime, babe.",
                                 "That's what I'm here for.", "You're welcome.", "No problem.", "Of course."), out.get("say"))
r.handle("what time is it", "kitchen", satellite="Kitchen Echo", heard="wake")
out = r.handle("Thank you.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'Thank you.' right after she answered -> a short reply", len(out.get("say", "")) < 30 and out.get("ok"), out.get("say"))
out = r.handle("eat a fat butthole, are you?", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'eat a fat butthole' -> a comeback, not a polite line", "manners" not in out.get("say", "").lower()
      and "watch your" not in out.get("say", "").lower(), out.get("say"))
r.intercom = _ic_ok
for text in ("could you give me the time", "can you tell us the time", "check the time", "Alright, what's the time",
             "I need the time", "you got the time?", "Uh, what's the time?", "Hey Ava, uh, what's the time?"):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> the clock", bool(re.match(r"^It's \d", out.get("say", ""))), out.get("say"))
for text in ("I need a dirty joke for my bachelor party tomorrow", "tell me a dirty one once more",
             "tell me another dirty joke after that one"):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} is not refused for later", "save one for later" not in out.get("say", ""), out.get("say", "")[:60])
asked.clear()
r.handle("ask them if they want pizza", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'ask them if they want pizza' (after a message) -> a reply 'Do you want pizza?'",
      asked and asked[-1][:3] == ("walkie:reply", "", "Do you want pizza?"), asked)
for text in ("can you tell me the time", "Could you tell me the time please?", "what's the current time"):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> the clock", bool(re.match(r"^It's \d", out.get("say", ""))), out.get("say"))
out = r.handle("is the weather going to be nasty tomorrow", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'... nasty tomorrow' is not a deferred dirty joke", "save one for later" not in out.get("say", ""), out.get("say"))
out = r.handle("tell me a dirty joke, it's just us tonight", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'a dirty joke, it's just us tonight' tells one", _jokes.in_pool(out.get("say", ""), "dirty"), out.get("say", "")[:60])
for text in ("tell the kids a joke", "tell everyone a joke"):
    asked.clear()
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> a joke here, nothing sent", _jokes.in_pool(out.get("say", "")) and not asked, (asked, out.get("say")))
out = r.handle("tell the kids a dirty joke", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'tell the kids a dirty joke' -> refused", "not for the kids" in out.get("say", "").lower(), out.get("say"))
for text in ("tell me a dirty joke when the kids go to bed", "tell me a dirty joke. I'm kidding."):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} tells none now", not _jokes.in_pool(out.get("say", ""), "dirty"), out.get("say", "")[:60])
for text in ("gimme a dirty joke", "can I get a dirty joke", "what's your best dirty joke"):
    out = r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} tells a dirty joke", _jokes.in_pool(out.get("say", ""), "dirty"), out.get("say", "")[:60])
out = r.handle("It's movie time.", "kitchen", satellite="Kitchen Echo", heard="wake")
check("'It's movie time.' is not the clock", not re.match(r"^It's \d", out.get("say", "")), out.get("say"))
for text in ("hang up the call", "hangup", "end the intercom"):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} hangs up", asked and asked[-1][0] == "hangup", asked)
for text, want in (("tell my mom dinner is ready", "Mom, dinner is ready"), ("let mom know dinner is ready", "Mom, dinner is ready")):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} -> everyone, {want!r}", asked and asked[-1][2] == want, asked)
# R23: the calls-only lead must not turn everyday "I need to ..." into an intercom reply / house-wide announcement
for text in ("I need to reply to an email.", "I want to reply to my boss tonight", "I need to tell everyone about my promotion",
             "i want to do it again"):
    asked.clear()
    r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
    check(f"{text!r} is not an intercom/announcement", not any(a[0].startswith("walkie:") for a in asked), asked)


# ---- the real hub: the app's Call when the live intercom fails; switches on the hub's own thread ----
from houseava.http_server import ReplyServer      # noqa: E402
from houseava.satellite import Satellite, SatelliteHub   # noqa: E402


class HCfg:
    def __init__(self):
        self.d = {"http.host": "127.0.0.1", "http.port": 0, "http.public_host": "127.0.0.1"}

    def get(self, k, d=None):
        return self.d.get(k, d)


class HubClient(FakeClient):
    def __init__(self, name):
        super().__init__(name, reply=types.SimpleNamespace(success=False, error_message="intercom: this device has no house word"))
        self.switch_threads, self.announced = [], []

    def switch_command(self, key, state, device_id=0):
        self.switch_threads.append(threading.current_thread().name)
        super().switch_command(key, state, device_id)

    async def send_voice_assistant_announcement_await_response(self, media_id, timeout, text="", preannounce_media_id="",
                                                               start_conversation=False):
        self.announced.append(text)
        return types.SimpleNamespace(success=True)


class HRouter:
    def handle(self, text, room="", **kw):
        return {"ok": True, "say": "ok"}


hloop = asyncio.new_event_loop()
threading.Thread(target=hloop.run_forever, daemon=True, name="hub-loop").start()
H = SatelliteHub(HCfg(), router=HRouter(), transcribe=lambda pcm: "", synth=lambda t: b"RIFF" + b"\0" * 40,
                 replies=ReplyServer(HCfg()).start())
H._loop = hloop
hk = Satellite(H, {"name": "Kitchen Echo", "ip": "127.0.0.1", "room": "kitchen"})
hb = Satellite(H, {"name": "Echo Show", "ip": "127.0.0.1", "room": "bedroom"})
for s in (hk, hb):
    s.client, s.connected = HubClient(s.name), True
    H.calls.learn(s, ENTS, SVCS, s.name)
H.sats = [hk, hb]
res = H.call_cmd("call", target="bedroom", source="kitchen")             # the app's Call button (no Echo heard it)
check("app Call + no house word -> says how to fix it, opens NO mic anywhere",
      res.get("ok") is False and "house word" in res.get("say", "") and not res.get("intercom_open")
      and hk.intercom_to is None and hb.intercom_to is None, res)
res = H.call_cmd("call", satellite="Kitchen Echo", room="kitchen", target="kitchen", source="bedroom")
check("said at the KITCHEN 'call the kitchen from the bedroom' + no house word -> no message armed at the wrong Echo",
      res.get("ok") is False and not res.get("intercom_open") and hk.intercom_to is None, res)
res = H.call_cmd("call", satellite="Kitchen Echo", room="kitchen", target="bedroom")   # said AT the kitchen
check("said at the calling Echo + no house word -> walkie-talkie: 'leave a message. Go ahead.'",
      res.get("intercom_open") and "leave a message" in res.get("say", ""), res)
# P11: a room's alias reaches its Echo ("the master bedroom")
r_ = H.intercom_cmd("announce", satellite="Kitchen Echo", room="kitchen", target="master bedroom", msg="dinner's ready")
check("announce to the master bedroom -> the bedroom Echo", r_.get("ok") and not r_.get("unknown_room"), r_)
check("call_cmd finds 'the master bedroom'", H.call_cmd("status").get("ok") and
      H.call_cmd("drop_in", target="the master bedroom").get("say", "").endswith("for the bedroom."),
      H.call_cmd("drop_in", target="the master bedroom"))
time.sleep(0.3)
# U16: an offline Echo is never "told"
hb.connected = False
r_ = H.intercom_cmd("announce", satellite="Kitchen Echo", room="kitchen", target="bedroom", msg="dinner's ready")
check("announce to an OFFLINE bedroom -> says it isn't connected (not 'Told the bedroom')",
      not r_.get("ok") and "connected" in r_.get("say", ""), r_)
hb.connected = True
# Q14: a "tell the kitchen ..." message can be replied to (it sounds just like an intercom message)
r_ = H.intercom_cmd("announce", satellite="Echo Show", room="bedroom", target="kitchen", msg="dinner's ready")
check("tell the kitchen X -> the kitchen's reply goes back to the bedroom", hk.intercom_from == "Echo Show", (r_, hk.intercom_from))
time.sleep(0.3)
res = H.call_cmd("dnd", target="bedroom", on=True)
check("do-not-disturb switch sent on the hub's loop thread (the API connection isn't thread-safe)",
      res.get("ok") and hb.client.switch_threads == ["hub-loop"], (res, hb.client.switch_threads))
res = H.call_cmd("drop_in", target="bedroom", on=False)
check("drop-in off: on the loop thread too", res.get("ok") and hb.client.switch_threads[-1] == "hub-loop",
      hb.client.switch_threads)
# T36: a timer / announce() synthesized while the bedroom started ringing is held, not sent onto the ring
real_synth = H.synth
H.synth = lambda text: (text == "Timer done!" and H.calls.on_state(hb, St(K["phone"], "Ringing"))) or b"RIFF" + b"\0" * 40
asyncio.run_coroutine_threadsafe(H.announce("Timer done!", "bedroom"), hloop).result(5)
check("announce() re-checks after synthesis: held for the ringing Echo",
      "Timer done!" not in hb.client.announced and [h[0] for h in H.calls.of(hb).held] == ["Timer done!"],
      (hb.client.announced, H.calls.of(hb).held))
H.synth = real_synth
H.calls.on_state(hb, St(K["phone"], "Ready"))
time.sleep(0.5)
check("... and said once the ring ends", "Timer done!" in hb.client.announced, hb.client.announced)
# T34: "Go ahead." when the Echo is in a call: disarmed + told after the call, never a stray open mic later
H.calls.on_state(hk, St(K["phone"], "In call"))
hk.intercom_to, hk.intercom_until = hb, time.time() + 15
asyncio.run_coroutine_threadsafe(H._go_ahead(hk, hb, "Go ahead."), hloop).result(5)
check("'Go ahead.' during a call -> intercom disarmed, nothing open-mic queued",
      hk.intercom_to is None and all(not sc for _, sc in H.calls.of(hk).held)
      and any("intercom closed" in t for t, _ in H.calls.of(hk).held), (hk.intercom_to, H.calls.of(hk).held))
H.calls.on_state(hk, St(K["phone"], "Ready"))
time.sleep(0.5)

# 10-19: "stop listening for an hour" = echod's mic_mute on THAT Echo (after its reply), back on by itself / by
# "unmute the kitchen"; "you keep waking up" = only that Echo's wake strictness goes up
hk.mic_mute_key, hb.mic_mute_key = 901, 902
hk.client.switches.clear()
res = H.mute_cmd("mute", satellite="Kitchen Echo", room="kitchen", minutes=60)
check("stop listening -> 'Okay, I'll stop listening for 1 hour'", res.get("ok") and "1 hour" in res.get("say", ""), res)
check("... not muted yet (her reply goes out first)", (901, True) not in hk.client.switches, hk.client.switches)
time.sleep(4.6)
check("... muted ~4 s later", (901, True) in hk.client.switches, hk.client.switches)
check("... the other Echo untouched", not [s for s in hb.client.switches if s[0] == 902], hb.client.switches)
check("... the unmute is scheduled and saved", hk.unmute_handle is not None and "Kitchen Echo" in H._mutes(), H._mutes())
res = H.mute_cmd("unmute", room="kitchen", named=True)
time.sleep(0.3)
check("unmute the kitchen -> mic back on, nothing scheduled", res.get("ok") and (901, False) in hk.client.switches
      and hk.unmute_handle is None and "Kitchen Echo" not in H._mutes(), (res, hk.client.switches))
H._save_mutes({"Echo Show": time.time() - 5})          # a mute whose hour ran out while the hub was down
hb.client.switches.clear()
H._restore_mute(hb)
time.sleep(0.3)
check("restart after the hour ran out -> mic back on", (902, False) in hb.client.switches, hb.client.switches)
res = H.mute_cmd("mute", satellite="Kitchen Echo", room="kitchen", named=False, minutes=1)
time.sleep(4.6)
hk.client.switches.clear()
time.sleep(57)
check("a 1-minute mute turns itself back off", (901, False) in hk.client.switches, hk.client.switches)
before = hb.conf.get("wake_threshold")
res = H.wake_cmd("less", satellite="Echo Show", room="bedroom")
check("you keep waking up -> that Echo's wake strictness 0.87 -> 0.90, the other's untouched",
      res.get("ok") and hb.conf.get("wake_threshold") == 0.9 and hk.conf.get("wake_threshold") is None
      and before is None, (res, hb.conf, hk.conf))
check("... and it's what that Echo's slots get", abs((hb.wake_threshold_for(0) or 0) - 0.9) < 1e-9,
      hb.wake_threshold_for(0))
for _ in range(5):
    H.wake_cmd("less", satellite="Echo Show")
check("... never past 0.97", hb.conf.get("wake_threshold") == 0.97, hb.conf)

print(f"\n{len(fails)} failure(s)" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
