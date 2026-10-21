"""Announce / intercom / reply between the Echoes — the router's parsing and the hub's delivery, with fake Echoes.

    .venv/Scripts/python.exe tests/test_intercom.py
"""
import asyncio
import os
import sys
import tempfile
import types
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="avaic-")
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

import houseava.lights as _L                                             # noqa: E402
from houseava.http_server import ReplyServer                              # noqa: E402
from houseava.satellite import Satellite, SatelliteHub, Turn               # noqa: E402

# never reach the real bulbs: "turn off the bedroom lights" below really switched a bedroom off once
_L._send = lambda ip, payload, timeout=1.2: {"method": payload.get("method"), "result": {"success": True, "state": False}}

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


# ---- the router's parsing (a fake hub records what it was asked) ----------------------------------
class FakeIntercom:
    def __init__(self):
        self.calls = []
        self.rooms = {"kitchen", "bedroom"}

    def __call__(self, action, satellite="", room="", target="", msg="", named=False):
        self.calls.append((action, satellite, room, target, msg))
        t = (target or "").lower().removeprefix("the ").strip()
        if action == "announce" and t and t not in self.rooms | {"all", "everyone", "everybody"}:
            return {"ok": False, "unknown_room": True, "say": f"There's no Echo in the {t}."}
        if action == "intercom" and t not in self.rooms:
            return {"ok": False, "unknown_room": True, "say": f"There's no Echo in the {t}."}
        return {"ok": True, "say": "fake"}


def adult_config():
    """These tests tell the dirty jokes on purpose, so they switch the adult bank on (it's off by default)."""
    from houseava.config import Config
    cfg = Config()
    cfg.set("personality.adult", True)
    cfg.set("personality.cussing", True)
    return cfg


