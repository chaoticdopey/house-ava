"""House Ava music checks: the vendored library, the song server, one player per Echo, the router's music path and the
hub's media_player wiring — against FAKE Echoes (no device, no NAS, nothing but localhost).

    .venv\\Scripts\\python.exe tests\\test_music.py

A SYNTHETIC library: a temp folder of tiny real MP3s / FLACs tagged with mutagen (plus a .wma the Echo can't play and
a file outside the library). The index, playlists and sessions live in the temp folder; nothing on Z: is touched.
"""
import asyncio
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)

TMP = Path(tempfile.mkdtemp(prefix="houseava_music_"))
os.environ["HOUSEAVA_DIR"] = str(TMP / "home")          # (nothing lands in ~/.houseava)
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from aioesphomeapi import MediaPlayerEntityState, MediaPlayerInfo, MediaPlayerState, SwitchInfo  # noqa: E402

import houseava.music as HM  # noqa: E402
import houseava.web as W  # noqa: E402
from houseava.brain import Router  # noqa: E402
from houseava.config import Config  # noqa: E402
from houseava.http_server import ReplyServer  # noqa: E402
from houseava.llm import BrainError  # noqa: E402
from houseava.satellite import Satellite, SatelliteHub  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


# =============================================================== the synthetic library
LIB = TMP / "Music"
FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413            # one MPEG-1 Layer III frame, 128 kbps, 44.1 kHz


def _flac_head() -> bytes:
    info = struct.pack(">HH", 4096, 4096) + b"\0" * 6
    info += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + b"\0" * 16     # 44.1 kHz, 2 ch, 16 bit
    return b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + info


def song(rel, artist, album, title, genre="", date="", track=None, frames=12):
    from mutagen.easyid3 import EasyID3
    from mutagen.flac import FLAC

    p = LIB / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix == ".flac":
        p.write_bytes(_flac_head() + b"\xff\xf8" + b"\0" * 300)
        t = FLAC(str(p))
    else:
        p.write_bytes(FRAME * frames)
        t = EasyID3()
    for k, v in (("artist", artist), ("album", album), ("title", title), ("genre", genre), ("date", date),
                 ("tracknumber", track)):
        if v:
            t[k] = str(v)
    t.save(str(p)) if p.suffix != ".flac" else t.save()
    return str(p)


S = {}
S["lose"] = song(r"Eminem/8 Mile (2002)/01 - Eminem - Lose Yourself.mp3", "Eminem", "8 Mile", "Lose Yourself", "Rap", 2002, 1)
S["8mile"] = song(r"Eminem/8 Mile (2002)/02 - Eminem - 8 Mile.mp3", "Eminem", "8 Mile", "8 Mile", "Rap", 2002, 2)
S["stan"] = song(r"Eminem/The Marshall Mathers LP (2000)/01 - Eminem - Stan.mp3", "Eminem", "The Marshall Mathers LP",
                 "Stan", "Rap", 2000, 1)
S["slim"] = song(r"Eminem/The Marshall Mathers LP (2000)/02 - Eminem - The Real Slim Shady.mp3", "Eminem",
                 "The Marshall Mathers LP", "The Real Slim Shady", "Rap", 2000, 2)
S["mock"] = str(LIB / "Eminem" / "Encore (2004)" / "01 - Eminem - Mockingbird.wma")      # the Echo can't play wma
Path(S["mock"]).parent.mkdir(parents=True, exist_ok=True)
Path(S["mock"]).write_bytes(b"\x30\x26\xb2\x75" + b"\0" * 200)
S["tat"] = song(r"Cyndi Lauper/She's So Unusual (1983)/04 - Cyndi Lauper - Time After Time.mp3", "Cyndi Lauper",
                "She's So Unusual", "Time After Time", "Pop", 1983, 4)
S["storm"] = song(r"Etta James/At Last! (1960)/05 - Etta James - Stormy Weather.flac", "Etta James", "At Last!",
                  "Stormy Weather", "Jazz", 1960, 5)
S["rain"] = song(r"Prince/Purple Rain (1984)/09 - Prince - Purple Rain.mp3", "Prince", "Purple Rain", "Purple Rain",
                 "Rock", 1984, 9)
S["big"] = song(r"Prince/Purple Rain (1984)/01 - Prince - Let's Go Crazy.mp3", "Prince", "Purple Rain", "Let's Go Crazy",
                "Rock", 1984, 1, frames=4000)                                       # ~1.7 MB: a long download
LOGIC = [song(f"Logic/Everybody (2017)/{i:02d} - Logic - Track {i}.mp3", "Logic", "Everybody", f"Track {i}", "Hip-Hop",
              2017, i) for i in range(1, 8)]
# titles that are also words she hears ("stop", "pause", "turn on the news", "play thank you")
for k, (who, album, title) in {"dsmn": ("Queen", "Jazz", "Don't Stop Me Now"), "pause": ("The Pausers", "Breaks", "Pause"),
                               "news": ("Jack Johnson", "On and On", "The News"),
                               "dnd": ("Drake", "More Life", "Do Not Disturb"), "spr": ("The Lawns", "Grass", "Sprinklers"),
                               "ty": ("Dido", "No Angel", "Thank You"), "rn": ("Van Halen", "OU812", "Right Now")}.items():
    S[k] = song(f"{who}/{album}/01 - {who} - {title}.mp3", who, album, title, "Soul", 1999, 1)
OUTSIDE = TMP / "outside.mp3"
OUTSIDE.write_bytes(FRAME * 3)
(LIB / "notes.txt").write_text("not a song", encoding="utf-8")
(LIB / "folder.mp3").mkdir()                            # a directory with a song's name

