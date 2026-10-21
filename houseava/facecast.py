"""Ava's face on the Echo's screen — a dashcast streaming server.

echod can stream its screen from a server instead of drawing a Home Assistant dashboard ("streamed"
dashboard mode). It connects out to us, does a Noise NNpsk0 handshake (prologue "techo5-dashcast/1",
PSK = sha256("techo5-dashcast psk:" + key)), sends a JSON hello + touches, and we push 960x480 JPEG
frames. We render Ava's face (the same picture-swap frames as her desktop face) with a clock, so the
idle screen is HER, not a blank clock. During a voice turn echod takes the screen back for its own
Listening/Thinking UI; the face returns when idle.

Wire format (per docs/RESEARCH): on the wire every Noise record is [4-byte BE length][ciphertext],
<=60000 bytes plaintext each. Through that encrypted channel: the device sends newline JSON lines; we
send application messages [4-byte BE length][kind][payload] — kind 1 = JPEG at x,y (uint16 BE).
"""

from __future__ import annotations

import hashlib
import secrets
import io
import json
import logging
import socket
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("houseava.facecast")

_PROLOGUE = b"techo5-dashcast/1"
_RECORD_MAX = 60000
_W, _H = 960, 480
_KIND_JPEG = 1


def _psk(key: str) -> bytes:
    return hashlib.sha256(b"techo5-dashcast psk:" + key.encode()).digest()


# ---- which face ---------------------------------------------------------------------------------
# The face packs that ship with House Ava live in faces/<name>/ (face.json + frames/). Any other
# picture works too: point facecast.frames at a pack folder, a folder of pictures, or one picture.
FACES_DIR = Path(__file__).resolve().parent.parent / "faces"
_PICTURE_TYPES = (".jpg", ".jpeg", ".png", ".webp")
# The widest a picture may be, as a share of the screen: the clock keeps the rest.
_FACE_SHARE = 0.45
# A face that couldn't be shown is tried again after this long (the picture may have been copied in since).
_RETRY_S = 30.0


def face_source(cfg) -> Path:
    """Where the face comes from: facecast.frames (a picture or a folder) if set, else the pack named in
    facecast.face (default "ava")."""
    frames = str(cfg.get("facecast.frames", "") or "").strip()
    if frames:
        return Path(frames).expanduser()
    name = str(cfg.get("facecast.face", "") or "ava").strip() or "ava"
    return FACES_DIR / name


def list_faces() -> list[dict]:
    """The face packs in faces/, for the app's picker: [{"id", "name", "gender", "voice"}], Ava first."""
    out = []
    try:
        packs = sorted(p for p in FACES_DIR.iterdir() if (p / "face.json").is_file())
    except OSError:
        return out
    for p in packs:
        try:
            meta = json.loads((p / "face.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        out.append({"id": p.name, "name": str(meta.get("name") or p.name), "gender": str(meta.get("gender") or ""),
                    "voice": str(meta.get("default_voice") or "")})
    out.sort(key=lambda f: (f["id"] != "ava", f["name"].lower()))
    return out


def face_pictures(src: Path) -> dict[str, Path]:
    """The idle picture (and a blink one, if there is one) found at src.

    src may be one picture, which is then the whole face; a face pack (face.json beside frames/); its
    frames/ folder; or any folder of pictures, where idle.* and blink.* are used when they are there
    and otherwise the first picture by name is the face."""
    src = Path(src)
    if src.is_file():
        return {"idle": src} if src.suffix.lower() in _PICTURE_TYPES else {}
    if (src / "frames").is_dir():
        src = src / "frames"
    if not src.is_dir():
        return {}
    pics = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() in _PICTURE_TYPES)
    out = {}
    for name in ("idle", "blink"):
        for p in pics:
            if p.stem.lower() == name:
                out[name] = p
                break
    if "idle" not in out and pics:
        out["idle"] = pics[0]
    return out


