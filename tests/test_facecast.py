"""Ava's face on the Echo screen: which picture is used, and that the clock never runs over it.

Run on a box with Pillow and the DejaVu fonts (apt install python3-pil fonts-dejavu-core):
    python tests/test_facecast.py
"""
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from houseava import facecast  # noqa: E402
from houseava.facecast import FaceRenderer, face_pictures, face_source  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def picture(path, size, color, mode="RGB"):
    Image.new(mode, size, color).save(path)
    return path


def text_left_edge(jpeg):
    """The leftmost column holding a bright grey pixel. The test pictures are dark or red, so any
    bright grey pixel, over the picture or beside it, is the clock or the date."""
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    px = img.load()
    for x in range(w):
        for y in range(h):
            r, g, b = px[x, y]
            if r > 120 and g > 120 and b > 120:
                return x
    return w


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    # Which picture a source gives.
    single = picture(tmp / "dog.png", (800, 600), (200, 30, 30))
    check("a single picture is the whole face", face_pictures(single) == {"idle": single})
    check("a file that is not a picture gives nothing", face_pictures(tmp / "notes.txt") == {})

    pack = tmp / "pack"
    (pack / "frames").mkdir(parents=True)
    idle = picture(pack / "frames" / "idle.jpg", (300, 450), (40, 40, 60))
    blink = picture(pack / "frames" / "blink.jpg", (300, 450), (40, 40, 60))
    (pack / "face.json").write_text("{}")
    check("a pack folder finds its frames", face_pictures(pack) == {"idle": idle, "blink": blink})
    check("its frames folder works the same", face_pictures(pack / "frames") == {"idle": idle, "blink": blink})

    loose = tmp / "loose"
    loose.mkdir()
    b = picture(loose / "b.png", (400, 400), (0, 0, 0))
    picture(loose / "c.webp", (400, 400), (0, 0, 0))
    check("a folder with no idle uses its first picture", face_pictures(loose) == {"idle": b})
    check("a missing folder gives nothing", face_pictures(tmp / "nope") == {})

    # Where the face comes from.
    check("default is the ava pack", face_source(Cfg()) == facecast.FACES_DIR / "ava")
    check("a pack by name", face_source(Cfg({"facecast.face": "maya"})) == facecast.FACES_DIR / "maya")
    check("frames wins over face",
          face_source(Cfg({"facecast.face": "maya", "facecast.frames": str(single)})) == single)

    # A transparent PNG is flattened onto the dark background, not black.
    clear = picture(tmp / "clear.png", (200, 200), (255, 0, 0, 0), mode="RGBA")
    r = FaceRenderer(clear)
    check("a transparent picture sits on the dark background",
          r._src["idle"].getpixel((5, 5)) == (15, 15, 20), str(r._src["idle"].getpixel((5, 5))))

    # The clock never runs over the face, at the Show 5, Show 8 and odd sizes, with a portrait face,
    # a wide picture and a square one.
    for label, src in (("portrait pack", pack), ("wide picture", single), ("square picture", b)):
        r = FaceRenderer(src)
        for (w, h) in ((960, 480), (1280, 800), (1024, 600), (800, 480), (480, 272)):
            face = r._faces_for(w, h)["idle"]
            face_right = round(20 * w / 960) + face.size[0]
            check(f"{label} at {w}x{h}: picture fits its share", face.size[0] <= round(w * facecast._FACE_SHARE) + 1
                  and face.size[1] <= h, f"{face.size}")
            left = text_left_edge(r.render(0.0, w, h))
            check(f"{label} at {w}x{h}: clock right of the picture", left >= face_right, f"text at {left}, face to {face_right}")

    # The Show 5 layout is the one it was tuned on: same fonts as before at 960x480 for the Ava pack.
    real = facecast.FACES_DIR / "ava"
    if (real / "frames" / "idle.jpg").is_file():
        r = FaceRenderer(real)
        jpeg = r.render(0.0, 960, 480)
        check("the ava pack renders at 960x480", Image.open(io.BytesIO(jpeg)).size == (960, 480))

    # The app's face picker: the packs it lists, Ava first.
    packs = tmp / "faces"
    for pid, meta in (("zed", '{"name": "Zed", "gender": "male", "default_voice": "en-US-BrianNeural"}'),
                      ("ava", '{"name": "Ava", "default_voice": "en-US-AvaNeural"}'), ("broken", "{nope")):
        (packs / pid / "frames").mkdir(parents=True)
        picture(packs / pid / "frames" / "idle.jpg", (300, 450), (40, 40, 60))
        (packs / pid / "face.json").write_text(meta)
    (packs / "not-a-pack").mkdir()
    real_dir, facecast.FACES_DIR = facecast.FACES_DIR, packs
    try:
        got = facecast.list_faces()
        check("the picker lists the packs, Ava first", [f["id"] for f in got] == ["ava", "broken", "zed"], str(got))
        check("... with each one's name and voice",
              got[2] == {"id": "zed", "name": "Zed", "gender": "male", "voice": "en-US-BrianNeural"}, str(got[2]))

        # A face picked in the app shows on the next frame, no restart.
        cfg = Cfg({"facecast.key": "k"})
        srv = facecast.FaceCastServer(cfg)
        srv._renderer = FaceRenderer(srv.frames_dir)
        first = srv.renderer()
        check("the same face keeps the same renderer", srv.renderer() is first and srv.frames_dir == packs / "ava")
        cfg["facecast.face"] = "zed"
        check("picking another pack switches to it", srv.renderer() is not first and srv.frames_dir == packs / "zed")
        cfg["facecast.frames"] = str(single)
        check("your own picture wins over the pack", srv.frames_dir != single and srv.renderer() and srv.frames_dir == single)
        jpeg = srv.renderer().render(0.0, 960, 480)
        check("... and renders", Image.open(io.BytesIO(jpeg)).size == (960, 480))

        # A picture that can't be shown (a typo'd path, an empty folder) keeps the face that's up.
        shown = srv.renderer()
        cfg["facecast.frames"] = str(tmp / "typo.png")
        check("a typo'd picture path keeps the face that's up", srv.renderer() is shown and srv.frames_dir == single)
        empty = tmp / "empty"
        empty.mkdir()
        cfg["facecast.frames"] = str(empty)
        check("an empty folder keeps the face that's up", srv.renderer() is shown and srv.frames_dir == single)
        # ... and once the picture is there it's picked up after the retry wait, no restart
        picture(empty / "idle.png", (300, 450), (40, 40, 60))
        check("... not retried on every frame", srv.renderer() is shown)
        srv._tried = (srv._tried[0], srv._tried[1] - facecast._RETRY_S - 1)
        check("... picked up once it's there", srv.renderer() is not shown and srv.frames_dir == empty)

        # Nothing to show at start: the pack it names, else Ava.
        bad = facecast.FaceCastServer(Cfg({"facecast.key": "k", "facecast.port": 0, "facecast.face": "zed",
                                           "facecast.frames": str(tmp / "nope.jpg")}))
        bad.start()
        check("a bad picture at start shows the pack it names", bad.frames_dir == packs / "zed"
              and "idle" in bad.renderer()._src, str(bad.frames_dir))
        bad._srv.close()
        worse = facecast.FaceCastServer(Cfg({"facecast.key": "k", "facecast.port": 0, "facecast.face": "gone"}))
        worse.start()
        check("... and a pack that isn't there falls back to Ava", worse.frames_dir == packs / "ava", str(worse.frames_dir))
        worse._srv.close()
    finally:
        facecast.FACES_DIR = real_dir

