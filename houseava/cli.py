"""Talk to House Ava by typing — proves the whole brain half before any Echo exists.

    python -m houseava.cli --room bedroom

Type what you'd say out loud. She controls the real WiZ bulbs, Hubspace plugs (if set
up), answers questions, searches the web, remembers things — all in her own sassy voice.
"""

from __future__ import annotations

import argparse
import sys

from .app import build_router
from .config import Config


def main() -> int:
    ap = argparse.ArgumentParser(description="Talk to House Ava from the terminal.")
    ap.add_argument("--room", default="", help="pretend you're speaking to this room's unit (e.g. bedroom)")
    ap.add_argument("--once", default="", help="handle one line and exit")
    args = ap.parse_args()

    cfg = Config()
    router = build_router(cfg)
    name = cfg.get("name", "Ava")
    up, detail = router.brain.status()
    print(f"{name} (house) — room: {args.room or 'unknown'} — brain: {'ready' if up else detail}")
    print("Type to talk. Ctrl-C or 'quit' to stop.\n")

    if args.once:
        print(f"{name}: " + router.handle(args.once, args.room).get("say", ""))
        return 0

    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.lower() in ("quit", "exit", "bye"):
            return 0
        if not line:
            continue
        res = router.handle(line, args.room)
        print(f"{name}: {res.get('say', '')}\n")


if __name__ == "__main__":
    sys.exit(main())
