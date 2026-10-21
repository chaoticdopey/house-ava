"""Satellite hub checks against a FAKE Echo (no device, no network beyond localhost, no brain/whisper/edge-tts).

    .venv\\Scripts\\python.exe tests\\test_satellite.py
"""
import asyncio
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile  # noqa: E402

os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="houseava-test-")   # never the real conversation log / memory
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from aioesphomeapi import VoiceAssistantEventType as E  # noqa: E402

from houseava.http_server import ReplyServer  # noqa: E402
from houseava.satellite import Satellite, SatelliteHub  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


class Cfg:
    def __init__(self):
        self.d = {"http.host": "127.0.0.1", "http.port": 0, "http.public_host": "127.0.0.1"}

    def get(self, k, d=None):
        return self.d.get(k, d)


class FakeClient:
    def __init__(self):
        self.events = []

    def send_voice_assistant_event(self, etype, data):
        self.events.append((E(etype).name.replace("VOICE_ASSISTANT_", ""), dict(data or {})))


class FakeRouter:
    def __init__(self):
        self.calls = []

    def handle(self, text, room=""):
        self.calls.append((text, room))
        return {"ok": True, "say": f"okay, {text} in the {room or 'house'}"}


WAV = b"RIFF" + b"\0" * 40


def make_hub(transcript="turn the light off"):
    router = FakeRouter()
    replies = ReplyServer(Cfg()).start()
    hub = SatelliteHub(Cfg(), router=router, transcribe=lambda pcm: transcript if pcm else "",
                       synth=lambda text: WAV, replies=replies)
    return hub, router, replies


async def one_turn(sat, audio=b"\x01\x00" * 1600):
    port = await sat.handle_start("conv-1", 0, None, "hey ava")
    await sat.handle_audio(audio)
    await sat.handle_stop(False)
    return port