# The app's config: the face list comes along, the face stream key never does, and neither can be saved.
import json  # noqa: E402
import os  # noqa: E402
import urllib.request  # noqa: E402

with tempfile.TemporaryDirectory() as home:
    os.environ["HOUSEAVA_DIR"] = home
    from houseava.admin import AdminServer  # noqa: E402
    from houseava.config import Config  # noqa: E402
    acfg = Config(path=Path(home) / "cfg.json")
    acfg.set("admin.port", 0)
    acfg.set("admin.pin", "2468")
    acfg.set("facecast.key", "secret-stream-key")
    adm = AdminServer(acfg)
    adm.start()

    def api(method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{adm.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Pin": "2468", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read() or b"{}")

    try:
        d = api("GET", "/api/config")
        check("the app gets the face list", any(f["id"] == "ava" for f in d.get("_faces", [])), str(d.get("_faces")))
        check("the app never sees the face stream key", "key" not in d.get("facecast", {}), str(d.get("facecast")))
        api("PATCH", "/api/config", {"facecast.face": "maya", "facecast.frames": "", "_faces": [1],
                                     "facecast.key": "from-the-app"})
        check("the face picked in the app is saved", acfg.get("facecast.face") == "maya")
        check("the face list isn't saved into the config", "_faces" not in acfg.data)
        check("the app can't change the stream key", acfg.get("facecast.key") == "secret-stream-key")

        # Wrong PINs: after 5 in a row that address waits (the right PIN too), and the app's empty probe doesn't count.
        def login(p):
            req = urllib.request.Request(f"http://127.0.0.1:{adm.port}/api/auth", method="POST",
                                         data=json.dumps({"pin": p}).encode(), headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        admin_mod = sys.modules["houseava.admin"]
        admin_mod._MISSES.clear()
        check("the empty-PIN probe doesn't count as a guess", [login("") for _ in range(8)] == [401] * 8
              and not admin_mod._MISSES, str(admin_mod._MISSES))
        codes = [login("1111") for _ in range(5)]
        check("4 wrong PINs are just 'wrong'; the 5th starts the wait", codes == [401] * 4 + [429], codes)
        check("... then that address has to wait", login("1111") == 429 and login("2468") == 429)
        for ip, (n, _) in list(admin_mod._MISSES.items()):
            admin_mod._MISSES[ip] = (n, 0.0)        # (the wait's over)
        check("... after the wait the right PIN works and clears it", login("2468") == 200 and not admin_mod._MISSES)
    finally:
        adm.stop()

    # A --port for one run isn't written into the config.
    pcfg = Config(path=Path(home) / "port.json")
    pcfg.set("admin.pin", "2468")
    padm = AdminServer(pcfg, port=18799)
    check("a port given to the server is used, and isn't put in the config",
          padm.port == 18799 and pcfg.get("admin.port") == 8792, (padm.port, pcfg.get("admin.port")))
    del padm

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
