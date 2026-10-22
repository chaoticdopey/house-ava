"""The bulb finder: it hears a WiZ bulb answer, ignores junk, and --blink puts a bulb back. A fake bulb on this
machine answers; no real bulb is touched (HOUSEAVA_FAKE_BULBS covers --blink).

    .venv/Scripts/python.exe tests/test_find_bulbs.py
"""
import io
import json
import os
import socket
import sys
import threading
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from houseava import find_bulbs  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


# a fake bulb on 127.0.0.1:38899 that answers getSystemConfig, plus a "device" that answers with junk
srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    srv.bind(("127.0.0.1", find_bulbs._PORT))
except OSError as exc:
    print(f"[skip] port {find_bulbs._PORT} is busy here ({exc}); the finder test needs it")
    sys.exit(0)
srv.settimeout(0.2)
asked = []
stop = threading.Event()


def bulb():
    while not stop.is_set():
        try:
            data, addr = srv.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            return
        asked.append(json.loads(data))
        srv.sendto(b"not json", addr)
        srv.sendto(b"[1]", addr)                       # (JSON, but not an object)
        srv.sendto(b'"ok"', addr)
        srv.sendto(json.dumps({"method": "getSystemConfig", "env": "pro", "result": {
            "mac": "a8bb50aabbcc", "moduleName": "ESP25_SHRGB_01", "fwVersion": "1.31.0"}}).encode(), addr)


t = threading.Thread(target=bulb, daemon=True)
t.start()
try:
    got = find_bulbs.find(1.2, broadcast="127.0.0.1")
    check("finds the bulb that answers", got == {"127.0.0.1": {"mac": "a8bb50aabbcc", "module": "ESP25_SHRGB_01"}}, got)
    check("it only asked (getSystemConfig), nothing was switched",
          asked and all(a.get("method") == "getSystemConfig" for a in asked), asked)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = find_bulbs.main(["--seconds", "1.2", "--broadcast", "127.0.0.1"])
    text = out.getvalue()
    block = text[text.index('  "lights": '):].strip()
    cfg = json.loads("{" + block.rstrip(",") + "}")
    check("prints the lights block to put in the config", rc == 0 and block.endswith(",") and cfg == {
        "lights": {"bulbs": {"room 1": "127.0.0.1"}, "rooms": {"room": ["room 1"]}, "aliases": {}}}, text[-300:])
    check("... and how to tell them apart", "--blink 127.0.0.1" in text)
finally:
    stop.set()
    srv.close()

out = io.StringIO()
with redirect_stdout(out):
    rc = find_bulbs.main(["--seconds", "0.6", "--broadcast", "127.0.0.1"])
check("nobody answering says so", rc == 1 and "No WiZ bulbs answered" in out.getvalue(), out.getvalue())

out = io.StringIO()
with redirect_stdout(out):
    rc = find_bulbs.main(["--blink", "192.0.2.101"])
check("--blink flashes the bulb and puts it back (fake bulbs)", rc == 0 and "192.0.2.101: blinked" in out.getvalue(),
      out.getvalue())

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
