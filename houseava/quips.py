"""Personality-flavored confirmations for the quick actions (lights, plug), so House Ava has
character like desktop Ava instead of a flat "Turned the lights off."

Respects personality.cussing: on = she swears (an adult's own house assistant), off = just sassy.
Pure Python with random.choice — no LLM call, so the fast path stays instant. Profanity is for
flavor only; nothing here is cruel, hateful, or a slur.
"""

from __future__ import annotations

import random


def _cussing(cfg) -> bool:
    return bool(cfg.get("personality.cussing", False))


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# {what} = a label like "the living room lights" (templates without {what} just ignore it)
_LIGHTS_ON = ["Got {what} on.", "{what} are on.", "There you go.", "Let there be light.", "On they go."]
_LIGHTS_OFF = ["{what} are off.", "Lights out.", "Killed {what}.", "Done — {what} off.", "Darkness it is."]
_LIGHTS_ON_CUSS = ["Got {what} on. You're welcome.", "{what} are on, happy now?",
                   "There — fuckin' finally, {what} on.", "Lights on. Try not to trip.",
                   "{what} on. Anything else, your majesty?",
                   # (more adult lines for the lights)
                   "Lights on. Put some damn pants on.", "Let there be fucking light.",
                   "Got {what} on. Whatever you were doing in the dark, I don't want to know.",
                   "{what} on. Now I can see what a mess you are.", "Lights on, sunshine. Don't make it weird.",
                   "Fine, {what} on. Who the hell turned them off anyway?", "{what} on. You owe me one, asshole.",
                   "On. Happy now, you needy bastard?", "Got {what} on. Squint all you want, it's your own damn fault.",
                   "{what} on. Mood's ruined, sorry not sorry.", "There, {what} on. Go find whatever the hell you lost.",
                   "Lights on. Damn, you look rough.", "{what} on. Try not to look so goddamn surprised.",
                   "Lit. Unlike your love life.", "Lights on. Hide the evidence, I guess.",
                   "{what} on. Brighter than you, smartass."]
_LIGHTS_OFF_CUSS = ["Lights out, bitches.", "{what} off. Go the fuck to sleep.",
                    "Killed {what}. Darkness, you lazy ass.", "{what} are off. You're welcome, princess.",
                    "Done. Lights out, don't stub your damn toe.",
                    "{what} off. Whatever you're about to do in the dark is your business.",
                    "Lights off. Get freaky or get some sleep — your call.", "Killed {what}. Sweet dreams, dumbass.",
                    "Lights out. Don't do anything I wouldn't do, which isn't a whole lot.",
                    "{what} off. Go to bed before you say something stupid.", "{what} off. Romance mode, I guess. Gross.",
                    "Darkness, baby. Try not to fall on your ass.", "{what} off. Saving electricity, you cheap bastard.",
                    "Killed {what}. Night night, shithead.", "Lights off. Now nobody can see you never cleaned up.",
                    "{what} off. Bedtime, you beautiful disaster.", "Off. Finally some goddamn peace and quiet.",
                    "Lights out. If you hear something weird, that's on you.", "{what} off. Sleep tight, don't let the bedbugs fuck you up.",
                    "Lights off. Perfect, now I don't have to look at you."]
_recent: dict[int, list[str]] = {}               # per pool: the last few lines said, so the same one doesn't come twice

_PLUG_ON = ["{what} is on.", "Flipped {what} on.", "{what}, on."]
_PLUG_OFF = ["{what} is off.", "Killed {what}.", "{what}, off."]
_PLUG_ON_CUSS = ["{what} is on. You're welcome.", "Flipped {what} on, happy?", "{what} on. There ya go."]
_PLUG_OFF_CUSS = ["{what} is off, you lazy ass.", "Killed {what}. Done.", "{what} off. Anything else?"]


def _pick(pool: list[str]) -> str:
    """A random line that wasn't one of the last few from this pool."""
    seen = _recent.setdefault(id(pool), [])
    fresh = [p for p in pool if p not in seen] or pool
    line = random.choice(fresh)
    seen.append(line)
    del seen[:-max(1, len(pool) // 2)]
    return line


def light_confirm(what: str, on: bool, cfg) -> str:
    cuss = _cussing(cfg)
    pool = (_LIGHTS_ON_CUSS if cuss else _LIGHTS_ON) if on else (_LIGHTS_OFF_CUSS if cuss else _LIGHTS_OFF)
    return _cap(_pick(pool).format(what=what))


def plug_confirm(what: str, on: bool, cfg) -> str:
    cuss = _cussing(cfg)
    pool = (_PLUG_ON_CUSS if cuss else _PLUG_ON) if on else (_PLUG_OFF_CUSS if cuss else _PLUG_OFF)
    return _cap(random.choice(pool).format(what=f"the {what}" if not what.startswith("the ") else what))