async def main():
    # a normal turn, room-aware
    hub, router, replies = make_hub()
    kitchen = Satellite(hub, {"name": "kitchen-echo", "ip": "127.0.0.1", "room": "kitchen"})
    kitchen.client = FakeClient()
    hub.sats = [kitchen]
    port = await one_turn(kitchen)
    names = [e[0] for e in kitchen.client.events]
    check("handle_start returns 0 (audio over the API), never None", port == 0, port)
    check("event order RUN_START STT_START STT_END INTENT_START INTENT_END TTS_START TTS_END RUN_END",
          names == ["RUN_START", "STT_START", "STT_END", "INTENT_START", "INTENT_END", "TTS_START", "TTS_END", "RUN_END"],
          names)
    ev = dict(kitchen.client.events)
    check("STT_END carries the words heard", ev.get("STT_END", {}).get("text") == "turn the light off", ev)
    check("the brain gets THIS Echo's room (room-aware by default)", router.calls == [("turn the light off", "kitchen")],
          router.calls)
    check("INTENT_END carries the conversation id", ev.get("INTENT_END", {}).get("conversation_id") == "conv-1", ev)
    check("TTS_START carries the reply text", "in the kitchen" in ev.get("TTS_START", {}).get("text", ""), ev)
    url = ev.get("TTS_END", {}).get("url", "")
    check("TTS_END carries a fetchable reply URL", url.startswith("http://127.0.0.1:") and url.endswith(".wav"), url)
    try:
        body = urllib.request.urlopen(url, timeout=5).read()
    except Exception as exc:
        body = repr(exc).encode()
    check("the device can download the reply WAV from that URL", body == WAV, body[:40])
    try:
        urllib.request.urlopen(url.rsplit("/", 1)[0] + "/nope.wav", timeout=5).read()
        bad = False
    except Exception:
        bad = True
    check("unknown reply ids are 404 (nothing else is served)", bad)

    # nothing heard
    hub2, router2, _ = make_hub(transcript="")
    s2 = Satellite(hub2, {"name": "bedroom-echo", "ip": "127.0.0.1", "room": "bedroom"})
    s2.client = FakeClient()
    await one_turn(s2)
    names2 = [e[0] for e in s2.client.events]
    # (no ERROR: on echod that's the failure chime + red ring - wrong for "nobody said anything", which is what a
    # false wake is; RUN_END alone puts it straight back to idle)
    check("silence -> RUN_END alone (no error chime), brain never called",
          names2 == ["RUN_START", "STT_START", "RUN_END"] and not router2.calls, s2.client.events)

    # an aborted turn
    hub3, router3, _ = make_hub()
    s3 = Satellite(hub3, {"name": "x", "ip": "127.0.0.1", "room": "bedroom"})
    s3.client = FakeClient()
    await s3.handle_start("c", 0, None, None)
    await s3.handle_audio(b"\x01\x00" * 100)
    await s3.handle_stop(True)
    check("abort -> RUN_END, no brain call", [e[0] for e in s3.client.events][-1] == "RUN_END" and not router3.calls,
          s3.client.events)

    # two rooms hear the same wake word: the first answers, the other is told it's a duplicate
    hub4, router4, _ = make_hub()
    a = Satellite(hub4, {"name": "a", "ip": "127.0.0.1", "room": "kitchen"})
    b = Satellite(hub4, {"name": "b", "ip": "127.0.0.1", "room": "living room"})
    a.client, b.client = FakeClient(), FakeClient()
    pa = await a.handle_start("c1", 0, None, "hey ava")
    pb = await b.handle_start("c2", 0, None, "hey ava")
    await b.handle_audio(b"\x01\x00" * 100)
    await b.handle_stop(False)
    await a.handle_audio(b"\x01\x00" * 100)
    await a.handle_stop(False)
    check("both still return 0", pa == 0 and pb == 0)
    check("the second room gets ERROR duplicate_wake_up_detected",
          b.client.events and b.client.events[0][0] == "ERROR"
          and b.client.events[0][1].get("code") == "duplicate_wake_up_detected", b.client.events)
    check("only the first room's request reaches the brain", router4.calls == [("turn the light off", "kitchen")],
          router4.calls)

    # a brain crash still ends the turn cleanly with a spoken apology
    hub5, _, _ = make_hub()
    hub5.router = type("Boom", (), {"handle": lambda self, t, r="": (_ for _ in ()).throw(RuntimeError("x"))})()
    s5 = Satellite(hub5, {"name": "y", "ip": "127.0.0.1", "room": "office"})
    s5.client = FakeClient()
    await one_turn(s5)
    ev5 = dict(s5.client.events)
    check("brain error -> she says sorry and the turn still ends", "Sorry" in ev5.get("TTS_START", {}).get("text", "")
          and s5.client.events[-1][0] == "RUN_END", s5.client.events)

    # 10-20: a command done quietly (intruder alert / back to normal) ends the turn with no reply at all
    hub6, _, _ = make_hub(transcript="intruder alert")
    spoken = []
    hub6.synth = lambda text: spoken.append(text) or WAV
    hub6.router = type("Quiet", (), {"handle": lambda self, t, r="": {"ok": True, "silent": True, "say": "Alert on."}})()
    s6 = Satellite(hub6, {"name": "z", "ip": "127.0.0.1", "room": "kitchen"})
    s6.client = FakeClient()
    await one_turn(s6)
    names6 = [e[0] for e in s6.client.events]
    check("a silent command: STT_END then RUN_END, no reply audio", names6 == ["RUN_START", "STT_START", "STT_END", "RUN_END"]
          and not spoken, (names6, spoken))
    check("  ...still in the conversation log", any("intruder alert" in str(x) for x in hub6.convlog.recent()), "")

    # 10-20: a volume button press during the intruder alert reaches the router (on a thread); nothing when no alert
    pressed = []

    class AlertRouter:
        _alert = {"satellite": "z"}

        def alert_button(self, room, sat):
            pressed.append((room, sat))
            return {"ok": True, "silent": True, "say": "off"}
    hub6.router = AlertRouter()
    hub6.alert_button(s6)
    for _ in range(50):
        if pressed:
            break
        await asyncio.sleep(0.02)
    check("volume button during an alert -> the router stops the music", pressed == [("kitchen", "z")], pressed)
    AlertRouter._alert = None
    pressed.clear()
    hub6.alert_button(s6)
    await asyncio.sleep(0.1)
    check("volume button with no alert on -> nothing", not pressed, pressed)

    replies.stop()