# desktop Ava's file list, as it is on the PC (D:\Music\..., backslashes) — the seed
DESK = TMP / "desktop_music_index.json"
win = ["D:\\Music\\" + str(Path(p).relative_to(LIB)).replace("/", "\\") for p in [*S.values(), *LOGIC]]
DESK.write_text(json.dumps({"built": time.time(), "files": win + ["E:\\Elsewhere\\a.mp3", "D:\\Musical\\b.mp3"],
                            "artists": {"D:\\Music": sorted(os.listdir(LIB)), "E:\\Elsewhere": ["x"]}}), encoding="utf-8")


# =============================================================== fakes
class FakeSpeaker:
    """An Echo's Speaker control: records every call."""

    def __init__(self, media=True):
        self.calls = []
        self.media = media

    def has_media(self):
        return self.media

    def play_url(self, url):
        self.calls.append(("url", url))
        return self.media

    def command(self, name):
        self.calls.append(("cmd", name))
        return self.media

    def set_volume(self, v):
        self.calls.append(("vol", round(v, 2)))
        return self.media

    def urls(self):
        return [c[1] for c in self.calls if c[0] == "url"]

    def cmds(self):
        return [c[1] for c in self.calls if c[0] == "cmd"]


class FakeLights:
    def __init__(self):
        self.calls = []

    def rooms(self):
        return {"kitchen": ["k1"], "bedroom": ["b1"], "living room": ["l1"], "dining room": ["d1"]}

    def set_state(self, which, on, room=""):
        self.calls.append(("state", which, on, room))
        return {"ok": True, "say": "lights"}

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
        return {"ok": True, "say": "plug"}


class FakeBrain:
    def __init__(self, intent=None):
        self.intent = intent or {"action": "answer", "say": "the brain answered"}
        self.json_calls = 0

    def status(self):
        return False, "down"                 # (no mood labels / best-known songs: deterministic)

    def chat_json(self, system, user, temperature=None):
        self.json_calls += 1
        return self.intent

    def chat(self, system, user, temperature=None, history=None):
        return "the brain chatted"


class Mem:
    def add_turn(self, *a, **k):
        pass

    def recent(self, *a, **k):
        return []

    def recall(self, *a, **k):
        return []

    def remember(self, w):
        return {"ok": True, "say": "remembered"}

    def forget(self, w):
        return {"ok": True, "say": "forgotten"}


WEATHER = []
W.weather = lambda cfg, when="today", where="": (WEATHER.append(when) or {"ok": True, "say": "It's sunny."})


def make_cfg(data, port=0, seed=""):
    cfg = Config(path=TMP / "__no_config__.json")       # defaults only
    for k, v in (("music.enabled", True), ("music.desktop_root", "D:\\Music"),
                 ("music.root", str(LIB)), ("music.data_dir", str(data)), ("music.http_port", port),
                 ("music.seed_file", seed), ("music.index", False), ("http.host", "127.0.0.1"),
                 ("http.public_host", "127.0.0.1"),
                 # no audio-prefs on connect in these tests, so the command counts stay about music only
                 ("voice.echo_volume", None), ("voice.bass", None), ("voice.treble", None),
                 ("voice.follow_up", None), ("voice.stop_word", None), ("wake.enabled", False),
                 ("facecast.enabled", False)):
        cfg.set(k, v)
    return cfg


