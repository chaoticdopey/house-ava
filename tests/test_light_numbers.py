"""Single bulbs by number: "bedroom light one", "bedroom light number two", "one bedroom light" - and the whole room
still works. No network: the WiZ send is recorded, not sent.

    .venv/Scripts/python.exe tests/test_light_numbers.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="avalt-")

import houseava.lights as L                      # noqa: E402
from houseava.app import build_router            # noqa: E402
from houseava.config import Config               # noqa: E402

fails, sent = [], []
L._send = lambda ip, payload, timeout=1.2: (sent.append((ip, payload)) or True)


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


BED1, BED2 = "192.0.2.105", "192.0.2.106"
cfg = Config()
cfg.set("lights.bulbs", {"living room 1": "192.0.2.101", "living room 2": "192.0.2.102",
                         "dining room 1": "192.0.2.103", "dining room 2": "192.0.2.104",
                         "bedroom 1": BED1, "bedroom 2": BED2})
cfg.set("lights.rooms", {"living room": ["living room 1", "living room 2"],
                         "dining room": ["dining room 1", "dining room 2"],
                         "bedroom": ["bedroom 1", "bedroom 2"]})
r = build_router(cfg)


def say(text, room="kitchen"):
    sent.clear()
    res = r.handle(text, room, satellite="Kitchen Echo", heard="wake")
    return res, sorted({ip for ip, _ in sent}), [p["params"] for _, p in sent]


res, ips, params = say("turn on bedroom light one")
check("'bedroom light one' -> only bedroom 1, on", ips == [BED1] and params[0]["state"] is True, (ips, params, res))
res, ips, params = say("turn off bedroom light number two")
check("'bedroom light number two' -> only bedroom 2, off", ips == [BED2] and params[0]["state"] is False, (ips, res))
res, ips, params = say("turn on bedroom light 2")
check("'bedroom light 2' (a digit) -> bedroom 2", ips == [BED2], ips)
res, ips, params = say("only turn on one bedroom light")
check("'only turn on one bedroom light' -> exactly one bedroom bulb", len(ips) == 1 and ips[0] in (BED1, BED2), (ips, res))
res, ips, params = say("turn on the bedroom lights")
check("'the bedroom lights' -> both", ips == [BED1, BED2], ips)
res, ips, params = say("turn on light two", room="bedroom")
check("'light two' said in the bedroom -> bedroom 2", ips == [BED2], (ips, res))
res, ips, params = say("dim bedroom light two to 40 percent")
check("'dim bedroom light two to 40 percent' -> bedroom 2 at 40", ips == [BED2] and params[0].get("dimming") == 40, (ips, params))
res, ips, params = say("bedroom light one off")
check("'bedroom light one off' (name-first form) -> bedroom 1", ips == [BED1] and params[0]["state"] is False, (ips, res))
res, ips, params = say("turn off the living room and dining room lights")
check("compound rooms still work", ips == ["192.0.2.101", "192.0.2.102", "192.0.2.103", "192.0.2.104"], ips)
res, ips, params = say("turn on the lights", room="bedroom")
check("'the lights' in the bedroom -> both bedroom bulbs", ips == [BED1, BED2], ips)
# review findings 10-15: whisper's comma, number lists, count in the name-first order
res, ips, params = say("turn on bedroom light, number one")
check("'bedroom light, number one' (whisper's comma) -> only bedroom 1", ips == [BED1], (ips, res))
res, ips, params = say("turn off bedroom lights one and two")
check("'bedroom lights one and two' -> both", ips == [BED1, BED2] and all(p["state"] is False for p in params), (ips, res))
res, ips, params = say("turn one bedroom light on")
check("'turn one bedroom light on' -> exactly one bedroom bulb", len(ips) == 1 and ips[0] in (BED1, BED2), (ips, res))
res, ips, params = say("dim bedroom light, number two to 40 percent")
check("'dim bedroom light, number two to 40 percent' -> bedroom 2", ips == [BED2] and params[0].get("dimming") == 40, (ips, params))

print(f"\n{len(fails)} failure(s)" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
