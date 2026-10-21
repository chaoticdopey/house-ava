"""A phone playing its own music through an Echo: the voice commands, pairing mode on the Echo, and House Ava's own
music not jumping back in after the phone's music ends. Fake Echo, no network beyond localhost.

    .venv\\Scripts\\python.exe tests\\test_bluetooth.py
"""
import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="houseava-test-")   # never the real conversation log / memory
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from aioesphomeapi import NumberInfo  # noqa: E402

from houseava.brain import Router  # noqa: E402
from houseava.config import Config  # noqa: E402
from houseava.http_server import ReplyServer  # noqa: E402
from houseava.music import MusicServer, SpeakerPlayer  # noqa: E402
from houseava.satellite import Satellite, SatelliteHub  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


# ---- the voice commands ------------------------------------------------------------------------------------------
class FakeLights:
    def rooms(self):
        return {"living room": ["living room 1"], "bedroom": ["bedroom 1"]}

    def set_state(self, which, on, room=""):
        return {"ok": True, "say": f"lights {which} {on}"}


class FakePlugs:
    devices = {"porch": "Porch Light"}


class NoBrain:
    calls = 0

    def chat_json(self, *a, **k):
        NoBrain.calls += 1
        return {"action": "answer", "say": "brain"}

    def chat(self, *a, **k):
        return "brain"


class FakeMem:
    def __init__(self):
        self.turns, self.facts = [], []

    def add_turn(self, *a):
        self.turns.append(a)
        return a

    def drop_turn(self, entry):
        return False

    def recent(self, *a, **k):
        return []


cfg = Config(path=os.path.join(os.path.dirname(__file__), "__no_such_config__.json"))
r = Router(cfg, NoBrain(), FakeLights(), FakePlugs(), FakeMem(), lambda: "persona")
got = []
r.bluetooth = lambda action, satellite="", room="", named=False: got.append((action, satellite, room, named)) or {
    "ok": True, "say": f"bt {action}"}

HERE = ("kitchen-echo", "kitchen", False)
CASES = [
    ("pair my phone", ("pair",) + HERE),
    ("Pair.", ("pair",) + HERE),
    ("Alexa, turn on Bluetooth.", ("pair",) + HERE),
    ("turn the bluetooth on", ("pair",) + HERE),
    ("connect her phone to the bluetooth", ("connect",) + HERE),
    ("bluetooth pairing mode", ("pair",) + HERE),
    ("can you hook up my iphone", ("pair",) + HERE),
    ("Start pairing.", ("pair",) + HERE),
    ("Put it in pairing mode, please.", ("pair",) + HERE),
    ("Let me play music from my phone.", ("pair",) + HERE),
    ("pair my phone to the bluetooth", ("pair",) + HERE),
    ("pair my phone in the bedroom", ("pair", "", "bedroom", True)),
    ("Pair my phone in the bedroom, please.", ("pair", "", "bedroom", True)),
    ("Turn on the Bluetooth in the bedroom.", ("pair", "", "bedroom", True)),
    ("connect my phone to the bedroom echo", ("connect", "", "bedroom", True)),
    ("stop bluetooth pairing", ("stop",) + HERE),
    ("cancel pairing", ("stop",) + HERE),
    ("Turn pairing off.", ("stop",) + HERE),
    ("Stop Bluetooth.", ("stop",) + HERE),
    ("turn off bluetooth", ("disconnect",) + HERE),
    ("disconnect my phone", ("disconnect",) + HERE),
]
for said, want in CASES:
    got.clear()
    res = r.handle(said, "kitchen", satellite="kitchen-echo", heard="wake")
    check(f"voice: {said!r} -> {want[0]}{' in ' + want[2] if want[3] else ''}",
          got == [want] and res.get("say") == f"bt {want[0]}", (got, res))

# questions, sentences about phones, TV ads: never pairing mode (it lets ANY nearby phone pair for 3 minutes)
for said in ("what's connected to bluetooth", "where's my phone", "I'm on the phone", "turn on the living room lights",
             "the phone disconnected again", "Is Bluetooth on?", "How do I connect my phone to the TV?",
             "How do I connect my iPhone to my car?", "my bluetooth headphones died on me",
             "The Bluetooth on my phone keeps disconnecting.", "My phone's Bluetooth is on.",
             "Can you send the link to my phone?", "I'll link it to my phone later.", "Hook up the phone charger.",
             "I need to connect with the phone company", "connect your phone and save big today"):
    got.clear()
    r.handle(said, "kitchen", satellite="kitchen-echo", heard="wake")
    check(f"voice: {said!r} is NOT a pairing command", got == [], got)