def fetch(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


# =============================================================== 1. the seed: desktop's file list moved onto the mount
check("seed: D:\\Music\\... -> /mnt/music/... with forward slashes",
      HM.rewrite_path("D:\\Music\\Eminem\\8 Mile (2002)\\01 - Lose Yourself.mp3", "D:\\Music", "/mnt/music")
      == "/mnt/music/Eminem/8 Mile (2002)/01 - Lose Yourself.mp3")
check("seed: the drive letter / case doesn't matter",
      HM.rewrite_path("d:\\music\\A\\b.flac", "D:\\Music", "/mnt/music/") == "/mnt/music/A/b.flac")
check("seed: paths outside desktop_root (or only sharing its prefix, or climbing out) are dropped",
      HM.rewrite_path("E:\\Other\\a.mp3", "D:\\Music", "/mnt/music") is None
      and HM.rewrite_path("D:\\Musical\\a.mp3", "D:\\Music", "/mnt/music") is None
      and HM.rewrite_path("D:\\Music\\..\\secret.mp3", "D:\\Music", "/mnt/music") is None)

DATA = TMP / "data"
cfg = make_cfg(DATA, seed=str(DESK))
svc = HM.MusicService(cfg, publish_host=lambda ip="": "127.0.0.1")
seeded = json.loads((DATA / "music_index.json").read_text(encoding="utf-8"))
want = sorted(os.path.normpath(p) for p in [*S.values(), *LOGIC])
check("seed: written into the music data folder, every desktop song moved onto music.root (others dropped)",
      svc.seeded == len(want) and sorted(seeded["files"]) == want, (svc.seeded, seeded["files"][:2]))
check("seed: the artist folders are keyed by music.root", list(seeded["artists"]) == [str(LIB)], seeded["artists"].keys())
check("seed: one time only (a second service leaves it alone)",
      HM.seed_index(DESK, DATA, "D:\\Music", str(LIB)) == 0)

from ava.tools.musiclib import index as IX  # noqa: E402  (the vendored copy, via houseava.music)

stop = threading.Event()
stop.set()
IX.scan([str(LIB)], stop=stop)                           # a pass stopped at once: only the seed goes in
c = IX.counts()
check("seed: the tag index's first pass seeds its rows from it (before any tag is read)",
      c["tracks"] == len(want) and c["tagged"] == 0, c)
IX.scan([str(LIB)])                                      # the whole pass: every tag read
c = IX.counts()
check("the tag index reads the synthetic library's tags", c["tagged"] >= len(want) - 1 and c["tracks"] >= len(want), c)

# =============================================================== 2. the song server
srv = svc.server
srv.start()
base = f"http://127.0.0.1:{srv.port}"
u = srv.register(S["lose"], "127.0.0.1")
st, hd, body = fetch(u)
check("server: a registered MP3 comes back byte for byte, audio/mpeg with its length",
      st == 200 and body == Path(S["lose"]).read_bytes() and hd.get("Content-Type") == "audio/mpeg"
      and int(hd.get("Content-Length", -1)) == len(body), (st, hd))
check("server: the URL carries no path, just /m/<token>.mp3", "/m/" in u and "Eminem" not in u and u.endswith(".mp3"), u)
uf = srv.register(S["storm"], "127.0.0.1")
st, hd, body = fetch(uf)
check("server: FLAC is served as audio/flac", st == 200 and hd.get("Content-Type") == "audio/flac"
      and body == Path(S["storm"]).read_bytes(), (st, hd))
st, hd, body = fetch(u, method="HEAD")
check("server: HEAD gives the headers and no body", st == 200 and body == b""
      and int(hd.get("Content-Length", -1)) == os.path.getsize(S["lose"]), (st, hd))
size = os.path.getsize(S["lose"])
st, hd, body = fetch(u, headers={"Range": "bytes=10-19"})
check("server: a byte range -> 206 with exactly those bytes",
      st == 206 and body == Path(S["lose"]).read_bytes()[10:20] and hd.get("Content-Range") == f"bytes 10-19/{size}",
      (st, hd))
st, hd, body = fetch(u, headers={"Range": "bytes=-7"})
check("server: a suffix range (the last 7 bytes)", st == 206 and body == Path(S["lose"]).read_bytes()[-7:], (st, hd))
st, hd, body = fetch(u, headers={"Range": f"bytes={size + 10}-"})
check("server: a range past the end -> 416", st == 416, st)
tok = u.rsplit("/", 1)[1].split(".")[0]
bad = [base + "/m/AAAAAAAAAAAAAAAAAAAAAAAA.mp3", f"{base}/m/{tok}.flac", base + "/", base + "/m/",
       base + "/m/../../etc/passwd", base + "/m/..%2f..%2foutside.mp3", base + "/Eminem/8%20Mile%20(2002)/",
       f"{base}/m/{tok}.mp3/../../outside.mp3", base + "/tts/" + tok + ".wav"]
codes = [fetch(x)[0] for x in bad]
check("server: unknown tokens, a wrong extension, traversal tries and directories are all 404", set(codes) == {404},
      list(zip(bad, codes)))
raw = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
raw.sendall(b"GET /../outside.mp3 HTTP/1.0\r\n\r\n")
check("server: a raw '..' request line is 404 too", raw.recv(64).split(b"\r\n")[0].endswith(b"404 Not Found"))
raw.close()
check("server: refuses a file outside music.root", srv.register(str(OUTSIDE)) is None)
check("server: refuses '..' that climbs out of music.root", srv.register(str(LIB / ".." / "outside.mp3")) is None)
check("server: refuses what the Echo can't play (wma), non-audio, folders and missing files",
      srv.register(S["mock"]) is None and srv.register(str(LIB / "notes.txt")) is None
      and srv.register(str(LIB / "folder.mp3")) is None and srv.register(str(LIB / "nope.mp3")) is None)
check("server: the extension check ignores case", srv.allowed(S["lose"][:-4] + ".MP3") is not None
      if os.path.normcase("A") == "a" else True)
link = LIB / "sneaky.mp3"
try:
    os.symlink(OUTSIDE, link)
    made = True
except (OSError, NotImplementedError):
    made = False                                         # (Windows without the symlink right)
check("server: a symlink inside the library pointing out of it is refused" if made else
      "server: (symlink check skipped: can't make symlinks here)", not made or srv.register(str(link)) is None)
ux = srv.register(S["rain"])
with srv._lock:
    k = ux.rsplit("/", 1)[1].split(".")[0]
    exp, real, ct = srv._items[k]
    srv._items[k] = (time.time() - 1, real, ct)
check("server: an expired token is 404", fetch(ux)[0] == 404)
ub = srv.register(S["big"])
conn = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
conn.sendall(f"GET /m/{ub.rsplit('/', 1)[1]} HTTP/1.0\r\n\r\n".encode())
conn.recv(1024)
conn.close()                                             # the Echo hangs up mid-song (next / stop)
time.sleep(0.2)
st, _, body = fetch(ub)
check("server: a client that hangs up mid-song is shrugged off; it keeps serving",
      st == 200 and body == Path(S["big"]).read_bytes(), st)

# =============================================================== 3. the router + one player per Echo
KIT, BED, LIV = FakeSpeaker(), FakeSpeaker(), FakeSpeaker(media=False)
for name, room, spk in (("kitchen", "kitchen", KIT), ("bedroom", "bedroom", BED), ("living", "living room", LIV)):
    svc.add_speaker(name, room, spk, "127.0.0.1")
L, P = FakeLights(), FakePlugs()
brain = FakeBrain()
r = Router(cfg, brain, L, P, Mem(), lambda: "sys", music=svc)
kp, bp = svc.players["kitchen"], svc.players["bedroom"]


def say(text, room="kitchen", sat="kitchen"):
    return r.handle(text, room, satellite=sat).get("say", "")


def reset():
    for s in (KIT, BED, LIV):
        s.calls.clear()


out = say("play eminem")
eminem = {os.path.normcase(S[k]) for k in ("lose", "8mile", "stan", "slim")}
check("'play eminem' said to the kitchen plays on the kitchen Echo only",
      len(KIT.urls()) == 1 and not BED.calls and out.startswith("Playing"), (out, KIT.calls, BED.calls))
check("its queue is Eminem's MP3s only (the .wma is skipped)", {os.path.normcase(p) for p in kp.queue} == eminem,
      kp.queue)
st, hd, body = fetch(KIT.urls()[0])
check("the Echo's URL comes from the song server with that song's exact bytes",
      st == 200 and body == Path(kp.current_file()).read_bytes(), (st, kp.current_file()))
check("each Echo has its own Now Playing list and session", (DATA / "Now Playing - kitchen.m3u8").is_file()
      and (DATA / "music_session-kitchen.json").is_file(), os.listdir(DATA))
title, who = svc.describe(kp.current_file())
out = say("what is playing")
check("'what is playing' says the title and artist from the index", out == f"This is {title} by Eminem.", out)
check("'what song is this' / 'who sings this' too",
      say("what song is this") == out and say("who sings this") == out)

# the Echo's state moves the queue on
kp.on_state("playing")
kp._sent -= 10                                           # (a song that played for a while)
kp.on_state("idle")
check("PLAYING -> IDLE after a real song plays the next one", wait_for(lambda: len(KIT.urls()) == 2) and kp.index == 1,
      KIT.calls)
out = say("skip")
check("'skip' with music on -> the next song", len(KIT.urls()) == 3 and kp.index == 2 and out == "Skipped.", out)
out = say("next song")
check("'next song' too", len(KIT.urls()) == 4 and kp.index == 3, out)
out = say("previous song")
check("'previous song' goes back", len(KIT.urls()) == 5 and kp.index == 2 and out == "Going back.", out)
out = say("pause")
check("'pause' with music on -> PAUSE", KIT.cmds()[-1:] == ["pause"] and kp.paused and out == "Paused.", (out, KIT.calls))
kp.on_state("paused")
check("'what's playing' while paused says so", say("what's playing").endswith("it's paused."))
out = say("resume")
check("'resume' -> PLAY", KIT.cmds()[-1:] == ["play"] and not kp.paused and out == "Resuming.", out)
kp.on_state("playing")
kp.on_state("paused")
check("PAUSED from the Echo itself marks it paused", kp.paused)
say("unpause")
kp.on_state("playing")
out = say("stop the music")
check("'stop the music' -> STOP, nothing loaded after", KIT.cmds()[-1:] == ["stop"] and not kp.has_music()
      and out == "Stopped.", out)
kp.on_state("paused")                                    # (echod keeps a STOP as a pause)
n = len(KIT.urls())
out = say("keep playing")
check("'keep playing' after a stop -> PLAY (the same song, no new URL)", KIT.cmds()[-1:] == ["play"]
      and len(KIT.urls()) == n and kp.has_music(), (out, KIT.calls))
kp.on_state("playing")

# the volume — never the lights
L.calls.clear()
out = say("set the volume to 40 percent")
check("'set the volume to 40 percent' -> set_volume(0.4), lights untouched",
      KIT.calls[-1] == ("vol", 0.4) and not L.calls and "40 percent" in out, (out, KIT.calls[-1:], L.calls))
say("set the volume to forty-five percent")
check("'set the volume to forty-five percent' (spelled out)", KIT.calls[-1] == ("vol", 0.45), KIT.calls[-1:])
say("volume 7")
check("'volume 7' = 7 out of 10", KIT.calls[-1] == ("vol", 0.7), KIT.calls[-1:])
say("turn it up")
check("'turn it up' with music on -> louder (+0.1)", KIT.calls[-1] == ("vol", 0.8), KIT.calls[-1:])
say("make it a little quieter")
check("'make it a little quieter' -> -0.1", KIT.calls[-1] == ("vol", 0.7), KIT.calls[-1:])
say("set the bedroom volume to 30")
check("'set the bedroom volume to 30' from the kitchen -> the bedroom Echo", BED.calls[-1:] == [("vol", 0.3)]
      and KIT.calls[-1] == ("vol", 0.7), (BED.calls, KIT.calls[-1:]))
say("mute")
say("unmute")
check("'mute' / 'unmute'", KIT.cmds()[-2:] == ["mute", "unmute"], KIT.cmds())
check("no light was touched by any of that", not L.calls, L.calls)

# vague phrases with nothing on in that room are NOT music
reset()
j = brain.json_calls
say("turn it up", "bedroom", "bedroom")
say("pause", "bedroom", "bedroom")
check("'turn it up' / 'pause' with nothing on there -> not music (the brain gets them)",
      not BED.calls and brain.json_calls == j + 2, (BED.calls, brain.json_calls - j))
say("louder", "bedroom", "bedroom")
check("'louder' is always the speaker", BED.calls == [("vol", 0.4)], BED.calls)

# another room
reset()
out = say("play eminem in the bedroom")
check("'play eminem in the bedroom' from the kitchen -> the bedroom Echo only",
      len(BED.urls()) == 1 and not KIT.urls() and out.endswith("on the bedroom speaker."), (out, KIT.calls, BED.calls))
from ava.tools.musiclib import player as PLR, playlists as PLS  # noqa: E402

PLS.save("road trip", [S["tat"], S["rain"], S["storm"]])
reset()
out = say("play my road trip playlist on the bedroom speaker")
check("a saved playlist 'on the bedroom speaker' -> the bedroom, in order",
      len(BED.urls()) == 1 and not KIT.urls() and [os.path.normcase(p) for p in bp.queue]
      == [os.path.normcase(S[k]) for k in ("tat", "rain", "storm")], (out, bp.queue))
reset()
out = say("play eminem in the garage")
check("an unknown room -> says which rooms have speakers", not KIT.calls and not BED.calls
      and "no speaker in the garage" in out and "kitchen" in out and "bedroom" in out, out)
out = say("play eminem in the living room")
check("a room whose Echo has no speaker connected says so", "can't reach the living room speaker" in out
      and not KIT.calls and not BED.calls, out)

# the collisions that used to go elsewhere
reset()
out = say("play time after time")
check("'play time after time' is music, not the clock", out == "Playing Time After Time by Cyndi Lauper."
      and os.path.normcase(kp.current_file()) == os.path.normcase(S["tat"]), out)
WEATHER.clear()
out = say("play stormy weather")
check("'play stormy weather' is music, not the forecast", not WEATHER and out.startswith("Playing Stormy Weather")
      and KIT.urls()[-1].endswith(".flac"), (out, WEATHER))
L.calls.clear()
out = say("turn on purple rain")
check("'turn on purple rain' plays Prince, not a purple light", not L.calls and out.startswith("Playing Purple Rain"),
      (out, L.calls))
r.handle("turn on the bedroom lights", "kitchen", satellite="kitchen")
r.handle("make the living room blue", "kitchen", satellite="kitchen")
check("lights still work: 'turn on the bedroom lights', 'make the living room blue'",
      L.calls == [("state", "bedroom", True, "kitchen"), ("color", "living room", "blue", "kitchen")], L.calls)
r.handle("turn the porch on", "kitchen", satellite="kitchen")
check("the porch plug still works", P.calls[-1] == ("porch", True), P.calls)
check("'what time is it' still tells the time", ":" in say("what time is it"))
check("'play a game' isn't claimed by music (the brain gets it)", say("play a game") == "the brain answered")
out = say("play some rock")
check("musiclib's grammar: 'play some rock' builds a playlist from the tags", out.startswith("Playing")
      and os.path.normcase(kp.current_file()) in {os.path.normcase(S["rain"]), os.path.normcase(S["big"])}, out)
out = say("what albums do i have by eminem")
check("musiclib's library questions work", "8 Mile" in out and "Marshall Mathers" in out, out)
out = say("i love this song")
check("'i love this song' knows the song on THIS Echo", out.startswith("Noted") and "by Prince" in out, out)
out = say("delete my road trip playlist")
check("deleting a playlist asks first", "Say yes" in out and PLS.find("road trip"), out)
out = say("yes")
check("... and a yes from the same Echo deletes the list", "Deleted" in out and not PLS.find("road trip"), out)
rb = Router(cfg, FakeBrain({"action": "music", "query": "lose yourself", "say": "On it."}), FakeLights(), FakePlugs(),
            Mem(), lambda: "sys", music=svc)
reset()
out = rb.handle("could you throw that lose yourself thing on", "kitchen", satellite="kitchen")["say"]
check("a 'play' the fast path missed reaches music through the brain ('music' intent)",
      out.startswith("Playing Lose Yourself") and len(KIT.urls()) == 1, out)
OFF = FakeSpeaker()
svc.add_speaker("office", "office", OFF, "127.0.0.1")
out = say("play some music", "office", "office")
check("'play some music' with nothing on: a shuffled mix of the whole library (musiclib skips songs under 40 s, "
      "so here only the long ones)", out.startswith("Playing") and "shuffled" in out and len(OFF.urls()) == 1, out)
say("pause the music", "office", "office")
out = say("play some music", "office", "office")
check("'play some music' while paused: carries on with it", out == "Resuming." and OFF.cmds()[-2:] == ["pause", "play"]
      and len(OFF.urls()) == 1, (out, OFF.calls))
KIT.media = False
out = say("play eminem")
check("an Echo that isn't connected: says so plainly", "can't reach the kitchen speaker" in out, out)
KIT.media = True
r0 = Router(cfg, FakeBrain(), FakeLights(), FakePlugs(), Mem(), lambda: "sys", music=None)
out = r0.handle("play eminem", "kitchen")["say"]
out2 = r0.handle("set the volume to 50 percent", "kitchen")["say"]
check("without the music service: a plain 'isn't set up', and the volume still never dims a light",
      "isn't set up" in out and "isn't set up" in out2 and not r0.lights.calls, (out, out2, r0.lights.calls))

# words that are also song titles
DEN = FakeSpeaker()
svc.add_speaker("den", "den", DEN, "127.0.0.1")                  # (nothing ever played on it)
outs = []
for q in ("pause", "stop"):
    rq = Router(cfg, FakeBrain({"action": "music", "query": q, "say": "On it."}), FakeLights(), FakePlugs(), Mem(),
                lambda: "sys", music=svc)
    outs.append(rq.handle(q, "den", satellite="den")["say"])
check("the brain's 'music' intent for 'pause' / 'stop' with nothing on: she says so — never a song search (\"Pause\", "
      "\"Don't Stop Me Now\")", not DEN.calls and all(o.startswith("Nothing's playing") for o in outs), (outs, DEN.calls))
reset()
j = brain.json_calls
outs = [say(t) for t in ("turn on the news", "turn on do not disturb", "turn on the sprinklers")]
check("'turn on the news' / 'turn on do not disturb' / 'turn on the sprinklers' aren't music, whatever songs share the "
      "name (the brain gets them)", not KIT.calls and brain.json_calls == j + 3 and set(outs) == {"the brain answered"},
      (outs, KIT.calls))
out = say("play twenty questions")
check("'play twenty questions' (no such song) goes to the brain, not 'I couldn't find'", out == "the brain answered"
      and not KIT.calls, out)


class DownBrain(FakeBrain):
    def chat_json(self, system, user, temperature=None):
        raise BrainError("down")


out = Router(cfg, DownBrain(), FakeLights(), FakePlugs(), Mem(), lambda: "sys", music=svc).handle(
    "play twenty questions", "kitchen", satellite="kitchen")["say"]
check("... and with the brain down, she says she couldn't find it", out == "I couldn't find twenty questions in your "
      "music.", out)
out = say("play thank you")
check("'play thank you' is Dido's song (not a bare 'play' and some manners)", out == "Playing Thank You by Dido.", out)
out = say("play right now")
check("'play right now' is Van Halen's", out == "Playing Right Now by Van Halen.", out)
out = say("play, thank you")
check("... but 'play, thank you' is still just 'play'", out == "It's already playing." and len(KIT.urls()) == 2, out)
out = say("play country paper scissors")
check("a name musiclib can't place comes back as a sentence, never just the name", out == "I couldn't find country "
      "paper scissors in your music.", out)
outs = [say(t, "living room", "living") for t in ("what albums do i have by eminem", "list my playlists", "play eminem")]
check("library questions need no speaker: an Echo whose speaker can't be reached still answers them (but can't play)",
      "8 Mile" in outs[0] and "playlist" in outs[1] and "can't reach the living room speaker" in outs[2], outs)
say("stop the music")
res = r.handle("skip this artist", "kitchen", satellite="kitchen")
check("'skip this artist' with nothing on doesn't claim it skipped", not res.get("ok") and "Skipped" not in res["say"],
      res)

# =============================================================== 4. songs that end, fail, and blips
q7 = TMP / "seven.m3u8"
PLS.write_m3u8(q7, LOGIC)
reset()
with PLR.use(kp):
    kp.open_list(str(q7))
for i in range(5):
    kp.on_state("playing")
    kp._sent = time.monotonic() - 2.0                   # IDLE 2 s after its URL: it never really played
    kp.on_state("idle")
    wait_for(lambda: len(KIT.urls()) == i + 2 or not kp.has_music(), 2)
check("5 songs in a row that stop at once -> she stops (5 URLs, no 6th)", len(KIT.urls()) == 5 and not kp.has_music(),
      (len(KIT.urls()), kp.has_music()))
reset()
kp.on_state("idle")
kp.on_state("playing")
kp.on_state("idle")
time.sleep(0.2)
check("IDLE / PLAYING blips with nothing loaded (a voice reply) are ignored", not KIT.calls, KIT.calls)
with PLR.use(kp):
    kp.open_list(str(q7))
kp.STALE_S = 0.3
kp.on_state("playing")
kp.on_state("idle")                                     # at once: maybe the tail of what came before
kp.on_state("playing")                                  # ... it was: the new song started
time.sleep(0.6)
check("an IDLE right after a new URL is looked at again — and ignored when the song starts", len(KIT.urls()) == 1,
      KIT.urls())
kp.on_state("idle")                                     # at once again, and it stays idle
check("... but when it stays idle, that song failed: the next one", wait_for(lambda: len(KIT.urls()) == 2)
      and kp.index == 1 and kp._fails == 1, (KIT.urls(), kp._fails))
del kp.STALE_S
kp.on_state("playing")
kp._sent -= 300
kp.on_state("idle")
check("a song that played to its end resets the failure count", wait_for(lambda: len(KIT.urls()) == 3)
      and kp._fails == 0, kp._fails)
with PLR.use(kp):
    one = TMP / "one.m3u8"
    PLS.write_m3u8(one, [S["tat"]])
    kp.open_list(str(one))
reset()
kp.on_state("playing")
kp._sent -= 300
kp.on_state("idle")
time.sleep(0.3)
check("the end of the list: she stops quietly (no URL, no command)", not KIT.calls and not kp.has_music(), KIT.calls)

# a blip: the Echo drops off the API mid-song, but its song stream (a connection of its own) plays on
with PLR.use(kp):
    kp.open_list(str(q7))
kp.on_state("playing")
kp.lost()
kp.on_state("playing")                                  # its first state after the reconnect: still playing
reset()
out = say("stop the music")
check("after a blip, the song that played on can still be stopped", out == "Stopped." and KIT.cmds() == ["stop"],
      (out, KIT.calls))
with PLR.use(kp):
    kp.open_list(str(q7))
kp.on_state("playing")
kp.lost()
kp.on_state("playing")
reset()
outs = [say("resume"), say("what's playing"), say("pause")]
check("... and 'resume' doesn't restart it, 'what's playing' knows it, a bare 'pause' pauses it",
      outs[0] == "It's already playing." and outs[1].startswith("This is Track 1") and outs[2] == "Paused."
      and KIT.calls == [("cmd", "pause")], (outs, KIT.calls))

# PAUSED, then PLAYING again on the Echo itself (or by echod after a voice turn): it's playing
with PLR.use(kp):
    kp.open_list(str(q7))
for s in ("playing", "paused", "playing"):
    kp.on_state(s)
reset()
out = say("pause")
check("PAUSED then PLAYING from the Echo: playing again, so 'pause' pauses it", out == "Paused." and KIT.cmds() == ["pause"]
      and kp.paused, (out, KIT.calls))
kp.on_state("paused")
kp.on_state("announcing")                               # a reply over the pause
check("... and a reply over a pause (ANNOUNCING) leaves it paused", kp.paused and say("what's playing").endswith("paused."))

# a slow NAS (spinning up) while a song changes never holds up the hub's loop: the Echo's state goes to the speaker's
# own thread
say("resume")
real_register, gate = srv.register, threading.Event()
srv.register = lambda path, reach_ip="": (gate.wait(5), real_register(path, reach_ip))[1]
th = threading.Thread(target=say, args=("next",))
th.start()
time.sleep(0.2)                                          # ("next" holds the speaker while it registers the song)
t0 = time.monotonic()
svc.on_state("kitchen", "playing", 0.42, False)          # what the hub calls, on its loop
took = time.monotonic() - t0
gate.set()
th.join(10)
srv.register = real_register
check("a song change waiting on a slow NAS doesn't hold up the hub's loop (the state is handed over at once)",
      took < 0.1 and wait_for(lambda: kp.volume == 0.42) and kp.index == 1, (took, kp.volume, kp.index))

svc.stop()

# =============================================================== 5. the hub: media_player on the Echo, on the loop
class FakeClient:
    def __init__(self, obj="speaker"):
        self.events, self.cmds, self.state_cb, self.obj = [], [], None, obj

    def send_voice_assistant_event(self, etype, data):
        self.events.append((etype, dict(data or {})))

    def subscribe_voice_assistant(self, **kw):
        self.voice = kw

    async def list_entities_services(self):
        return [SwitchInfo(object_id="dlna", key=3, name="DLNA"),
                MediaPlayerInfo(object_id=self.obj, key=7, name="Speaker", device_id=0)], []

    def subscribe_states(self, cb):
        self.state_cb = cb

    def media_player_command(self, key, *, command=None, volume=None, media_url=None, announcement=None, device_id=0):
        self.cmds.append((threading.get_ident(), key, command, volume, media_url))


def state(s, vol=0.5):
    return MediaPlayerEntityState(key=7, state=s, volume=vol, muted=False)


async def hub_checks():
    loop_thread = threading.get_ident()
    hcfg = make_cfg(TMP / "hubdata")
    rt = Router(hcfg, FakeBrain(), FakeLights(), FakePlugs(), Mem(), lambda: "sys")
    replies = ReplyServer(hcfg).start()
    hub = SatelliteHub(hcfg, router=rt, transcribe=lambda pcm: "play eminem", synth=lambda text: b"RIFF",
                       replies=replies)
    hub._loop = asyncio.get_running_loop()
    sat = Satellite(hub, {"name": "kitchen", "ip": "127.0.0.1", "room": "kitchen"})
    other = Satellite(hub, {"name": "office", "ip": "127.0.0.1", "room": "office"})
    hub.sats = [sat, other]
    hub.start_music()
    check("hub: makes the music service (music.enabled) and hands it to the router",
          hub.music is not None and rt.music is hub.music and set(hub.music.players) == {"kitchen", "office"})
    cli = FakeClient()
    sat.client = cli
    await hub.on_connect(sat, cli)
    check("hub: on connect it finds the media_player 'speaker' and follows its state",
          sat.connected and sat.media_key == 7 and cli.state_cb is not None and cli.voice, (sat.media_key,))
    res = await asyncio.to_thread(hub._ask_router, "play eminem", sat)
    await asyncio.sleep(0.1)
    check("hub: a play from the router's worker thread is sent on the hub's loop",
          res.get("say", "").startswith("Playing") and len(cli.cmds) == 1 and cli.cmds[0][0] == loop_thread
          and cli.cmds[0][1] == 7 and cli.cmds[0][4].startswith("http://127.0.0.1:"), (res, cli.cmds))
    st, _, body = fetch(cli.cmds[0][4])
    sp = hub.music.players["kitchen"]
    check("hub: and the Echo can fetch that song", st == 200 and body == Path(sp.current_file()).read_bytes(), st)
    cli.state_cb(state(MediaPlayerState.ANNOUNCING, 0.5))
    check("hub: ANNOUNCING reaches its player as itself (a reply over the music, never 'it's playing again')",
          wait_for(lambda: sp.device == "announcing"), sp.device)
    cli.state_cb(state(MediaPlayerState.PLAYING, 0.5))
    cli.state_cb(MediaPlayerEntityState(key=99, state=MediaPlayerState.IDLE))      # another entity: ignored
    check("hub: the Echo's state reaches its player (and only its media_player's) — off the loop, in order",
          sat.media_state == "playing" and wait_for(lambda: sp.device == "playing" and sp.volume == 0.5)
          and sp.device == "playing", (sat.media_state, sp.device))
    sp._sent -= 10
    cli.state_cb(state(MediaPlayerState.IDLE))
    await asyncio.sleep(0.3)
    check("hub: a song that ends -> the next URL, sent from the timer thread but on the loop",
          len(cli.cmds) == 2 and cli.cmds[1][0] == loop_thread and cli.cmds[1][4] and sp.index == 1, cli.cmds)
    res = await asyncio.to_thread(hub._ask_router, "pause the music", sat)
    await asyncio.sleep(0.1)
    check("hub: 'pause the music' -> media_player_command(PAUSE) on the loop",
          cli.cmds[-1][2] is not None and cli.cmds[-1][2].name == "PAUSE" and cli.cmds[-1][0] == loop_thread, cli.cmds)
    res = await asyncio.to_thread(hub._ask_router, "set the volume to 25 percent", sat)
    await asyncio.sleep(0.1)
    check("hub: volume -> media_player_command(volume=0.25)", cli.cmds[-1][3] == 0.25, cli.cmds[-1])
    await hub.on_disconnect(sat)
    n = len(cli.cmds)
    ok = sat.speaker.command("play")
    res = await asyncio.to_thread(hub._ask_router, "resume the music", sat)
    await asyncio.sleep(0.1)
    check("hub: disconnected -> nothing is sent, and she says she can't reach it",
          ok is False and len(cli.cmds) == n and "can't reach" in res.get("say", ""), (res, cli.cmds[n:]))
    await hub.on_connect(sat, cli)
    cli.state_cb(state(MediaPlayerState.PLAYING))
    cli.state_cb(state(MediaPlayerState.IDLE))
    await asyncio.sleep(0.3)
    check("hub: after a reconnect the Echo's old state never starts a song by itself", len(cli.cmds) == n
          and not sp.has_music(), cli.cmds[n:])
    res = await asyncio.to_thread(hub._ask_router, "resume the music", sat)
    await asyncio.sleep(0.1)
    check("hub: ... and 'resume the music' brings that song back", len(cli.cmds) == n + 1 and cli.cmds[-1][4]
          and sp.has_music() and res.get("say") == "Resuming.", (res, cli.cmds[n:]))
    cli2 = FakeClient(obj="media_player")
    other.client = cli2
    await hub.on_connect(other, cli2)
    check("hub: an Echo whose media_player isn't called 'speaker' still gets music", other.media_key == 7)
    # a whole voice turn: the router hears which Echo it came from
    from aioesphomeapi import VoiceAssistantEventType as E

    cli.cmds.clear()
    hub2 = SatelliteHub(hcfg, router=rt, transcribe=lambda pcm: "play time after time", synth=lambda t: b"RIFF",
                        replies=replies, music=hub.music)
    hub2._loop = hub._loop
    sat.hub = hub2
    await sat.handle_start("c1", 0, None, "hey ava")
    await sat.handle_audio(b"\x01\x00" * 1600)
    await sat.handle_stop(False)
    await asyncio.sleep(0.1)
    tts = [d.get("text", "") for t, d in cli.events if E(t).name.endswith("TTS_START")]
    check("hub: a voice turn passes this Echo to the router, so it plays HERE",
          tts and tts[-1].startswith("Playing Time After Time") and cli.cmds and cli.cmds[-1][4], (tts, cli.cmds))
    hub.music.stop()
    replies.stop()


asyncio.run(hub_checks())

# =============================================================== 6. the vendored library pulls in nothing else
PROBE = r"""
import os, sys, time, types
sys.path.insert(0, sys.argv[1])
os.environ["HOUSEAVA_DIR"] = sys.argv[3]
import houseava.music as HM
import houseava.satellite, houseava.brain
from houseava.brain import Router
from houseava.config import Config
cfg = Config(path=os.path.join(sys.argv[3], "none.json"))
for k, v in (("music.enabled", True), ("music.root", sys.argv[2]), ("music.data_dir", sys.argv[3]), ("music.http_port", 0),
             ("music.index", False), ("http.host", "127.0.0.1")):
    cfg.set(k, v)
class Spk:
    def __init__(self): self.n = 0
    def has_media(self): return True
    def play_url(self, u): self.n += 1; return True
    def command(self, c): return True
    def set_volume(self, v): return True
svc = HM.MusicService(cfg, [types.SimpleNamespace(name="kitchen", room="kitchen", ip="127.0.0.1", speaker=Spk())],
                      publish_host=lambda ip="": "127.0.0.1")
class L:
    def rooms(self): return {}
class P: devices = {}
class B:
    def status(self): return False, ""
class M:
    def add_turn(self, *a): pass
r = Router(cfg, B(), L(), P(), M(), lambda: "", music=svc)
said = [r.handle(t, "kitchen", satellite="kitchen")["say"] for t in ("play eminem", "i love this song",
        "never play this song again", "next", "skip this artist", "what's playing")]
print("SAID:" + " | ".join(said))
print("PLAYED:%d" % svc.players["kitchen"].speaker.n)
print("MODS:" + ",".join(sorted(m for m in sys.modules if m == "ava" or m.startswith("ava."))))
print("FILES:" + ",".join(sorted({os.path.dirname(os.path.abspath(sys.modules[m].__file__))
                                   for m in sys.modules if (m == "ava" or m.startswith("ava.")) and getattr(sys.modules[m], "__file__", None)})))
bad = [m for m in sys.modules if m.startswith(("ava.tools.media", "ava.tools.windows", "pyautogui", "win32", "PySide6",
                                                "ava.brain", "ava.assistant", "pywinauto"))]
print("BAD:" + ",".join(sorted(bad)))
"""
probe_dir = TMP / "probe"
probe_dir.mkdir()
pr = subprocess.run([sys.executable, "-c", PROBE, PROJ, str(LIB), str(probe_dir)], capture_output=True, text=True,
                    timeout=120)
lines = dict(ln.split(":", 1) for ln in pr.stdout.splitlines() if ":" in ln and ln.split(":", 1)[0].isupper())
mods = [m for m in lines.get("MODS", "").split(",") if m]
allowed = {"ava", "ava.log", "ava.paths", "ava.tools", "ava.tools.music", "ava.tools.musiclib"}
outside = [m for m in mods if m not in allowed and not m.startswith("ava.tools.musiclib.")]
said = lines.get("SAID", "").split(" | ")
check("import closure (fresh interpreter): play / love / never again / next / skip this artist ran on a room's speaker",
      pr.returncode == 0 and int(lines.get("PLAYED", "0") or 0) == 4 and len(said) == 6 and said[0].startswith("Playing")
      and said[1].startswith("Noted") and said[2].startswith("Okay") and said[3] == "Skipped."
      and said[4].startswith("Skipped Eminem"), (pr.returncode, lines.get("PLAYED"), said, pr.stderr[-800:]))
check("import closure: only the vendored list of ava modules was imported", mods and not outside, outside or mods)
vend = os.path.normcase(os.path.join(PROJ, "vendor"))
check("import closure: every one of them came from vendor/ (never desktop Ava)",
      all(os.path.normcase(f).startswith(vend) for f in lines.get("FILES", "").split(",") if f), lines.get("FILES"))
check("import closure: no PC-control / desktop module (media keys, windows, pyautogui, win32*, PySide6, brain, "
      "assistant)", lines.get("BAD", "x") == "", lines.get("BAD"))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n==== RESULT: {'ALL PASSED' if not fails else 'FAILURES: ' + str(fails)} ====")
sys.exit(1 if fails else 0)