def _flatten(img):
    """A picture as plain RGB on the dark background, so a transparent PNG doesn't come out black-edged."""
    from PIL import Image
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (15, 15, 20))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


# ---- the face renderer -------------------------------------------------------------------------
class FaceRenderer:
    """Composites Ava's face + a clock onto a frame at the Echo's OWN screen size.

    The layout was tuned for a 960x480 Show 5 ("checkers"); everything scales proportionally
    off that baseline so a Show 8 ("crown", 1280x800) or any other panel renders correctly
    instead of a tiny face in the top-left corner. Scaled faces + fonts are cached per size,
    so the common case (one or two fixed-size Echoes) does the resize work only once."""

    def __init__(self, frames_dir: Path):
        from PIL import Image, ImageFont
        self._Image = Image
        self._ImageFont = ImageFont
        self.frames_dir = Path(frames_dir)
        self._src: dict[str, "Image.Image"] = {}         # unscaled source faces
        for name, p in face_pictures(self.frames_dir).items():
            try:
                self._src[name] = _flatten(Image.open(p))
            except Exception:
                log.warning("face renderer: %s is not a picture it can read", p)
        log.info("face renderer: %d frame(s) from %s", len(self._src), self.frames_dir)
        self._faces_cache: dict = {}                      # (w,h) -> {name: scaled Image}
        self._font_cache: dict = {}                       # h -> (font_clock, font_date)
        self._lock = threading.Lock()
        self._last_blink = 0.0

    def _font(self, size):
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try:
                return self._ImageFont.truetype(path, int(size))
            except Exception:
                continue
        try:
            return self._ImageFont.load_default(int(size))     # no DejaVu: Pillow 10.1+ scales its own font
        except TypeError:
            return self._ImageFont.load_default()

    def _fonts_for(self, w: int, h: int, room: int):
        """The clock and date fonts for a screen, and the scale they were picked at.

        Laid out for 960x480. Scaled by the height alone, the text outgrows a screen that is taller for
        its width (a Show 8 is 1280x800) and runs over the face, so it takes the smaller of the two
        scales, and smaller again if the widest clock or date still would not fit in ``room``, the space
        right of the face."""
        with self._lock:
            got = self._font_cache.get((w, h))
            if got is None:
                sc = min(w / _W, h / _H)
                for _ in range(2):
                    fonts = (self._font(max(12, round(168 * sc))), self._font(max(8, round(58 * sc))))
                    widest = max(self._width(fonts[0], "12:59"), self._width(fonts[1], "Wednesday, Sep 30"))
                    if widest <= room or widest <= 0:
                        break
                    sc *= room / widest
                got = (fonts[0], fonts[1], sc)
                self._font_cache[(w, h)] = got
            return got

    @staticmethod
    def _width(font, text: str) -> float:
        try:
            return float(font.getlength(text))
        except Exception:
            return 0.0

    def _faces_for(self, w: int, h: int):
        """The pictures scaled for a screen: as tall as it is, less a margin, and no wider than
        _FACE_SHARE of it, so a wide picture of anything still leaves the clock its side."""
        with self._lock:
            out = self._faces_cache.get((w, h))
            if out is None:
                box_h = max(1, h - round(20 * h / _H))
                box_w = max(1, round(w * _FACE_SHARE))
                out = {}
                for name, img in self._src.items():
                    sw, sh = img.size
                    k = min(box_h / sh, box_w / sw)
                    out[name] = img.resize((max(1, round(sw * k)), max(1, round(sh * k))), self._Image.LANCZOS)
                self._faces_cache[(w, h)] = out
            return out

    def render(self, now: float, w: int = _W, h: int = _H) -> bytes:
        from PIL import Image, ImageDraw
        w = max(1, int(w))
        h = max(1, int(h))
        sx, sy = w / _W, h / _H
        canvas = Image.new("RGB", (w, h), (15, 15, 20))     # Ava's dark theme
        # face on the left, blinking now and then
        faces = self._faces_for(w, h)
        blinking = (now - self._last_blink) > 3.6 and (now % 3.6) < 0.14
        face = faces.get("blink" if (blinking and "blink" in faces) else "idle")
        face_x = round(20 * sx)
        face_right = face_x
        if face is not None:
            canvas.paste(face, (face_x, (h - face.size[1]) // 2))
            face_right = face_x + face.size[0]
        # clock + date on the right, the block centered on the screen's middle as it is at 960x480
        d = ImageDraw.Draw(canvas)
        rx = w - round(36 * sx)
        font_clock, font_date, sc = self._fonts_for(w, h, rx - face_right - round(24 * sx))
        dt = datetime.now()
        clock = dt.strftime("%I:%M").lstrip("0")
        ampm = dt.strftime("%p")
        date = dt.strftime("%A, %b %d").replace(" 0", " ")
        mid = h / 2
        d.text((rx, round(mid + (200 - _H / 2) * sc)), clock, font=font_clock, fill=(232, 232, 240), anchor="rs")
        d.text((rx, round(mid + (262 - _H / 2) * sc)), ampm, font=font_date, fill=(167, 139, 250), anchor="rs")
        d.text((rx, round(mid + (352 - _H / 2) * sc)), date, font=font_date, fill=(150, 150, 172), anchor="rs")
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


# ---- the Noise-encrypted connection ------------------------------------------------------------
class _Secure:
    def __init__(self, sock: socket.socket, key: str):
        from noise.connection import NoiseConnection
        self.sock = sock
        self.nc = NoiseConnection.from_name(b"Noise_NNpsk0_25519_ChaChaPoly_SHA256")
        self.nc.set_as_responder()
        self.nc.set_prologue(_PROLOGUE)
        self.nc.set_psks(_psk(key))
        self.nc.start_handshake()

    def _read_frame(self) -> bytes:
        hdr = self._readn(4)
        (n,) = struct.unpack(">I", hdr)
        return self._readn(n)

    def _readn(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("closed")
            out += chunk
        return out

    def _write_frame(self, data: bytes) -> None:
        self.sock.sendall(struct.pack(">I", len(data)) + data)

    def handshake(self) -> None:
        # NNpsk0: initiator -> (psk,e); responder -> (e,ee). We are the responder: read, then write.
        self.nc.read_message(self._read_frame())
        self._write_frame(self.nc.write_message())
        if not self.nc.handshake_finished:
            raise ConnectionError("handshake did not finish")

    def recv_plain(self) -> bytes:
        return self.nc.decrypt(self._read_frame())

    def send_app(self, payload: bytes) -> None:
        """Send one application message, split into <=60000-byte encrypted records."""
        for i in range(0, len(payload), _RECORD_MAX):
            self._write_frame(self.nc.encrypt(payload[i:i + _RECORD_MAX]))


# ---- the server --------------------------------------------------------------------------------
class FaceCastServer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.port = int(cfg.get("facecast.port", 8793))
        # No key set = a fresh random one each start: the hub hands it to every Echo as it connects.
        self.key = str(cfg.get("facecast.key", "") or "") or secrets.token_urlsafe(16)
        self.frames_dir = face_source(cfg)
        self._renderer: FaceRenderer | None = None
        self._srv: socket.socket | None = None
        self._swap = threading.Lock()
        self._tried: tuple[Path, float] | None = None   # (a face that wouldn't load, when): retried every _RETRY_S

    @staticmethod
    def _load(src: Path) -> "FaceRenderer | None":
        """A renderer for src, or None when there's no resting picture there to show."""
        r = FaceRenderer(src)
        return r if "idle" in r._src else None

    def start(self) -> None:
        try:
            self._renderer = self._load(self.frames_dir)
            if self._renderer is None:
                # nothing to show where the config points (a typo'd picture, an empty folder): the pack it names,
                # else Ava, and the configured one is tried again now and then (a picture copied in later shows up)
                name = str(self.cfg.get("facecast.face", "") or "ava").strip() or "ava"
                for fallback in dict.fromkeys((FACES_DIR / name, FACES_DIR / "ava")):
                    self._renderer = self._load(fallback)
                    if self._renderer is not None:
                        break
                if self._renderer is None:
                    fallback = self.frames_dir
                    self._renderer = FaceRenderer(fallback)          # the clock on its own
                log.warning("facecast: no picture to show at %s; using %s for now", self.frames_dir, fallback)
                self._tried = (self.frames_dir, time.monotonic())
                self.frames_dir = fallback
        except Exception:
            log.exception("facecast off: the renderer didn't load (PIL/frames)")
            return
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("0.0.0.0", self.port))
        self._srv.listen(4)
        threading.Thread(target=self._accept_loop, daemon=True, name="facecast-accept").start()
        log.info("facecast server on port %s", self.port)

    def _waiting(self, want: Path) -> bool:
        return bool(self._tried) and self._tried[0] == want and time.monotonic() - self._tried[1] < _RETRY_S

    def renderer(self) -> "FaceRenderer | None":
        """The renderer for the face the config names NOW: a new face picked in the app shows on the next frame.
        One that can't be shown (a typo'd path, an empty folder) keeps the old face and is tried again every
        _RETRY_S, so copying the picture in afterwards works without a restart."""
        want = face_source(self.cfg)
        if want != self.frames_dir and not self._waiting(want):
            with self._swap:
                if want != self.frames_dir and not self._waiting(want):
                    fresh = None
                    try:
                        fresh = self._load(want)
                    except Exception:
                        log.exception("facecast: couldn't load the face at %s", want)
                    if fresh is None:
                        if not self._tried or self._tried[0] != want:
                            log.warning("facecast: no picture to show at %s; keeping %s", want, self.frames_dir)
                        self._tried = (want, time.monotonic())
                    else:
                        self._renderer, self.frames_dir, self._tried = fresh, want, None
                        log.info("facecast: face is now %s", want)
        return self._renderer

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, addr = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True, name="facecast-conn").start()

    def _serve(self, sock: socket.socket, addr) -> None:
        sock.settimeout(30)
        try:
            sec = _Secure(sock, self.key)
            sec.handshake()
            hello = sec.recv_plain()
            # the device's hello carries its screen size ({"w":..,"h":..}); render to THAT so the
            # face fills the panel (Show 5 = 960x480, Show 8 = 1280x800), not a fixed 960x480 corner.
            dw, dh = _W, _H
            try:
                line = hello.decode("utf-8", "replace").strip().splitlines()[0] if hello.strip() else "{}"
                hj = json.loads(line)
                if isinstance(hj, dict):
                    cw, ch = int(hj.get("w") or 0), int(hj.get("h") or 0)
                    if 16 <= cw <= 4096 and 16 <= ch <= 4096:
                        dw, dh = cw, ch
            except Exception:
                pass
            log.info("facecast: %s connected %dx%d (%s)", addr[0], dw, dh,
                     hello[:120].decode(errors="replace").strip())
            # drain the device's touch/hello lines in the background so recv never blocks our sends
            threading.Thread(target=self._drain, args=(sec,), daemon=True).start()
            while True:
                jpeg = self.renderer().render(time.time(), dw, dh)
                msg = struct.pack(">I", 5 + len(jpeg)) + bytes([_KIND_JPEG]) + struct.pack(">HH", 0, 0) + jpeg
                sec.send_app(msg)
                time.sleep(1.0)                     # ~1 fps: a clock + an occasional blink is plenty
        except Exception as exc:
            log.info("facecast: %s gone (%s)", addr[0], exc)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _drain(self, sec: "_Secure") -> None:
        try:
            while True:
                sec.recv_plain()                    # touches / pings; we don't act on them yet
        except Exception:
            pass