got.clear()
r.handle("pair my phone", "kitchen", satellite="kitchen-echo", heard="follow_up")
check("voice: no pairing from a follow-up (the mic only stayed open; needs the wake word)", got == [], got)
r2 = Router(cfg, NoBrain(), FakeLights(), FakePlugs(), FakeMem(), lambda: "persona")
res = r2.handle("pair my phone", "")
check("voice: no Echo hub (the CLI) -> says so, no crash", not res.get("ok") and "Echo" in res.get("say", ""), res)


# ---- the hub: pairing mode on the right Echo ------------------------------------------------------------------------
class Cfg:
    def __init__(self, **kw):
        self.d = {"http.host": "127.0.0.1", "http.port": 0, "http.public_host": "127.0.0.1", **kw}

    def get(self, k, d=None):
        return self.d.get(k, d)


class FakeClient:
    def __init__(self):
        self.switches = []
        self.on_thread = []

    def switch_command(self, key, state, device_id=0):
        self.switches.append((key, state))
        self.on_thread.append(threading.current_thread().name)


class FakePlayer:
    def __init__(self, loaded):
        self.loaded, self.stops = loaded, 0

    def has_music(self):
        return self.loaded

    def stop(self):
        self.stops += 1
        self.loaded = False
        return True


class FakeMusic:
    def __init__(self, players):
        self.players = players


async def hub_checks():
    replies = ReplyServer(Cfg()).start()
    hub = SatelliteHub(Cfg(), router=type("R", (), {"handle": lambda self, t, r="": {"say": "x"}})(), transcribe=lambda b: "",
                       synth=lambda t: b"", replies=replies)
    hub._loop = asyncio.get_running_loop()
    k = Satellite(hub, {"name": "kitchen-echo", "ip": "127.0.0.1", "room": "kitchen"})
    k.client, k.connected, k.bt_pair_key, k.bt_name = FakeClient(), True, 77, "echo-show"; k.bt_phone_ok = True
    hub.sats = [k]
    player = FakePlayer(loaded=True)
    hub.music = FakeMusic({"kitchen-echo": player})

    res = await asyncio.to_thread(hub.bluetooth_cmd, "pair", "kitchen-echo", "kitchen")   # (the router's worker thread)
    await asyncio.sleep(0.05)
    check("hub: pair -> pairing switch ON, sent on the hub's loop (not the worker thread)",
          k.client.switches == [(77, True)] and k.client.on_thread == [threading.current_thread().name],
          (k.client.switches, k.client.on_thread))
    check("hub: ... tells her what to pick on the phone ('echo show', no hyphen)", "pick echo show" in res["say"], res)
    check("hub: ... and stops House Ava's own music on that Echo first", player.stops == 1)
    res = await asyncio.to_thread(hub.bluetooth_cmd, "pair", "kitchen-echo", "kitchen")
    check("hub: ... (no music of ours on -> nothing stopped)", player.stops == 1)
    k.client.switches.clear()
    res = hub.bluetooth_cmd("stop", "kitchen-echo", "kitchen")
    check("hub: stop -> pairing switch OFF", k.client.switches == [(77, False)] and res["ok"], (k.client.switches, res))
    k.client.switches.clear()
    res = hub.bluetooth_cmd("disconnect", "kitchen-echo")
    check("hub: disconnect -> pairing off + explains a playing phone is let go from the phone",
          k.client.switches == [(77, False)] and "on the phone" in res["say"], res)
    res = hub.bluetooth_cmd("pair", "", "console")
    check("hub: from the app/console with one Echo -> that Echo", res["ok"], res)
    res = hub.bluetooth_cmd("pair", room="bedroom", named=True)
    check("hub: a named room with no Echo -> says so (never pairs the kitchen instead)",
          not res["ok"] and "no Echo in the bedroom" in res["say"], res)
    k.connected = False
    res = hub.bluetooth_cmd("pair", "kitchen-echo")
    check("hub: Echo offline -> can't reach it", not res["ok"] and "can't reach" in res["say"], res)
    k.connected, k.bt_pair_key = True, None
    res = hub.bluetooth_cmd("pair", "kitchen-echo")
    check("hub: an Echo without Bluetooth -> says so", not res["ok"] and "Bluetooth" in res["say"], res)
    replies.stop()


