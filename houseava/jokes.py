"""Dirty jokes for House Ava's adult mode - a built-in bank, no model call.

The stock brain (qwen2.5:14b) refuses "tell me a dirty joke" and swaps in a clean one, and the only uncensored
model on a 12 GB card is another 14B that can't share the 12 GB card with it (every joke would swap models, ~10 s).
So the dirty ones come from here: instant, never refused, and no repeats until the whole bank's been told
(remembered across restarts in jokes_used.json). Adult and raunchy, never hateful: no slurs, nothing aimed at
who a person is. Plain "tell me a joke" still goes to the brain.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

_BANK = [
    "Why does Santa have such a big sack? Because he only comes once a year.",
    "What's the difference between a G-spot and a golf ball? A guy will actually spend twenty minutes looking "
    "for the golf ball.",
    "Why did the sperm cross the road? Because I put on the wrong sock this morning.",
    "What do a Rubik's cube and a penis have in common? The longer you play with them, the harder they get.",
    "What's the difference between your wife and your job? After five years, the job still sucks.",
    "I asked my wife to try anal. She said, 'Sure - as soon as you try having a job.'",
    "What do you call a guy with a small dick? Justin.",
    "Why do walruses go to Tupperware parties? To find a tight seal.",
    "How is sex like a game of bridge? If you've got a good hand, you don't need a partner.",
    "What's the difference between a tire and 365 used condoms? One's a Goodyear, the other's a great year.",
    "What did the penis say to the condom? Cover me, I'm going in.",
    "Why do vegetarians give good head? Because they're used to eating nuts.",
    "What's the best thing about a blowjob? Ten minutes of silence.",
    "What's long, hard, and full of seamen? A submarine.",
    "Why is it called a wet dream? Because you're the only one that comes.",
    "What's the difference between a light bulb and a pregnant woman? You can unscrew a light bulb.",
    "Why did the banana go to the doctor? It wasn't peeling well. Why did the cucumber go? It heard the "
    "doctor was into produce.",
    "What do you call the useless piece of skin on a dick? The man.",
    "What's the difference between a hooker and a drug dealer? A hooker can wash her crack and sell it again.",
    "Why doesn't a chicken wear underwear? Because its pecker is on its face.",
    "What's the difference between oral sex and anal sex? Oral sex makes your whole day, anal makes your "
    "hole weak.",
    "What do boobs and toys have in common? They were both made for kids but dad ends up playing with them.",
    "What's worse than waking up at a party with a dick drawn on your face? Finding out it was traced.",
    "What do a bungee jump and a hooker have in common? They're both cheap, fast, and if the rubber breaks "
    "you're pretty much fucked.",
    "Why did the woman leave the strip club with a hundred dollars? She'd gone in with two hundred.",
    "What does a 75-year-old woman have between her breasts that a 25-year-old doesn't? Her belly button.",
    "How do you make your girlfriend scream during sex? Call her and tell her.",
    "What's the difference between a pickpocket and a peeping Tom? One snatches your watch, the other "
    "watches your snatch.",
]


# ---- the four categories (regular, dirty, adult and morbid; racist is the one kind she won't do) ----
# Pools are APPEND-ONLY: the never-repeat history stores positions. _BANK stays first in "dirty" so the old
# jokes_used.json (a plain list of _BANK positions) still means the same jokes.
ADULT_ONLY = {"dirty", "adult", "morbid"}


def _norm(t: str) -> str:
    return " ".join((t or "").split()).strip().lower()


def _build() -> dict:
    from . import joke_bank as jb
    from . import joke_bank_extra as jx
    raw = {
        "regular": list(jb.REGULAR) + list(jx.REGULAR_EXTRA),
        "dirty": [("dirty", t) for t in _BANK] + [("dirty", t) for t in jb.DIRTY]
        + [("dirty", t) for t in getattr(jx, "DIRTY_EXTRA", [])],
        "adult": list(jb.ADULT) + list(getattr(jx, "ADULT_EXTRA", [])),
        "morbid": [("morbid", t) for t in list(jb.MORBID) + list(jx.MORBID_EXTRA)],
    }
    out = {}
    for cat, pool in raw.items():
        seen, keep = set(), []
        for kind, text in pool:
            if _norm(text) not in seen:
                seen.add(_norm(text))
                keep.append((kind, text))
        out[cat] = keep
    return out


CATS = _build()
_TEXT = {_norm(t): c for c, pool in CATS.items() for _, t in pool}


def counts() -> dict:
    return {c: len(p) for c, p in CATS.items()}


def in_pool(text: str, cat: str | None = None) -> bool:
    c = _TEXT.get(_norm(text))
    return c is not None and (cat is None or c == cat)


def _used_file() -> Path:
    from .config import data_dir
    return data_dir() / "jokes_used.json"


def _load_used() -> dict:
    try:
        d = json.loads(_used_file().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(d, list):                       # the 10-15 single-bank format = dirty positions
        return {"dirty": [int(i) for i in d]}
    return {c: [int(i) for i in v] for c, v in d.items() if isinstance(v, list)}


def _save_used(used: dict) -> None:
    try:
        f = _used_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({c: sorted(set(v)) for c, v in used.items()}), encoding="utf-8")
    except Exception:
        pass


def told_last(reply: str) -> str | None:
    """Which category her last reply came from, if it was a bank joke ("another one" = another of those)."""
    return _TEXT.get(_norm(reply))


def tell(cfg, cat: str = "regular", kind: str | None = None) -> dict | None:
    """One joke from `cat` (optionally of `kind`) she hasn't told yet; the category starts over once all are used.
    None = nothing of that kind (the brain makes one up instead)."""
    if cat in ADULT_ONLY and not bool(cfg.get("personality.adult", True)):
        return {"ok": True, "say": "Those jokes are switched off in my settings."}
    pool = CATS.get(cat) or []
    idxs = [i for i, (k, _) in enumerate(pool) if kind is None or k == kind]
    if not idxs:
        return None
    used = _load_used()
    seen = set(used.get(cat, []))
    left = [i for i in idxs if i not in seen]
    if not left:
        seen -= set(idxs)
        left = idxs
    i = random.choice(left)
    seen.add(i)
    used[cat] = sorted(seen)
    _save_used(used)
    return {"ok": True, "say": pool[i][1]}


def dirty(cfg) -> dict:
    """One dirty joke she hasn't told yet (adult mode only)."""
    return tell(cfg, "dirty")
