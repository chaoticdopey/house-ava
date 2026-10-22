"""Find the WiZ bulbs on your network and print a lights config to start from.

    cd ~/houseava && .venv/bin/python -m houseava.find_bulbs
    cd ~/houseava && .venv/bin/python -m houseava.find_bulbs --blink 192.168.1.201    which one is that?

Finding only asks the bulbs who they are (a WiZ "getSystemConfig" broadcast on UDP 38899); nothing is switched.
Bulbs that are off at the wall switch can't answer, so turn everything on first. --blink flashes one bulb 3 times and
puts it back the way it was.
"""

from __future__ import annotations

import argparse
import json
import socket
import time

_PORT = 38899


def find(seconds: float = 3.0, broadcast: str = "255.255.255.255") -> dict[str, dict]:
    """{ip: {"mac", "module"}} for every WiZ bulb that answers within `seconds`."""
    ask = json.dumps({"method": "getSystemConfig", "params": {}}).encode()
    found: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        s.settimeout(0.25)
        end, next_ask = time.time() + seconds, 0.0
        while time.time() < end:
            if time.time() >= next_ask:                       # (asked a few times: UDP to a sleepy bulb can drop)
                s.sendto(ask, (broadcast, _PORT))
                next_ask = time.time() + 1.0
            try:
                data, (ip, _) = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue
            res = (msg.get("result") if isinstance(msg, dict) else None) or {}
            if isinstance(res, dict) and (res.get("mac") or res.get("moduleName")):
                found[ip] = {"mac": str(res.get("mac") or ""), "module": str(res.get("moduleName") or "")}
    return found


def blink(ip: str) -> bool:
    """Flash one bulb 3 times and put it back the way it was, so you can see which one it is."""
    from .lights import Lights, _send_all
    lights = Lights(None)
    before = lights._read([ip])
    if not before:
        return False
    for _ in range(3):
        _send_all([ip], lambda _: {"method": "setPilot", "params": {"state": True, "dimming": 100,
                                                                    "r": 255, "g": 255, "b": 255}}, 1)
        time.sleep(0.45)
        _send_all([ip], lambda _: {"method": "setPilot", "params": {"state": False}}, 1)
        time.sleep(0.45)
    lights._put_back(before)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find the WiZ bulbs on your network.")
    ap.add_argument("--seconds", type=float, default=3.0, help="how long to listen (default 3)")
    ap.add_argument("--broadcast", default="255.255.255.255", help="broadcast address (e.g. 192.168.1.255)")
    ap.add_argument("--blink", nargs="+", metavar="IP", help="flash these bulbs 3 times each, then put them back")
    a = ap.parse_args(argv)
    if a.blink:
        bad = 0
        for ip in a.blink:
            ok = blink(ip)
            print(f"{ip}: {'blinked' if ok else 'did not answer'}")
            bad += not ok
        return 1 if bad else 0
    found = find(a.seconds, a.broadcast)
    if not found:
        print("No WiZ bulbs answered. Are they on (wall switch too) and on the same network as this box?")
        return 1
    ips = sorted(found, key=lambda ip: tuple(int(p) for p in ip.split(".") if p.isdigit()))
    print(f"Found {len(ips)} WiZ bulb(s):")
    for ip in ips:
        print(f"  {ip:<16} mac {found[ip]['mac'] or '?':<14} {found[ip]['module']}")
    bulbs = {f"room {n}": ip for n, ip in enumerate(ips, 1)}
    print("\nPut this in place of the \"lights\" block in ~/.houseava/houseava_config.json (keep everything else).")
    print("Rename \"room 1\" etc. to \"<room> <number>\" (like \"kitchen 1\") and put each one in its room. To see")
    print(f"which bulb an address is:  .venv/bin/python -m houseava.find_bulbs --blink {ips[0]}\n")
    block = json.dumps({"bulbs": bulbs, "rooms": {"room": list(bulbs)}, "aliases": {}}, indent=2)
    print('  "lights": ' + block.replace("\n", "\n  ") + ",")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