asyncio.run(hub_checks())


# ---- the song server: did the Echo hang up partway? -----------------------------------------------------------------
root = tempfile.mkdtemp(prefix="houseava-music-")
song = os.path.join(root, "song.mp3")
with open(song, "wb") as f:
    f.write(os.urandom(1024 * 1024) * 24)                    # 24 MB: far more than any socket buffers hold
srv = MusicServer(root, host="127.0.0.1", port=0).start()

url = srv.register(song)
tok = srv.token_of(url)
check("server: token_of finds the token in a song URL", tok and tok in url and srv.token_of("http://x/y") == "", url)
with urllib.request.urlopen(url, timeout=20) as resp:
    n = len(resp.read())
check("server: a song read to its end -> not hung up", n == 24 * 1024 * 1024 and not srv.hung_up(tok), n)

url2 = srv.register(song)
tok2 = srv.token_of(url2)
host, port = "127.0.0.1", srv.port
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
s.connect((host, port))
s.sendall(f"GET /m/{tok2}.mp3 HTTP/1.1\r\nHost: x\r\n\r\n".encode())
got_bytes = 0
while got_bytes < 256 * 1024:
    got_bytes += len(s.recv(65536))
s.close()                                                     # the Echo let go of it (a phone took the speaker)
for _ in range(100):
    if srv.hung_up(tok2):
        break
    time.sleep(0.05)
check("server: the Echo hanging up partway -> hung up", srv.hung_up(tok2))
with urllib.request.urlopen(urllib.request.Request(url2, headers={"Range": "bytes=1000-"}), timeout=20) as resp:
    resp.read()
check("server: fetched again (a resume) -> not hung up any more", not srv.hung_up(tok2))
srv.stop()


# ---- the player: no "next song" over the phone's music ending -------------------------------------------------------
class FakeSpeaker:
    def __init__(self):
        self.urls, self.cmds = [], []

    def play_url(self, u):
        self.urls.append(u)
        return True

    def command(self, c):
        self.cmds.append(c)
        return True

    def has_media(self):
        return True


class FakeServer:
    def __init__(self, hung_for):
        self.hung_for, self.n = hung_for, 0                   # seconds since the Echo let go of the song (0 = it didn't)

    def register(self, path, reach_ip=""):
        self.n += 1
        return f"http://h:1/m/{'t' * 20}{self.n}.mp3"

    token_of = staticmethod(MusicServer.token_of)

    def hung_up_for(self, token):
        return self.hung_for


def run_player(hung_for, idle=True):
    sp = FakeSpeaker()
    p = SpeakerPlayer("kitchen-echo", "kitchen", sp, FakeServer(hung_for))
    p.queue = ["/m/a.mp3", "/m/b.mp3", "/m/c.mp3"]
    with p._lock:
        p._start(0)
    p.on_state("playing")
    p._sent -= 60                                             # (a minute in: well past "it never played")
    if idle:
        p.on_state("idle")
        time.sleep(0.3)
    return p, sp


p, sp = run_player(hung_for=0)
check("player: our song ends normally -> the next one", p.index == 1 and len(sp.urls) == 2, (p.index, sp.urls))
p, sp = run_player(hung_for=8.0)
check("player: a phone took the speaker (Echo let go of our song 8 s before going IDLE) -> no next song; resume can "
      "bring it back", p.index == 0 and len(sp.urls) == 1 and not p.loaded and p.stopped,
      (p.index, sp.urls, p.loaded, p.stopped))
p, sp = run_player(hung_for=0.05)
check("player: echod gave up on a damaged song (let go just as it went IDLE) -> still on to the next one",
      p.index == 1 and len(sp.urls) == 2, (p.index, sp.urls))
p, sp = run_player(hung_for=8.0, idle=False)                  # (the phone is still playing)
res = p.next()
check("player: 'next' while a phone has the speaker -> doesn't start our song over hers",
      not res["ok"] and len(sp.urls) == 1 and not p.has_music(), (res, sp.urls))
check("player: ... and 'what's playing' doesn't name our old song", p.current_file() is None)
p, sp = run_player(hung_for=8.0, idle=False)
p.has_music()                                                 # (settles: ours isn't on)
check("player: 'stop' with only the phone playing still stops it (the Echo's stop)", p.stop() and sp.cmds == ["stop"],
      sp.cmds)