asyncio.run(main())

# a config file that doesn't read (a typo in a hand edit) is never written over, and the command line says why
import contextlib  # noqa: E402
import io  # noqa: E402

from houseava import satellite as satmod  # noqa: E402
from houseava.config import Config  # noqa: E402

cdir = tempfile.mkdtemp(prefix="houseava-cfg-")
cpath = os.path.join(cdir, "houseava_config.json")
mine = '{"satellites": [{"name": "Kitchen Echo", "ip": "kitchen-echo.local", "room": "kitchen", "psk": "x"}],\n'
with open(cpath, "w", encoding="utf-8") as f:
    f.write(mine + ' "lights": {"bulbs": {"kitchen 1": "192.0.2.101"}},}')      # (the trailing comma)
err = io.StringIO()
with contextlib.redirect_stderr(err):
    bad = Config(path=cpath)
check("config: a file that doesn't read is flagged, with the reason", not bad.load_ok and bad.load_error, bad.load_error)
with contextlib.redirect_stderr(err):
    saved = bad.save()
kept = os.path.join(cdir, "houseava_config.unreadable.json")
check("config: saving over it moves it aside first (nothing lost)", saved and os.path.exists(kept)
      and open(kept, encoding="utf-8").read().startswith(mine), os.listdir(cdir))
check("config: ... and the new file reads", Config(path=cpath).load_ok)
with open(cpath, "w", encoding="utf-8") as f:
    f.write("{nope")
os.environ["HOUSEAVA_DIR"] = cdir
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    rc = satmod.main(["add", "--name", "B", "--ip", "b.local", "--room", "bedroom"])
check("config: 'add' on a file that doesn't read stops with the reason instead of writing over it",
      rc == 2 and "isn't valid JSON" in err.getvalue() and open(cpath, encoding="utf-8").read() == "{nope",
      (rc, err.getvalue()[-200:]))
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    rc = satmod.main(["list"])
check("config: 'list' too, with nothing on stdout (the installer reads that)", rc == 2 and not out.getvalue(), out.getvalue())

# the reply/face/song address for an Echo added by a name this box can't look up: the main address, not 127.0.0.1
import socket  # noqa: E402

rs = ReplyServer(Cfg())
rs.public_host = ""
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _s:
        _s.connect(("10.255.255.255", 9))
        main_ip = _s.getsockname()[0]
except OSError:
    main_ip = None                     # (a box with no network at all: nothing to compare)
got_ip = rs.host_for("no-such-echo-house-ava-test.local")
check("reply address: an unresolvable Echo name gets this box's main address, not 127.0.0.1",
      main_ip is None or (got_ip == main_ip and got_ip != "127.0.0.1"), (got_ip, main_ip))

# the Echo's clock follows the server's own time zone when the config leaves it blank
from houseava.satellite import server_tz_rule  # noqa: E402

tzdir = tempfile.mkdtemp(prefix="houseava-tz-")
tzif = os.path.join(tzdir, "localtime")
with open(tzif, "wb") as f:
    f.write(b"TZif2" + b"\x00" * 40 + b"\nTZif2" + b"\x00" * 40 + b"\nEST5EDT,M3.2.0,M11.1.0\n")
check("time zone: the rule at the end of the server's zone file", server_tz_rule(tzif) == "EST5EDT,M3.2.0,M11.1.0",
      server_tz_rule(tzif))
with open(tzif, "wb") as f:
    f.write(b"not a zone file\nEST5EDT\n")
check("time zone: not a zone file = UTC0", server_tz_rule(tzif) == "UTC0")
check("time zone: no zone file = UTC0", server_tz_rule(os.path.join(tzdir, "nope")) == "UTC0")
with open(tzif, "wb") as f:
    f.write(b"TZif2" + b"\x00" * 40 + b"\n<+0530>-5:30\n")
check("time zone: a <+0530> style rule is kept", server_tz_rule(tzif) == "<+0530>-5:30", server_tz_rule(tzif))
print(f"\n==== RESULT: {'ALL PASSED' if not fails else 'FAILURES: ' + str(fails)} ====")
sys.exit(1 if fails else 0)