def router_tests():
    from houseava.app import build_router
    from houseava.config import Config
    r = build_router(adult_config())
    fake = FakeIntercom()
    r.intercom = fake

    def ask(text, room="bedroom"):
        fake.calls.clear()
        res = r.handle(text, room, satellite="Echo Show", heard="wake")
        return res, (fake.calls[-1] if fake.calls else None)

    res, c = ask("announce dinner's ready")
    check("announce X -> everyone", c == ("announce", "Echo Show", "bedroom", "", "dinner's ready"), c)
    res, c = ask("Alexa, announce to everyone that dinner's ready, please")
    check("announce to everyone that X (lead/tail stripped)", c and c[3] == "" and c[4] == "dinner's ready", c)
    res, c = ask("tell everyone dinner is ready")
    check("tell everyone X -> everyone", c and c[0] == "announce" and c[3] == "" and c[4] == "dinner is ready", c)
    res, c = ask("broadcast the movie is starting")
    check("broadcast X", c and c[0] == "announce" and c[4] == "the movie is starting", c)
    res, c = ask("announce to the kitchen dinner's ready")
    check("announce to the ROOM X", c == ("announce", "Echo Show", "bedroom", "kitchen", "dinner's ready"), c)
    res, c = ask("announce dinner's ready in the kitchen")
    check("announce X in the ROOM", c and c[3] == "kitchen" and c[4] == "dinner's ready", c)
    res, c = ask("tell the kitchen dinner's ready")
    check("tell the ROOM X", c and c[0] == "announce" and c[3] == "kitchen" and c[4] == "dinner's ready", c)
    res, c = ask("tell the kids to go to bed")
    check("tell the kids X -> said to everyone, addressed ('Kids, go to bed')",
          c and c[0] == "announce" and c[3] == "" and c[4] == "Kids, go to bed", c)
    res, c = ask("tell the truth")
    check("tell the <not a room / not a person> ... falls back to the brain (no fake reply)", res.get("say") != "fake", res)
    res, c = ask("tell me a dirty joke")
    check("tell me a dirty joke is still a joke, not an announcement", c is None and "fake" not in res.get("say", ""), res)
    res, c = ask("intercom the kitchen")
    check("intercom the ROOM -> two-step (no msg)", c == ("intercom", "Echo Show", "bedroom", "kitchen", ""), c)
    res, c = ask("drop in on the kitchen")
    check("drop in on the ROOM", c and c[0] == "intercom" and c[3] == "kitchen", c)
    res, c = ask("talk to the kitchen")
    check("talk to the ROOM", c and c[0] == "intercom" and c[3] == "kitchen", c)
    res, c = ask("intercom the kitchen, dinner's ready")
    check("intercom the ROOM, X -> message in her voice", c and c[0] == "intercom" and c[4] == "dinner's ready", c)
    res, c = ask("intercom the kitchen and say come here")
    check("intercom the ROOM and say X", c and c[4] == "come here", c)
    res, c = ask("reply")
    check("reply (no words) -> two-step reply", c == ("reply", "Echo Show", "bedroom", "", ""), c)
    res, c = ask("reply on my way")
    check("reply X", c and c[0] == "reply" and c[4] == "on my way", c)
    res, c = ask("reply to them and say five minutes")
    check("reply to them and say X", c and c[4] == "five minutes", c)
    for text in ("talk to me", "what time is it", "play some music", "turn off the bedroom lights"):
        res, c = ask(text)
        check(f"not an intercom thing: {text!r}", c is None, c)
    # review findings 10-15: a two-word "room" that swallowed the message's first word; "talk to <person>"
    res, c = ask("tell the kitchen dinner is ready")
    check("'tell the kitchen dinner is ready' -> kitchen / 'dinner is ready' (no apostrophe to save it)",
          c == ("announce", "Echo Show", "bedroom", "kitchen", "dinner is ready"), c)
    res, c = ask("announce to the kitchen that dinner is ready")
    check("'announce to the kitchen that dinner is ready'", c and c[3] == "kitchen" and c[4] == "dinner is ready", c)
    res, c = ask("intercom the kitchen and say come here")
    check("'intercom the kitchen and say come here' -> kitchen / 'come here'", c == ("intercom", "Echo Show", "bedroom", "kitchen", "come here"), c)
    res, c = ask("drop in on the kitchen and tell them dinner is ready")
    check("'drop in on the kitchen and tell them X'", c and c[0] == "intercom" and c[3] == "kitchen" and c[4] == "dinner is ready", c)
    res, c = ask("talk to grandma")
    check("'talk to grandma' is for the brain, not an Echo error", res.get("say") != "fake" and "no echo" not in res.get("say", "").lower(), res)
    # dirty-joke edges from the review
    from houseava import jokes
    # an insult right after the wake word is FOR her: the brain must not be offered 'ignore' (10-15 "eat a dick")
    from houseava import brain as B
    check("'eat a dick' is aimed at her", bool(B._RX_AT_HER.search("eat a dick")) and bool(B._RX_AT_HER.search("Fuck you, Ava")))
    check("... 'let's eat at Nick's' / 'pass the dip' are not", not B._RX_AT_HER.search("let's eat at nick's") and not B._RX_AT_HER.search("pass the dip"))
    res, _ = ask("stop telling dirty jokes")
    check("'stop telling dirty jokes' does NOT tell one", not jokes.in_pool(res.get("say", "")), res.get("say", "")[:60])
    res, _ = ask("tell me a dirty joke")
    check("(setup) a dirty joke was told", jokes.in_pool(res.get("say", ""), "dirty"))
    res, _ = ask("tell me another one, please")
    check("'tell me another one, please' (tail) -> another dirty joke", jokes.in_pool(res.get("say", ""), "dirty"), res.get("say", "")[:60])
    fake.calls.clear()
    res = r.handle("one more", "bedroom", satellite="Echo Show", heard="follow_up")
    check("'one more' as a follow-up right after a joke -> another dirty joke", jokes.in_pool(res.get("say", ""), "dirty"), res.get("say", "")[:60])


# ---- the hub's delivery, with fake Echoes ------------------------------------------------------
class Cfg:
    def __init__(self):
        self.d = {"http.host": "127.0.0.1", "http.port": 0, "http.public_host": "127.0.0.1"}

    def get(self, k, d=None):
        return self.d.get(k, d)