sp.cmds.clear()
check("player: ... and 'pause' pauses it", p.pause() and sp.cmds == ["pause"], sp.cmds)
p.device = "idle"
sp.cmds.clear()
check("player: nothing playing at all -> stop/pause do nothing", not p.stop() and not p.pause() and not sp.cmds, sp.cmds)

# ---- the microphone: "turn your mic up" --------------------------------------------------------------------------
mic_got = []
r.microphone = lambda action, value=None: mic_got.append((action, value)) or {"ok": True, "say": f"mic {action}"}
MIC_CASES = [
    ("turn your mic up", ("up", None)), ("Alexa, turn your microphone up a bit.", ("up", None)),
    ("listen harder", ("up", None)), ("you can't hear me", ("up", None)), ("turn up your ears", ("up", None)),
    ("be more sensitive", ("up", None)), ("mic up", ("up", None)),
    ("turn your mic down", ("down", None)), ("you're too sensitive", ("down", None)), ("lower your mic", ("down", None)),
    ("set your mic to 40", ("set", "40")), ("your microphone gain to 25", ("set", "25")),
    ("what's your mic at", ("what", None)), ("how's your hearing", ("what", None)),
]
for said, want in MIC_CASES:
    mic_got.clear()
    res = r.handle(said, "kitchen", satellite="kitchen-echo", heard="wake")
    check(f"mic voice: {said!r} -> {want}", mic_got == [want] and res.get("say") == f"mic {want[0]}", (mic_got, res))
for said in ("I can't hear the TV", "the mic on my phone is broken", "listen to some jazz", "turn up the volume",
             "is your mic on", "my ears hurt", "she's too sensitive about it"):
    mic_got.clear()
    r.handle(said, "kitchen", satellite="kitchen-echo", heard="wake")
    check(f"mic voice: {said!r} is NOT a mic command", mic_got == [], mic_got)
mic_got.clear()
r.handle("turn your mic up", "kitchen", satellite="kitchen-echo", heard="follow_up")
check("mic voice: never from a follow-up", mic_got == [])


async def mic_hub_checks():
    replies = ReplyServer(Cfg()).start()
    cfg = Cfg(**{"voice.mic_gain": 30})
    saved = []
    cfg.set = lambda k, v: cfg.d.__setitem__(k, v)
    cfg.save = lambda: saved.append(dict(cfg.d))
    hub = SatelliteHub(cfg, router=type("R", (), {"handle": lambda self, t, r="": {"say": "x"}})(), transcribe=lambda b: "",
                       synth=lambda t: b"", replies=replies)
    hub._loop = asyncio.get_running_loop()
    k = Satellite(hub, {"name": "kitchen-echo", "ip": "127.0.0.1", "room": "kitchen"})
    k.client, k.connected = FakeClient(), True
    k.client.number_command = lambda key, v, device_id=0: k.client.switches.append(("num", key, v))
    k.ents = [NumberInfo(object_id="microphone_gain", key=9, min_value=0.0, max_value=59.0, step=1.0)]
    hub.sats = [k]
    res = hub.mic_cmd("what")
    check("mic hub: 'what' reports the gain", "30 dB" in res["say"], res)
    res = hub.mic_cmd("up")
    await asyncio.sleep(0.05)
    check("mic hub: 'up' -> 36 dB, saved, and pushed to the Echo live",
          cfg.d["voice.mic_gain"] == 36 and saved and ("num", 9, 36.0) in k.client.switches and "36 dB" in res["say"],
          (cfg.d.get("voice.mic_gain"), k.client.switches, res))
    res = hub.mic_cmd("set", "70")
    check("mic hub: 'set 70' clamps to 59 and says it's the max", cfg.d["voice.mic_gain"] == 59 and "max" in res["say"], res)
    res = hub.mic_cmd("up")
    check("mic hub: 'up' at the top -> says so, no change", cfg.d["voice.mic_gain"] == 59 and "high as it goes" in res["say"], res)
    res = hub.mic_cmd("set", "lots")
    check("mic hub: a non-number -> asks for one", not res["ok"], res)
    replies.stop()


asyncio.run(mic_hub_checks())

print(f"\n==== RESULT: {'ALL PASSED' if not fails else 'FAILURES: ' + str(fails)} ====")
sys.exit(1 if fails else 0)
