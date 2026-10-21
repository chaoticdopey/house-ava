"""Music from the NAS library on the Echo that heard you ("play eminem", "play some 80s rock in the bedroom").

The library side is desktop Ava's musiclib, VENDORED (vendor/ava, copied in from desktop Ava): the tag index,
playlists from tags, saved playlists, "skip this artist", "what albums do I have by Logic". Only that subset is there —
no PC-control code — and vendor/ goes on sys.path here and nowhere else.

The House Ava side:
  SpeakerPlayer  one per Echo (musiclib's Player seam): its own queue, sent ONE URL PER SONG (echod has no queue of
                 its own, and an MP3 stream dies after 20 bad frames in total — never one long stream). A song that
                 ends on its own turns the Echo PLAYING -> IDLE, and that starts the next one.
  MusicServer    streams only the songs a speaker was told to play: /m/<random token>.mp3|flac, only real MP3 / FLAC
                 files under music.root, tokens expire after 12 h. No path in the URL, nothing else served.
  MusicService   ties them together for the hub: musiclib's data folder, its config (media.music_folders =
                 [music.root]), the background tag index, an optional one-time seed from desktop's file list.

The hub gives each SpeakerPlayer a Speaker control (satellite.py SatSpeaker): play_url(url), command(name) for
play / pause / stop / mute / unmute, set_volume(0..1), has_media(). SpeakerPlayer talks to nothing else.
The Echo plays MP3 and FLAC itself; wma / m4a are skipped, nothing is transcoded.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))         # (only here: the vendored copy of desktop Ava's music library)

from ava.tools import music as library   # noqa: E402  desktop's song search + its file-list index
from ava.tools.musiclib import index, intent, ops, player, playlists, spec  # noqa: E402,F401

log = logging.getLogger("houseava.music")

PLAYABLE = {".mp3": "audio/mpeg", ".flac": "audio/flac"}    # what echod decodes itself


# ---------------------------------------------------------------- what musiclib reads
class LibraryConfig:
    """House Ava's config as musiclib reads it: its one music folder is music.root (desktop's is media.music_folders)."""

    def __init__(self, cfg, root: str):
        self._cfg = cfg
        self.root = root

    def get(self, key: str, default=None):
        if key == "media.music_folders":
            return [self.root]
        if key == "media.music_player":
            return ""                                   # (no Winamp here)
        return self._cfg.get(key, default) if self._cfg is not None else default


class _Nowhere(player.Player):
    """The process default: a request without a speaker plays nowhere (never desktop's Winamp)."""

    name = "a speaker"


# ---------------------------------------------------------------- the one-time seed from desktop's file list
def rewrite_path(path: str, desktop_root: str, root: str) -> str | None:
    """'D:\\Music\\Eminem\\8 Mile (2002)\\01 - Lose Yourself.mp3' -> '/mnt/music/Eminem/8 Mile (2002)/01 - Lose
    Yourself.mp3' (None when it isn't under desktop_root). Separators follow `root`: '/' for a Linux mount."""
    src = desktop_root.replace("/", "\\").rstrip("\\")
    p = str(path or "").replace("/", "\\")
    if not src or not p.lower().startswith(src.lower() + "\\"):
        return None
    parts = [x for x in p[len(src) + 1:].split("\\") if x]
    if not parts or any(x in (".", "..") for x in parts):
        return None
    sep = "/" if root.startswith("/") else os.sep
    return root.rstrip("/\\") + sep + sep.join(parts)


def seed_index(seed_file, data_dir, desktop_root: str, root: str) -> int:
    """Desktop Ava's music_index.json ({built, files, artists}) with its paths moved onto this box's mount, saved as
    musiclib's own music_index.json — once (never over one that's there). The tag index seeds its rows from it on its
    first pass (index._seed_from_filelist), and the title search uses it at once. -> songs written."""
    dst = Path(data_dir) / "music_index.json"
    if dst.exists():
        return 0
    try:
        data = json.loads(Path(seed_file).read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("music seed %s not read: %s", seed_file, exc)
        return 0
    files = [q for q in (rewrite_path(p, desktop_root, root) for p in data.get("files") or []) if q]
    want = desktop_root.replace("/", "\\").rstrip("\\").lower()
    artists = {root: list(names) for r, names in (data.get("artists") or {}).items()
               if str(r).replace("/", "\\").rstrip("\\").lower() == want}     # (its artist folders: same names here)
    out = {"built": float(data.get("built", 0) or 0), "files": files, "artists": artists}
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_text(json.dumps(out), encoding="utf-8")
        os.replace(tmp, dst)
    except Exception as exc:
        log.warning("music seed not saved: %s", exc)
        return 0
    log.info("music: seeded %d songs from %s", len(files), seed_file)
    return len(files)


# ---------------------------------------------------------------- one Echo's music
class SpeakerPlayer(player.Player):
    """One Echo: its queue, played a song at a time. Commands arrive from the router's worker thread and the Echo's
    state from the hub's event loop, so everything is under one lock — which a song change holds while it registers the
    next file (that touches the network share: seconds when the NAS is slow). So the loop never takes it: the Echo's
    state and its disconnects are handed to this speaker's own thread (post), in order, and the loop never waits."""

    STALE_S = 1.5        # an IDLE this soon after a new URL may be the tail of what came before it: look again then
    FAIL_S = 3.0         # a song that stops within this long of its URL never really played (404, unplayable)
    MAX_FAILS = 5        # that many in a row and she stops

    def __init__(self, key: str, room: str, speaker, server: "MusicServer", reach_ip: str = ""):
        self.key = key                               # the satellite's name: its own Now Playing / session files
        self.room = room or ""
        self.name = f"the {room or key} speaker"
        self.speaker = speaker
        self.server = server
        self.reach_ip = reach_ip
        self._lock = threading.RLock()
        self.queue: list[str] = []
        self.index = -1
        self.loaded = False                          # a song of ours is on the Echo (playing or paused)
        self.paused = False
        self.stopped = False                         # you stopped it: "resume" brings that song back
        self.volume: float | None = None             # last known (the Echo's report, or what we set)
        self.muted = False
        self.device = ""                             # the Echo's last reported state: idle / playing / paused / announcing
        self.adrift = False                          # the Echo dropped with a song of ours on: did it play on?
        self._adopted = False                        # ... it did, and we follow it again — but don't follow on from it
        self._sent = 0.0                             # when the current song's URL went out (monotonic)
        self._token = ""                             # ... and its token on the song server (did the Echo hang up on it?)
        self._gen = 0                                # +1 per URL sent: a late check for an older song does nothing
        self._fails = 0
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()    # (fn, args) from the hub's loop, run in order (post)
        threading.Thread(target=self._drain, daemon=True, name=f"houseava-music-{key}").start()

    # ---- musiclib's Player contract -------------------------------------------------------------
    def open_list(self, m3u8_path, cfg=None) -> dict:
        paths = [p for p in playlists.read_m3u8(m3u8_path) if os.path.splitext(p)[1].lower() in PLAYABLE]
        if not paths:
            return {"ok": False, "player": self.name, "say": "None of those songs are MP3 or FLAC, and that's all the "
                                                             "speaker can play."}
        if not self.speaker.has_media():
            return {"ok": False, "player": self.name, "say": f"I can't reach {self.name} right now."}
        with self._lock:
            self.queue, self._fails = paths, 0
            ok = self._start(0)
        if not ok:
            return {"ok": False, "player": self.name, "say": f"I couldn't start {self.name}."}
        return {"ok": True, "player": self.name}

    def _settle(self) -> bool:
        """Lock held. Something else has taken the speaker from our song (a phone over Bluetooth, AirPlay - see
        _taken_over): ours isn't on any more, so "what's playing" / "next" aren't about it. -> it was taken over?
        (pause / stop still go to the Echo: they stop whatever is playing, which is what was asked.)"""
        if self.loaded and not self.paused and self._taken_over():
            self.loaded = self.paused = False
            self.stopped = True
            return True
        return False

    def current_file(self) -> str | None:
        with self._lock:
            self._settle()
            return self.queue[self.index] if self.loaded and 0 <= self.index < len(self.queue) else None

    def position(self):
        with self._lock:
            self._settle()
            return (self.index, len(self.queue)) if self.loaded else (None, None)

    def state(self) -> str | None:
        with self._lock:
            self._settle()
            if self.loaded:
                return "paused" if self.paused else "playing"
            return "stopped" if self.queue else None

    def next(self) -> dict:
        with self._lock:
            if self._settle():
                return {"ok": False, "say": "That's not my music playing. Skip it on whatever's playing it."}
            if not self.loaded:
                return {"ok": False, "say": f"Nothing's playing on {self.name}."}
            if self.index + 1 >= len(self.queue):
                self.speaker.command("stop")
                self.loaded = self.paused = False
                return {"ok": True, "say": "That was the last song."}
            ok = self._start(self.index + 1)
        return {"ok": ok, "say": "Skipped." if ok else f"I couldn't play the next song on {self.name}."}

    def previous(self) -> dict:
        with self._lock:
            if not self.queue or self.index < 0:
                return {"ok": False, "say": f"Nothing's playing on {self.name}."}
            back = max(0, self.index - 1)
            again = back == self.index                # (on the first song: that song from the top)
            ok = self._start(back)
        if not ok:
            return {"ok": False, "say": f"I couldn't play that on {self.name}."}
        return {"ok": True, "say": "Starting it over." if again else "Going back."}

    def pause(self) -> bool:
        with self._lock:
            if not self.loaded and self.device == "playing":     # something else is playing (a phone over
                return self.speaker.command("pause")             # Bluetooth, AirPlay): "pause" is for that
            if not self.loaded or self.paused:
                return False
            self.speaker.command("pause")
            self.paused = True
            return True

    def resume(self) -> bool:
        with self._lock:
            if self.loaded and self.paused:
                self.speaker.command("play")
                self.paused = self._adopted = False
                return True
            if self.loaded or not self.stopped or not (0 <= self.index < len(self.queue)):
                return False
            if self.device == "paused":              # echod keeps a STOP as a pause for 30 minutes
                if not self.speaker.command("play"):
                    return False
                self.loaded, self.paused, self.stopped, self._adopted = True, False, False, False
                return True
            return self._start(self.index)           # (it let go of it: the song again, from the top)

    # ---- the rest of a speaker's controls -------------------------------------------------------
    def stop(self) -> bool:
        with self._lock:
            if not self.loaded:
                if self.device == "playing":             # something else is playing (a phone over Bluetooth): stop it
                    return self.speaker.command("stop")
                return False
            self.speaker.command("stop")
            self.loaded = self.paused = False
            self.stopped = True
            return True

    def has_music(self) -> bool:
        with self._lock:
            self._settle()
            return self.loaded

    def set_volume(self, v: float) -> float:
        v = round(max(0.0, min(1.0, float(v))), 2)
        with self._lock:
            self.speaker.set_volume(v)
            self.volume = v
        return v

    def louder(self, step: float = 0.1) -> float:
        with self._lock:
            return self.set_volume((self.volume if self.volume is not None else 0.5) + step)

    def quieter(self, step: float = 0.1) -> float:
        return self.louder(-step)

    def mute(self, on: bool = True) -> None:
        with self._lock:
            self.speaker.command("mute" if on else "unmute")
            self.muted = on

    # ---- the Echo's state (from the hub's event loop, through post) -----------------------------
    def post(self, fn, *args) -> None:
        """fn(*args) on this speaker's own thread, one at a time and in order: how the hub's loop hands over the Echo's
        state, so it never waits for the lock."""
        self._inbox.put((fn, args))

    def _drain(self) -> None:
        while True:
            fn, args = self._inbox.get()
            try:
                fn(*args)
            except Exception:
                log.exception("%s: %s failed", self.key, getattr(fn, "__name__", fn))

    def on_state(self, state: str, volume: float | None = None, muted: bool | None = None) -> None:
        """idle / playing / paused / announcing from the Echo's media_player. A song of ours going PLAYING -> IDLE
        ended (or never started): the next one. With nothing of ours on, every change is a voice reply (IDLE ->
        PLAYING -> IDLE)."""
        with self._lock:
            prev, self.device = self.device, state
            if volume is not None:
                self.volume = round(float(volume), 2)
            if muted is not None:
                self.muted = bool(muted)
            if self.adrift:                          # the first state since a reconnect (lost)
                self.adrift = False
                if state in ("playing", "paused"):   # our song played on through the blip: follow it again
                    self.loaded, self.paused, self.stopped, self._adopted = True, state == "paused", False, True
                    return
            if not self.loaded:
                return
            if state == "paused":
                self.paused = True
                return
            if state == "playing":                   # (resumed on the Echo itself, or by whatever paused it)
                self.paused = False
                return
            if state != "idle":                      # announcing: a reply over our song (or our pause) changes nothing
                return
            if self._adopted or (self.paused and prev in ("paused", "announcing")):
                # a pause it held for 30 minutes and then let go of — or the song that outlived a blip, over: no
                # next song by surprise ("resume" brings it back)
                self.loaded = self.paused = False
                self.stopped = True
                return
            if prev not in ("playing", "announcing"):
                return
            self.paused = False                      # (it played on — resumed on the Echo itself — and ended)
            elapsed = time.monotonic() - self._sent
            self._check_later(self._gen, max(0.0, self.STALE_S - elapsed))

    def lost(self) -> None:
        """The Echo dropped off the API: we can't follow it — so a reconnect (maybe hours later) never starts the next
        song by surprise. "resume" brings the song back. Its stream is a connection of its own, though, so after a
        short blip the song may still be playing: if the Echo's first state says so, we follow that song again
        (stop / pause / next / "what's playing" work on it) — and it still doesn't run on into the next one."""
        with self._lock:
            if self.loaded:
                self.loaded = self.paused = False
                self.stopped = self.adrift = True
            self.device = ""

    TAKEOVER_S = 3.0     # echod let go of our song this long ago (or more) and is on something else = taken over

    def _taken_over(self) -> bool:
        """Something else has the speaker instead of our song: echod stopped reading it partway at least TAKEOVER_S ago.
        (When echod gives up on our song itself - a damaged file - it goes IDLE within moments of letting go, so that
        still moves on to the next song. Anything that takes over plays for longer: a phone's stream at least 5 s.)"""
        f = getattr(self.server, "hung_up_for", None)
        return f is not None and f(self._token) >= self.TAKEOVER_S

    def _check_later(self, gen: int, delay: float) -> None:
        t = threading.Timer(delay, self._check, args=(gen,))
        t.daemon = True
        t.start()

    def _check(self, gen: int) -> None:
        """The Echo went idle on song `gen`: is it still? Then that song is over — on to the next."""
        with self._lock:
            if gen != self._gen or not self.loaded or self.paused or self.device != "idle":
                return                               # it started after all, or something newer was sent
            elapsed = time.monotonic() - self._sent
            if elapsed < self.FAIL_S:
                self._fails += 1
                log.info("%s: %s didn't play (%d in a row)", self.key, os.path.basename(self.queue[self.index]),
                         self._fails)
                if self._fails >= self.MAX_FAILS:
                    log.warning("%s: %d songs in a row wouldn't play; stopping", self.key, self._fails)
                    self.loaded = self.paused = False
                    return
            else:
                self._fails = 0
                if self._taken_over():
                    # the Echo let go of our song partway, a while ago: something else took the speaker (a phone over
                    # Bluetooth, AirPlay...) and THAT has ended now. Don't start our next song over it - "resume"
                    # brings ours back.
                    log.info("%s: something else took the speaker; not starting the next song", self.key)
                    self.loaded = self.paused = False
                    self.stopped = True
                    return
            if not self._start(self.index + 1):
                self.loaded = self.paused = False    # the end of the list: stop quietly

    def _start(self, i: int) -> bool:
        """Send song i (else the next one the server will serve) to the Echo. Lock held. -> sent?"""
        while 0 <= i < len(self.queue):
            url = self.server.register(self.queue[i], self.reach_ip)
            if url:
                self.index = i
                self._gen += 1
                self._sent = time.monotonic()
                self._token = self.server.token_of(url)
                if self.speaker.play_url(url):
                    self.loaded, self.paused, self.stopped = True, False, False
                    self.adrift = self._adopted = False      # (a song we just sent: followed the usual way)
                    return True
                break                                # the Echo isn't there
            log.info("%s: skipping %s (missing, or not under the music folder)", self.key, self.queue[i])
            i += 1
        self.loaded = self.paused = False
        return False


# ---------------------------------------------------------------- the song server
_RX_URL = re.compile(r"/m/([A-Za-z0-9_-]{16,64})\.(mp3|flac)")
_RX_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def _byte_range(header: str, size: int):
    """One 'bytes=a-b' range -> (start, end); None = can't be met (416); False = ignore it (send the whole file)."""
    m = _RX_RANGE.fullmatch((header or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return False                                 # (several ranges, or nonsense: the whole file)
    if not m.group(1):                               # the last n bytes
        n = int(m.group(2))
        if n <= 0 or size <= 0:
            return None
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


class MusicServer:
    """The Echo fetches each song from here: http://<this box>:<port>/m/<token>.mp3. Only files registered for a
    speaker are served — real MP3 / FLAC files inside music.root (symlinks resolved first), under a random token that
    lasts 12 hours. No auth (echod can't send any), so nothing else is ever reachable: no paths, no listing."""

    KEEP = 12 * 3600
    CHUNK = 64 * 1024

    def __init__(self, root: str, host: str = "0.0.0.0", port: int = 8791, publish_host=None):
        self.root = os.path.realpath(root)
        self.host = host
        self.port = port
        self.publish_host = publish_host or (lambda reach_ip="": "127.0.0.1")
        self._items: dict[str, tuple[float, str, str]] = {}      # token -> (expires, real path, content type)
        # token -> the Echo hung up partway through that song. echod reads a song at the pace it plays, so a song that
        # ends is read to its end; one it hangs up on was pushed aside - a phone over Bluetooth, AirPlay, DLNA - and
        # whatever goes IDLE after that is that other thing ending, not our song (no "next song" by surprise).
        # (Missed when the rest of the song was already buffered - the last seconds - so it's only ever a "yes".)
        self._hung_up: dict[str, float] = {}
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None

    # ---- what may be served ---------------------------------------------------------------------
    def allowed(self, path) -> str | None:
        """The real path of `path` when it may be served (an MP3 / FLAC file inside music.root), else None."""
        try:
            real = os.path.realpath(str(path))
            root = os.path.normcase(self.root)
            if os.path.commonpath([os.path.normcase(real), root]) != root:
                return None
        except (OSError, ValueError):                # (another drive, a bad name)
            return None
        if os.path.splitext(real)[1].lower() not in PLAYABLE or not os.path.isfile(real):
            return None
        return real

    def register(self, path, reach_ip: str = "") -> str | None:
        """A URL the Echo at `reach_ip` can fetch this song from; None if it may not be served."""
        real = self.allowed(path)
        if real is None:
            return None
        ext = os.path.splitext(real)[1].lower()
        token = secrets.token_urlsafe(18)
        now = time.time()
        with self._lock:
            for k in [k for k, (exp, _, _) in self._items.items() if exp < now]:
                self._items.pop(k, None)
                self._hung_up.pop(k, None)
            self._items[token] = (now + self.KEEP, real, PLAYABLE[ext])
        return f"http://{self.publish_host(reach_ip)}:{self.port}/m/{token}{ext}"

    @staticmethod
    def token_of(url: str) -> str:
        m = _RX_URL.search(url or "")
        return m.group(1) if m else ""

    def hung_up(self, token: str) -> bool:
        """The Echo stopped reading this song partway (and hasn't fetched it again since)."""
        with self._lock:
            return token in self._hung_up

    def hung_up_for(self, token: str) -> float:
        """How long ago the Echo let go of this song partway; 0 = it didn't (or fetched it again since)."""
        with self._lock:
            at = self._hung_up.get(token)
        return max(0.001, time.time() - at) if at else 0.0

    def _fetching(self, token: str, cut: bool) -> None:
        with self._lock:
            if cut:
                self._hung_up[token] = time.time()
            else:
                self._hung_up.pop(token, None)          # (fetched again - a resume - so not pushed aside)

    def lookup(self, token: str, ext: str) -> tuple[str, str] | None:
        with self._lock:
            item = self._items.get(token)
        if item is None or item[0] < time.time() or os.path.splitext(item[1])[1].lower() != "." + ext:
            return None
        return item[1], item[2]

    # ---- serving --------------------------------------------------------------------------------
    def start(self) -> "MusicServer":
        if self._httpd is not None:
            return self
        srv = self

        class Handler(BaseHTTPRequestHandler):
            # (no timeout: a paused Echo holds its connection open without reading, for as long as it's paused)
            def do_GET(self):  # noqa: N802 (http.server API)
                self._serve(True)

            def do_HEAD(self):  # noqa: N802
                self._serve(False)

            def _serve(self, body: bool) -> None:
                m = _RX_URL.fullmatch(self.path.split("?", 1)[0])
                item = srv.lookup(m.group(1), m.group(2)) if m else None
                f = None
                if item is not None:
                    try:
                        f = open(item[0], "rb")
                    except OSError:
                        f = None
                if f is None:
                    self.send_error(404)
                    return
                with f:
                    size = os.fstat(f.fileno()).st_size
                    start, end, status = 0, size - 1, 200
                    rng = self.headers.get("Range")
                    if rng:
                        r = _byte_range(rng, size)
                        if r is None:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        if r:
                            (start, end), status = r, 206
                    length = max(0, end - start + 1)
                    self.send_response(status)
                    self.send_header("Content-Type", item[1])
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    if not body:
                        return
                    f.seek(start)
                    left = length
                    srv._fetching(m.group(1), cut=False)
                    try:
                        while left > 0:
                            chunk = f.read(min(srv.CHUNK, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    except OSError:                  # the Echo hung up (next song, stop, something else took over)
                        self.close_connection = True
                        if left > 0 and end == size - 1:
                            srv._fetching(m.group(1), cut=True)

            def log_message(self, *a):  # quiet
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):   # (a dropped connection is normal here)
                log.debug("music server: %s dropped: %s", client_address, sys.exc_info()[1])

        self._httpd = Server((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="houseava-music").start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# ---------------------------------------------------------------- the service the hub runs
class MusicService:
    """The library + one SpeakerPlayer per Echo + the song server.

    speakers: things with .name (the satellite), .room, .ip and .speaker (its Speaker control) — the hub's Satellites.
    publish_host: callable(reach_ip) -> this box's address as that Echo sees it (ReplyServer.host_for)."""

    def __init__(self, cfg, speakers=(), publish_host=None):
        from .config import data_dir

        self.cfg = cfg
        self.root = str(cfg.get("music.root", "/mnt/music") or "/mnt/music")
        self.lib_cfg = LibraryConfig(cfg, self.root)
        d = str(cfg.get("music.data_dir", "") or "")
        self.data_dir = Path(d) if d else data_dir() / "music"
        player.set_data_dir(self.data_dir)            # the index, playlists and sessions live here, not in vendor/
        player.set_default(_Nowhere())
        seed = str(cfg.get("music.seed_file", "") or "")
        self.seeded = seed_index(seed, self.data_dir, str(cfg.get("music.desktop_root", "") or ""),
                                 self.root) if seed else 0
        if publish_host is None:
            from .http_server import ReplyServer

            publish_host = ReplyServer(cfg).host_for
        port = cfg.get("music.http_port", 8791)
        self.server = MusicServer(self.root, host=str(cfg.get("http.host", "0.0.0.0") or "0.0.0.0"),
                                  port=8791 if port in (None, "") else int(port), publish_host=publish_host)
        self.players: dict[str, SpeakerPlayer] = {}
        for s in speakers:
            self.add_speaker(s.name, getattr(s, "room", ""), s.speaker, getattr(s, "ip", ""))

    def add_speaker(self, name: str, room: str, speaker, ip: str = "") -> SpeakerPlayer:
        p = self.players[name] = SpeakerPlayer(name, room, speaker, self.server, ip)
        return p

    # ---- lifecycle ------------------------------------------------------------------------------
    def start(self) -> "MusicService":
        self.server.start()
        log.info("music: library %s, songs served on port %s", self.root, self.server.port)
        if self.cfg.get("music.index", True) and self.library_ok():
            library.ensure_index(self.lib_cfg)        # the file list (title search) — once a day
            ops.ensure_index(self.lib_cfg)            # the tag index — hours the first time over the network
        return self

    def stop(self) -> None:
        self.server.stop()
        index.INDEXER.stop()                          # (it saves where it got to and carries on next start)

    def library_ok(self) -> bool:
        return os.path.isdir(self.root)

    def unreachable(self) -> dict:
        return {"ok": False, "say": f"I can't reach the music library right now — {self.root} isn't there. Is the "
                                    "NAS share mounted?"}

    # ---- the Echo's state, from the hub (its event loop: handed to the speaker's own thread) ------
    def on_state(self, name: str, state: str, volume=None, muted=None) -> None:
        p = self.players.get(name)
        if p is not None:
            p.post(p.on_state, state, volume, muted)

    def on_disconnect(self, name: str) -> None:
        p = self.players.get(name)
        if p is not None:
            p.post(p.lost)

    # ---- which speaker ---------------------------------------------------------------------------
    def rooms(self) -> list[str]:
        return list(dict.fromkeys(p.room or p.key for p in self.players.values()))

    def speaker_place(self, word: str) -> str | None:
        """'bedroom' / 'the kitchen echo' / 'living' (+ ' room') -> a room or satellite with a speaker, else None."""
        w = re.sub(r"^(?:the|my)\s+", "", (word or "").lower().strip())
        for cand in (w, w + " room"):
            for p in self.players.values():
                if cand and cand in (p.room.lower(), p.key.lower()):
                    return cand
        return None

    def _rooms_say(self) -> str:
        rs = self.rooms()
        if not rs:
            return "No Echo has a speaker set up for music yet."
        return "Speakers: " + (", ".join(rs[:-1]) + " and " + rs[-1] if len(rs) > 1 else rs[0]) + "."

    def target(self, satellite: str = "", room: str = "", where: str = "") -> tuple[SpeakerPlayer | None, str]:
        """The speaker a request is for: the one named ('in the bedroom'), else the Echo that heard it, else one in
        the caller's room. -> (player, "") or (None, what to say)."""
        if where:
            place = self.speaker_place(where)
            if place is None:
                return None, f"There's no speaker in the {where}. " + self._rooms_say()
            cands = [p for p in self.players.values() if place in (p.room.lower(), p.key.lower())]
            live = [p for p in cands if p.speaker.has_media()]
            if live:
                return live[0], ""
            return None, f"I can't reach {cands[0].name} right now."
        heard = self.players.get(satellite)
        if heard is not None and heard.speaker.has_media():
            return heard, ""
        same = [p for p in self.players.values() if room and p.room.lower() == room.lower() and p.speaker.has_media()]
        if same:
            return same[0], ""
        if heard is not None:
            return None, f"I can't reach {heard.name} right now."
        live = [p for p in self.players.values() if p.speaker.has_media()]
        if not satellite and len(live) == 1:
            return live[0], ""
        return None, ("Which room's speaker? " if live else "I can't reach a speaker right now. ") + self._rooms_say()

    # ---- what the router asks for ----------------------------------------------------------------
    def describe(self, path: str) -> tuple[str, str]:
        """(title, artist) from the index row (folder names when its tags aren't read yet)."""
        try:
            with ops.Lib(self.lib_cfg) as lib:
                r = lib.row_for(path)
        except Exception:
            r = {}
        title = r.get("title") or os.path.splitext(os.path.basename(path))[0]
        return title, (r.get("artist") or r.get("main_artist") or "")

    def whats_playing(self, sp: SpeakerPlayer) -> dict:
        p = sp.current_file()
        if not p:
            return {"ok": True, "say": f"Nothing's playing on {sp.name}."}
        title, who = self.describe(p)
        say = f"This is {title} by {who}" if who else f"This is {title}"
        return {"ok": True, "say": say + (" — it's paused." if sp.paused else "."), "path": p}

    def play_song(self, sp: SpeakerPlayer, path: str) -> dict:
        """One song (a title search hit) on this speaker."""
        title, who = self.describe(path)
        what = f"{title} by {who}" if who else title
        with player.use(sp):
            r = playlists.play([path], what, self.lib_cfg)
        if not r.get("ok"):
            return r
        return {"ok": True, "say": f"Playing {what}.", "kind": "song", "path": path}

    def transport(self, sp: SpeakerPlayer, op: str, arg=None) -> dict:
        """pause / stop / resume / next / previous / volume / louder / quieter / mute / unmute / what, on `sp`."""
        if op == "what":
            return self.whats_playing(sp)
        if op == "pause":
            if sp.pause():
                return {"ok": True, "say": "Paused."}
            return {"ok": True, "say": "It's already paused." if sp.has_music() else f"Nothing's playing on {sp.name}."}
        if op == "stop":
            return {"ok": True, "say": "Stopped." if sp.stop() else f"Nothing's playing on {sp.name}."}
        if op == "resume":
            if sp.has_music() and not sp.paused:
                return {"ok": True, "say": "It's already playing."}
            if sp.resume():
                return {"ok": True, "say": "Resuming."}
            return {"ok": False, "say": f"There's nothing paused on {sp.name} — tell me what to play."}
        if op == "next":
            return sp.next()
        if op == "previous":
            return sp.previous()
        if op == "volume":
            v = sp.set_volume(arg)
            return {"ok": True, "say": f"Volume {round(v * 100)} percent."}
        if op in ("louder", "quieter"):
            v = sp.louder() if op == "louder" else sp.quieter()
            return {"ok": True, "say": f"{'Louder' if op == 'louder' else 'Quieter'} — {round(v * 100)} percent."}
        if op in ("mute", "unmute"):
            sp.mute(op == "mute")
            return {"ok": True, "say": "Muted." if op == "mute" else "Unmuted."}
        return {"ok": False, "say": "I don't know that one."}