class FakeClient:
    def __init__(self):
        self.events, self.announced = [], []
        self.fail_next = False                      # echod cut the next announcement off (AnnounceFinished success=False)

    def send_voice_assistant_event(self, etype, data):
        from aioesphomeapi import VoiceAssistantEventType as E
        self.events.append((E(etype).name.replace("VOICE_ASSISTANT_", ""), dict(data or {})))

    async def send_voice_assistant_announcement_await_response(self, media_id, timeout, text="", preannounce_media_id="",
                                                               start_conversation=False):
        self.announced.append({"url": media_id, "text": text, "start": start_conversation})
        ok, self.fail_next = not self.fail_next, False
        return types.SimpleNamespace(success=ok)


class FakeRouter:
    def handle(self, text, room="", **kw):
        return {"ok": True, "say": "ok"}


WAV = b"RIFF" + b"\0" * 40          # not a real WAV: the lead-in is dropped, the recording still plays


async def hub_tests():
    import houseava.speechgate as sg
    # the real Silero VAD needs real speech; here "anything non-zero" is speech, zeros are silence
    sg.silero_spans = lambda pcm: [] if not any(pcm) else [(0.1, max(0.2, len(pcm) / 32000 - 0.1))]
    replies = ReplyServer(Cfg()).start()
    hub = SatelliteHub(Cfg(), router=FakeRouter(), transcribe=lambda pcm: "words", synth=lambda t: WAV, replies=replies)
    hub._loop = asyncio.get_running_loop()
    kitchen = Satellite(hub, {"name": "kitchen-echo", "ip": "127.0.0.1", "room": "kitchen"})
    bedroom = Satellite(hub, {"name": "bedroom-echo", "ip": "127.0.0.1", "room": "bedroom"})
    for s in (kitchen, bedroom):
        s.client, s.connected = FakeClient(), True
    hub.sats = [kitchen, bedroom]

    r = hub.intercom_cmd("announce", satellite="kitchen-echo", room="kitchen", target="", msg="dinner's ready")
    await asyncio.sleep(0.05)
    check("announce from the kitchen -> plays in the bedroom only", len(bedroom.client.announced) == 1
          and not kitchen.client.announced and r["say"] == "Announced.", (r, bedroom.client.announced))
    check("... prefixed with where it came from", bedroom.client.announced[0]["text"] == "From the kitchen: dinner's ready",
          bedroom.client.announced)
    r = hub.intercom_cmd("announce", satellite="kitchen-echo", room="kitchen", target="the bedroom", msg="come eat")
    await asyncio.sleep(0.05)
    check("announce to one room", r["say"] == "Told the bedroom." and bedroom.client.announced[-1]["text"].endswith("come eat"), r)
    r = hub.intercom_cmd("announce", satellite="kitchen-echo", room="kitchen", target="garage", msg="hi")
    check("unknown room is flagged for the router", r.get("unknown_room") is True, r)
    r = hub.intercom_cmd("intercom", satellite="kitchen-echo", room="kitchen", target="kitchen")
    check("intercom to your own Echo is refused", not r["ok"] and "this Echo" in r["say"], r)

    # two-step: "intercom the bedroom" -> Go ahead (mic opens) -> recording -> plays in the bedroom -> "Sent."
    r = hub.intercom_cmd("intercom", satellite="kitchen-echo", room="kitchen", target="bedroom")
    check("intercom the ROOM -> Go ahead + open", r.get("intercom_open") is True and r["intercom_target"] is bedroom, r)
    kitchen.client.events.clear()
    await hub._open_intercom(kitchen, Turn("c1"), "intercom the bedroom", r)
    await asyncio.sleep(0.05)
    names = [e[0] for e in kitchen.client.events]
    check("the command turn closes quietly with the words on screen", names == ["STT_END", "INTENT_START", "INTENT_END", "RUN_END"], names)
    check("'Go ahead.' is announced WITH start_conversation", kitchen.client.announced[-1] == {
        "url": kitchen.client.announced[-1]["url"], "text": "Go ahead.", "start": True}, kitchen.client.announced[-1])
    check("the kitchen now expects a recording for the bedroom", kitchen.intercom_to is bedroom and kitchen.intercom_until > 0)
    kitchen.client.events.clear()
    n_before = len(bedroom.client.announced)
    await kitchen.handle_start("c2", 0, None, "")          # the mic the announcement opened
    check("that turn is marked as the recording (not a follow-up)", kitchen.turn is not None and kitchen.turn.intercom is bedroom
          and not kitchen.turn.follow_up and kitchen.intercom_to is None)
    await kitchen.handle_audio(b"\x10\x00" * 16000)        # 1 s of "voice"
    await kitchen.handle_stop(False)
    await asyncio.sleep(0.05)
    check("the recording is NOT transcribed/answered (no STT_END/INTENT/TTS), it just ends",
          [e[0] for e in kitchen.client.events] == ["RUN_START", "STT_START", "RUN_END"], kitchen.client.events)
    check("it plays in the bedroom as an announcement", len(bedroom.client.announced) == n_before + 1
          and bedroom.client.announced[-1]["text"] == "Intercom from the kitchen", bedroom.client.announced[-1:])
    body = urllib.request.urlopen(bedroom.client.announced[-1]["url"], timeout=5).read()
    check("... a real 16 kHz WAV holding the voice (>= 1 s)", body[:4] == b"RIFF" and len(body) >= 32000 + 44, len(body))
    check("the kitchen hears 'Sent.'", kitchen.client.announced[-1]["text"] == "Sent." and not kitchen.client.announced[-1]["start"],
          kitchen.client.announced[-1])
    check("the bedroom remembers who intercommed it", bedroom.intercom_from == "kitchen-echo", bedroom.intercom_from)

    r = hub.intercom_cmd("reply", satellite="bedroom-echo", room="bedroom", msg="on my way")
    await asyncio.sleep(0.05)
    check("reply X goes back to the kitchen in her voice", r["say"] == "Sent to the kitchen." and
          kitchen.client.announced[-1]["text"] == "From the bedroom: on my way", (r, kitchen.client.announced[-1]))
    garage = Satellite(hub, {"name": "garage-echo", "ip": "127.0.0.1", "room": "garage"})
    garage.client, garage.connected = FakeClient(), True
    hub.sats.append(garage)
    r = hub.intercom_cmd("reply", satellite="garage-echo", room="garage", msg="ok")
    check("reply with nobody to reply to", not r["ok"] and "Nobody" in r["say"], r)

    # too short a recording (another room woke a moment ago: a recording is never deduped away as "another room")
    hub._last_wake = (__import__("time").time(), kitchen)
    r = hub.intercom_cmd("intercom", satellite="bedroom-echo", room="bedroom", target="kitchen")
    await hub._open_intercom(bedroom, Turn("c3"), "intercom the kitchen", r)
    await asyncio.sleep(0.05)                               # ("Go ahead." plays before anyone talks)
    await bedroom.handle_start("c4", 0, None, "")
    await bedroom.handle_audio(b"\0" * 96000)             # 3 s of silence (ran to the cutoff, nobody spoke)
    n_k = len(kitchen.client.announced)
    await bedroom.handle_stop(False)
    await asyncio.sleep(0.05)
    check("a recording with NO SPEECH (however long) isn't sent; she says so",
          "didn't catch" in bedroom.client.announced[-1]["text"] and len(kitchen.client.announced) == n_k, bedroom.client.announced[-1])

    # review findings 10-15 (async)
    # (1) "Go ahead." cut off / failed -> the arm is cleared, so the next real command is NOT shipped as a recording
    bedroom.client.fail_next = True
    r = hub.intercom_cmd("intercom", satellite="bedroom-echo", room="bedroom", target="kitchen")
    await hub._open_intercom(bedroom, Turn("c6"), "intercom the kitchen", r)
    await asyncio.sleep(0.1)
    check("'Go ahead' that didn't finish disarms the intercom and says so", bedroom.intercom_to is None
          and bedroom.intercom_until == 0.0 and "couldn't open the intercom" in bedroom.client.announced[-1]["text"],
          (bedroom.intercom_to, bedroom.client.announced[-2:]))
    hub._last_wake = (0.0, None)                            # (the other room's test turns were < 1.2 s ago)
    await bedroom.handle_start("c6b", 0, None, "alexa")
    check("... and the next wake turn there is a normal turn", bedroom.turn is not None and bedroom.turn.intercom is None,
          bedroom.turn)
    await bedroom.handle_stop(True)
    # (1b) a successful "Go ahead" keeps the arm, shortened to the moment it finished
    r = hub.intercom_cmd("intercom", satellite="bedroom-echo", room="bedroom", target="kitchen")
    await hub._open_intercom(bedroom, Turn("c6c"), "intercom the kitchen", r)
    await asyncio.sleep(0.1)
    import time as _t
    check("a played 'Go ahead' keeps the arm with a short window", bedroom.intercom_to is kitchen and 0 < bedroom.intercom_until - _t.time() <= 4.2,
          (bedroom.intercom_to, bedroom.intercom_until - _t.time()))
    bedroom.intercom_to, bedroom.intercom_until = None, 0.0
    # (5) echod's routine Stop after our RUN_END must not cancel a delivery in flight / double RUN_END
    r = hub.intercom_cmd("intercom", satellite="kitchen-echo", room="kitchen", target="bedroom")
    await hub._open_intercom(kitchen, Turn("c7"), "intercom the bedroom", r)
    await asyncio.sleep(0.05)
    await kitchen.handle_start("c8", 0, None, "")
    await kitchen.handle_audio(b"\x10\x00" * 16000)
    kitchen.client.events.clear()
    n_b = len(bedroom.client.announced)
    task = asyncio.create_task(kitchen.handle_stop(False))
    await asyncio.sleep(0)                                  # delivery underway
    await kitchen.handle_stop(True)                         # echod going idle after RUN_END
    await task
    await asyncio.sleep(0.05)
    check("echod's post-RUN_END Stop doesn't cancel the delivery (one RUN_END, message delivered, 'Sent.')",
          len(bedroom.client.announced) == n_b + 1 and [e[0] for e in kitchen.client.events].count("RUN_END") == 1
          and kitchen.client.announced[-1]["text"] == "Sent.", (kitchen.client.events, kitchen.client.announced[-1]))
    # (6) the lead-in's TTS failing must not lose the message
    real_synth = hub.synth
    hub.synth = lambda t: (_ for _ in ()).throw(RuntimeError("tts down")) if t.startswith("Intercom from") else WAV
    r = hub.intercom_cmd("intercom", satellite="kitchen-echo", room="kitchen", target="bedroom")
    await hub._open_intercom(kitchen, Turn("c9"), "intercom the bedroom", r)
    await asyncio.sleep(0.05)
    await kitchen.handle_start("c10", 0, None, "")
    await kitchen.handle_audio(b"\x10\x00" * 16000)
    n_b = len(bedroom.client.announced)
    await kitchen.handle_stop(False)
    await asyncio.sleep(0.05)
    check("lead-in TTS failure: the voice still goes out, 'Sent.' still said", len(bedroom.client.announced) == n_b + 1
          and kitchen.client.announced[-1]["text"] == "Sent.", (len(bedroom.client.announced) - n_b, kitchen.client.announced[-1]))
    hub.synth = real_synth

    # the window expires: a later wake is a normal turn, never a recording
    kitchen.intercom_to, kitchen.intercom_until = bedroom, 0.0
    hub._last_wake = (0.0, None)                            # (not "another room answering": the test moves fast)
    await kitchen.handle_start("c5", 0, None, "alexa")
    check("an expired intercom window gives a normal turn", kitchen.turn is not None and kitchen.turn.intercom is None)
    await kitchen.handle_stop(True)
    replies.stop() if hasattr(replies, "stop") else None


router_tests()
asyncio.run(hub_tests())
print(f"\n{len(fails)} failure(s)" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
