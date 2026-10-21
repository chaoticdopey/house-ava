"""The watchdog rebuilds a stuck Echo connection on the FIRST strike (no gentle-nudge stage), leaves healthy and
freshly-down Echoes alone, and never throws. Fake clients, no network.

    .venv/Scripts/python.exe tests/test_watchdog.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="avawd-")
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from houseava.satellite import Satellite, SatelliteHub   # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


class Cfg:
    def get(self, k, d=None):
        return d


class FakeClient:
    def __init__(self):
        self.disconnects = []

    async def disconnect(self, force=False):
        self.disconnects.append(force)


class FakeLogic:
    def __init__(self):
        self.stopped = 0

    async def stop(self):
        self.stopped += 1


class HangingClient(FakeClient):
    async def disconnect(self, force=False):          # a disconnect that never returns (the stuck handshake)
        await asyncio.sleep(3600)


async def main():
    hub = SatelliteHub(Cfg(), router=object(), transcribe=lambda p: "", synth=lambda t: b"", replies=None)
    rebuilt = []

    async def fake_connect(sat):
        rebuilt.append(sat.name)
    hub._connect = fake_connect

    now = time.time()
    healthy = Satellite(hub, {"name": "healthy", "ip": "127.0.0.1", "room": "a"})
    healthy.client, healthy.logic, healthy.connected, healthy.last_change = FakeClient(), FakeLogic(), True, now - 600
    fresh = Satellite(hub, {"name": "fresh-down", "ip": "127.0.0.1", "room": "b"})
    fresh.client, fresh.logic, fresh.connected, fresh.last_change = FakeClient(), FakeLogic(), False, now - 30
    stuck = Satellite(hub, {"name": "stuck", "ip": "127.0.0.1", "room": "c"})
    stuck.client, stuck.logic, stuck.connected, stuck.last_change = FakeClient(), FakeLogic(), False, now - 120
    hub.sats = [healthy, fresh, stuck]

    await hub._watchdog_tick(now, stuck_after=60.0)
    check("first strike (down 120 s) -> rebuilt straight away, no nudge stage", rebuilt == ["stuck"], rebuilt)
    check("... old ReconnectLogic stopped + old client force-dropped first", stuck.logic.stopped == 1 and stuck.client.disconnects == [True],
          (stuck.logic.stopped, stuck.client.disconnects))
    check("... last_change reset so it isn't hammered", abs(stuck.last_change - now) < 0.01)
    check("a healthy Echo is left alone", healthy.logic.stopped == 0 and not healthy.client.disconnects)
    check("an Echo down only 30 s is left alone", fresh.logic.stopped == 0 and not fresh.client.disconnects)

    rebuilt.clear()
    fresh.connected = True                            # (it came back on its own - only "stuck" is still down)
    await hub._watchdog_tick(now + 30, stuck_after=60.0)
    check("30 s after a rebuild: not rebuilt again yet", rebuilt == [], rebuilt)
    await hub._watchdog_tick(now + 61, stuck_after=60.0)
    check("still down 61 s after the rebuild: rebuilt again (the Echo was still booting)", rebuilt == ["stuck"], rebuilt)

    # a connect that raises must not kill the watchdog
    async def bad_connect(sat):
        raise RuntimeError("boom")
    hub._connect = bad_connect
    stuck.last_change = now - 500
    try:
        await hub._watchdog_tick(now, stuck_after=60.0)
        check("a failing rebuild is logged, not raised", True)
    except Exception as exc:
        check("a failing rebuild is logged, not raised", False, exc)

    # an old client whose disconnect hangs is given up on after the timeout, and the rebuild still happens
    hub._connect = fake_connect
    rebuilt.clear()
    stuck.client, stuck.last_change = HangingClient(), now - 500
    t0 = time.time()
    orig_wait_for = asyncio.wait_for

    async def quick_wait_for(aw, timeout):            # shrink the 10 s give-up to keep the test fast
        return await orig_wait_for(aw, 0.05)
    asyncio.wait_for = quick_wait_for
    try:
        await hub._watchdog_tick(now, stuck_after=60.0)
    finally:
        asyncio.wait_for = orig_wait_for
    check("a hanging old disconnect is abandoned and the rebuild proceeds", rebuilt == ["stuck"] and time.time() - t0 < 2, (rebuilt, time.time() - t0))


asyncio.run(main())
print(f"\n{len(fails)} failure(s)" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
