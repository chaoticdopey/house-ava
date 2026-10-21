""""Connect my phone" = an already-paired phone reconnects (echod's reconnect-phone button), "pair my phone" = pairing
mode; an Echo whose firmware can't take a phone says so. Fake Echo, no network.

    .venv/Scripts/python.exe tests/test_bt_connect.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="avabt-")
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from houseava.satellite import Satellite, SatelliteHub   # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


class FakeBT:
    def __init__(self):
        self.calls = []

    def __call__(self, action, satellite="", room="", named=False):
        self.calls.append((action, room, named))
        return {"ok": True, "say": action}


def router_tests():
    from houseava.app import build_router
    from houseava.config import Config
    r = build_router(Config())
    fake = FakeBT()
    r.bluetooth = fake
    for text, want in (("connect my phone", "connect"), ("reconnect my phone", "connect"), ("hook my phone back up", "connect"),
                       ("Alexa, connect my phone please", "connect"), ("connect my phone to you", "connect"),
                       ("reconnect the bluetooth", "connect"), ("pair my phone", "pair"), ("turn on bluetooth pairing", "pair"),
                       ("disconnect my phone", "disconnect"), ("stop pairing", "stop")):
        fake.calls.clear()
        r.handle(text, "kitchen", satellite="Kitchen Echo", heard="wake")
        got = fake.calls[-1][0] if fake.calls else None
        check(f"{text!r} -> {want}", got == want, got)
    fake.calls.clear()
    r.handle("connect my phone in the bedroom", "kitchen", satellite="Kitchen Echo", heard="wake")
    check("'connect my phone in the bedroom' names the room", fake.calls and fake.calls[-1] == ("connect", "bedroom", True), fake.calls)


class Cfg:
    def get(self, k, d=None):
        return d


class FakeClient:
    def __init__(self):
        self.buttons, self.switches = [], []

    def button_command(self, key):
        self.buttons.append(key)

    def switch_command(self, key, on):
        self.switches.append((key, on))


async def hub_tests():
    hub = SatelliteHub(Cfg(), router=object(), transcribe=lambda p: "", synth=lambda t: b"", replies=None)
    hub._loop = asyncio.get_running_loop()
    k = Satellite(hub, {"name": "kitchen-echo", "ip": "127.0.0.1", "room": "kitchen"})
    k.client, k.connected = FakeClient(), True
    k.bt_pair_key, k.bt_phone_ok, k.bt_reconnect_key = 5, True, 9
    b = Satellite(hub, {"name": "bedroom-echo", "ip": "127.0.0.1", "room": "bedroom"})
    b.client, b.connected = FakeClient(), True
    b.bt_pair_key, b.bt_phone_ok, b.bt_reconnect_key = 5, False, None       # a build with no phone support
    hub.sats = [k, b]

    r = hub.bluetooth_cmd("connect", satellite="kitchen-echo", room="kitchen")
    await asyncio.sleep(0.02)
    check("connect my phone presses the reconnect-phone button, no pairing mode", r["ok"] and k.client.buttons == [9]
          and not k.client.switches and "Reconnecting" in r["say"], (r, k.client.buttons, k.client.switches))
    r = hub.bluetooth_cmd("pair", satellite="kitchen-echo", room="kitchen")
    await asyncio.sleep(0.02)
    check("pair my phone turns pairing on (the phone-pairing switch)", r["ok"] and k.client.switches == [(5, True)], (r, k.client.switches))
    r = hub.bluetooth_cmd("connect", satellite="bedroom-echo", room="bedroom")
    check("an Echo without phone support says so instead of pairing earbuds", not r["ok"] and "firmware" in r["say"]
          and not b.client.switches and not b.client.buttons, r)
    r = hub.bluetooth_cmd("pair", satellite="bedroom-echo", room="bedroom")
    check("... for pair too", not r["ok"] and "firmware" in r["say"], r)


router_tests()
asyncio.run(hub_tests())
print(f"\n{len(fails)} failure(s)" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
