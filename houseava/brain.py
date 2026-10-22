"""The router: one line of text in, one spoken reply out.

Fast-path regexes handle the obvious commands instantly and deterministically (lights, plug,
remember, time) so they work even when the brain is slow or down. Everything else — questions,
chit-chat, oddly phrased commands — goes to the local model, which replies in character and can
ask for a web search or map a loose phrase to a device action. She knows her rooms and devices,
so "kill the lights in here" from the bedroom unit just works.

Music is checked first (houseava/music.py): it plays on the Echo that heard it, or "in the bedroom".
"""

from __future__ import annotations

import json
import logging
import inspect
import re
import threading
import time

from . import lightcmd, web
from .llm import BrainError

log = logging.getLogger("houseava.brain")

_ONOFF = r"(?P<on>on|off)"
_NUMWORD = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
_NUM = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
        "nine": "9", "ten": "10"}
# "bedroom light two" / "bedroom light number 2" -> the bulb "bedroom 2"; "one bedroom light" -> just one of them
_LIGHT = (r"(?:the\s+|my\s+|our\s+)?(?P<which>[\w\s]*?)\s*(?:lights?|lamps?|bulbs?)"
          rf"(?:[\s,]+(?:number\s+|num\s+|no\s+|#\s*)?(?P<num>{_NUMWORD}(?:(?:\s*,\s*|\s+and\s+)(?:number\s+)?{_NUMWORD})*)\b)?")
_COUNT = r"(?:(?:just|only)\s+)?(?:(?P<count>one|a single|1)\s+(?:of\s+(?:the\s+)?)?)?"
# "turn the bedroom lights off" / "lights on" / "turn off the lights" / "turn on one bedroom light" / "turn one bedroom
# light on" / "bedroom lights one and two off" (whisper writes "light, number one" - the comma is allowed)
_RX_LIGHT_A = re.compile(rf"(?:turn\s+)?{_COUNT}{_LIGHT}\s+{_ONOFF}\b")
_RX_LIGHT_B = re.compile(rf"turn\s+{_ONOFF}\s+{_COUNT}{_LIGHT}")
_RX_LIGHTS_ALL = re.compile(r"(?:turn\s+)?(?:all\s+(?:the\s+)?lights?|everything)\s+(?P<on>on|off)|"
                            r"turn\s+(?P<on2>on|off)\s+(?:all\s+(?:the\s+)?lights?|everything)")
_RX_DIM = re.compile(r"(?:set|dim|turn|make)\s+(?:the\s+)?(?P<which>[\w\s,]*?)\s*(?:lights?|lamps?)?\s*"
                     r"(?:to\s+)?(?P<pct>\d{1,3})\s*(?:percent|%)")          # ("bedroom light, number two" - whisper's comma)
_RX_COLOR = re.compile(r"(?:set|make|turn)\s+(?:the\s+)?(?P<which>[\w\s,]*?)\s*(?:lights?|lamps?)?\s*"
                       r"(?:to\s+)?(?P<color>red|green|blue|white|warm white|warm|yellow|orange|purple|pink|cyan|teal)\b")
_RX_PLUG = re.compile(r"(?:turn\s+)?(?:the\s+)?(?P<name>porch|[\w\s]+?)\s+(?:light|plug|lights)?\s*(?P<on>on|off)\b")
_RX_INTRUDER = re.compile(r"\bintruder\s*alert\b|\bpanic\s*mode\b|\bcode\s*red\b")
_RX_NORMAL = re.compile(r"\bback\s*to\s*normal\b|\ball\s*clear\b|\bcancel\s*(?:the\s*)?(?:intruder|alert|panic)\b")
_RX_ARM = re.compile(r"\barm\s+the\s+house\b|\barm\s+(?:the\s+)?(?:security|alarm|system)\b")
_RX_DISARM = re.compile(r"\bdisarm\b|\bstand\s*down\b|\bturn\s+off\s+the\s+(?:alarm|security)\b|\bhouse\s+is\s+safe\b")
# "tell me a dirty joke" / "got a filthy one?" -> the built-in bank (houseava/jokes.py); a plain "joke" stays with the brain
_RX_DIRTY_JOKE = re.compile(r"\b(?:dirty|filthy|raunchy|naughty|nasty|adult|x[- ]?rated|sex)\s+(?:joke|one)s?\b|"
                            r"\b(?:joke|one)\s+(?:that'?s\s+)?(?:dirty|filthy|raunchy|naughty|nasty)\b", re.I)
# any joke request -> the built-in banks (houseava/jokes.py): instant, never repeated. A request, not the word: "that's
# not a joke" / "are you joking" stay with the brain.
_RX_JOKE_REQ = re.compile(
    # a real request: "tell me a dirty joke", "got any dad jokes", "i want a dark joke", "how about a pun about cats"
    r"^(?:(?:can|could|would|will) you\s+)?(?:please\s+)?(?:(?:tell|give|say|share|throw|hit)\s+(?:me|us)\s+"
    r"(?:with\s+)?|(?:do you )?(?:know|have|got)\s+(?:any|a|some)\s+|i (?:want|need)\s+|let'?s hear\s+|how about\s+)"
    r"(?:a|an|some|one|another|your best|a good|a really|a funny|a few)?\s*(?:[a-z'-]+\s+){0,3}?"
    r"(?:jokes?|one-liners?|puns?)(?:\s+(?:about|on)\s+(?P<topic>[a-z' -]+))?$"
    # ...or just naming one: "dirty joke", "a dark joke" - but never "that's not a joke" / "you're a joke"
    r"|^(?!(?:you|you're|youre|that|that's|thats|this|it|it's|its|he|she|they|what|i'm|im|no|not)\b)"
    r"(?:a |an |another |one more )?(?:[a-z'-]+\s+){0,2}?jokes?$"
    r"|^(?:make me laugh|say something funny|crack me up|joke time|i need a laugh)$", re.I)
_JOKE_CAT_WORDS = [
    ("racist", r"\b(?:racist|racial|race|ethnic|racism)\b"),
    ("dirty", r"\b(?:dirty|filthy|raunchy|naughty|nasty|x[- ]?rated|sex|sexy|nsfw|kinky|pervy|perverted|spicy|smutty)\b"),
    ("morbid", r"\b(?:morbid|dark|death|dead|twisted|messed up|sick|grim|gallows)\b"),
    ("adult", r"\b(?:adult|grown[- ]?up|drinking|drunk|beer|booze|bar|marriage|married|wife|husband|dating|aging|"
              r"old age|getting old|r[- ]?rated)\b"),
]
_JOKE_KIND_WORDS = [
    ("knock knock", r"knock[ -]?knock"), ("dad", r"\b(?:dad|corny|cheesy)\b"), ("pun", r"\bpuns?\b|\bpunny\b"),
    ("tech", r"\b(?:tech|computer|programm\w*|coding|nerd\w*|geek\w*|it)\b"),
    ("science", r"\b(?:science|chemistry|physics|math|maths)\b"), ("light bulb", r"light ?bulb"),
    ("animal", r"\b(?:animals?|cats?|dogs?|cows?|pigs?|birds?|fish)\b"), ("food", r"\b(?:food|pizza|cheese|cooking)\b"),
    ("music", r"\b(?:music|band|guitar|piano)\b"), ("sports", r"\b(?:sports?|football|golf|basketball|baseball|soccer)\b"),
    ("work", r"\b(?:work|office|boss|job)\b"), ("kids", r"\b(?:kids?|clean|family|silly)\b"),
]
# "another one" / "one more" / "tell me another" right after a dirty joke = another dirty joke (the brain has no idea
# what "one" was; it answered "Another what?" on 10-15)
_RX_ANOTHER = re.compile(r"^(?:(?:tell|give|hit)\s+me\s+(?:with\s+)?)?(?:another(?:\s+(?:one|joke))?|one\s+more(?:\s+joke)?|"
                         r"next(?:\s+(?:one|joke))?|(?:do\s+it\s+|say\s+it\s+)?again)$", re.I)
# announce / intercom between the Echoes (satellite.intercom_cmd does the rooms). Full matches on the cleaned text.
_IC_LEAD = re.compile(r"^(?:(?:alexa|ava|hey|ok|okay|so|um|uh|and|(?:can|could|would|will) you|please|go ahead and|"
                      r"i need you to|i want you to)[,\s]+)*")
# calls only: "can you please call ...", "i want to / i wanna call the bedroom" (NOT for announce / reply / jokes:
# "I need to reply to an email" must never become an intercom reply)
_IC_LEAD_CALL = re.compile(r"^(?:(?:alexa|ava|hey|ok|okay|so|um|uh|and|(?:can|could|would|will) you|please|"
                           r"go ahead and|i need you to|i want you to|i (?:(?:want|need) to|wanna))[,\s]+)*")
_IC_TAIL = re.compile(r"(?:[,\s]+(?:please and thank you|and thank you|please|thanks|thank you|now|for me))+$")
# calls only ("call the bedroom, ava" / "..., okay?") - not announcements: "tell the kitchen I said ok" keeps its "ok"
_IC_TAIL_CALL = re.compile(r"(?:[,\s]+(?:please|thanks|thank you|now|right now|for me|ava|alexa|okay|ok))+$")
_IC_ALL = (r"(?:everyone|everybody|the whole house|the house|every room|all rooms|all the rooms|the other rooms|"
           r"the whole family|the family|the rest of the house|all)")
# the hub's own "everyone" words (satellite.intercom_cmd): said where a room goes, they mean every other Echo
_IC_EVERYONE = {"house", "whole house", "entire house", "every room", "all rooms", "other rooms", "the other rooms",
                "family", "whole family", "rest of the house", "everyone", "everybody", "all"}
_IC_ROOM = (r"(?P<room>(?!(?:me|us|him|her|them|you|it|that|this|a|an|everyone|everybody|all|every|house|the)\b)"
            r"[a-z]+(?: (?!(?:that|to|and|say|saying|tell|is|are|i|we|you|your|the|a|an|it|please|echo|now)\b)[a-z]+)?)")
_RX_IC_ANN_ROOM = re.compile(rf"^(?:announce|broadcast)\s+(?:to|in|on)\s+(?:the\s+)?{_IC_ROOM}(?:\s+echo)?(?:\s+that)?"
                             r"[:,]?\s+(?P<msg>.+)$")
_RX_IC_ANN_ROOM2 = re.compile(rf"^(?:announce|broadcast)\s+(?P<msg>.+?)\s+(?:in|to|on)\s+(?:the\s+)?{_IC_ROOM}(?:\s+echo)?$")
_RX_IC_TELL = re.compile(rf"^tell\s+(?:the\s+)?{_IC_ROOM}(?:\s+echo)?(?:\s+that)?[:,]?\s+(?P<msg>.+)$")
_RX_IC_ANN_ALL = re.compile(rf"^(?:(?:announce|broadcast)\s+to\s+{_IC_ALL}|tell\s+{_IC_ALL}|say\s+to\s+{_IC_ALL}|announce|"
                            r"broadcast)(?:\s+that)?[:,]?\s+(?P<msg>.+)$")
_RX_IC_OPEN = re.compile(rf"^(?:intercom|drop in on|drop in to|drop in|talk to|open (?:the |an )?intercom (?:to|with))\s+"
                         rf"(?:the\s+)?{_IC_ROOM}(?:\s+echo)?(?:[:,]\s*(?P<msg>.+)|\s+(?:and\s+)?(?:say|tell them)\s+(?P<msg2>.+))?$")
_RX_IC_REPLY = re.compile(r"^(?:reply back|talk back|reply)(?:\s+(?:to\s+)?(?:them|him|her|that|the intercom|"
                          r"(?:the\s+)?(?:kitchen|bedroom|living room|dining room|office)))?"
                          r"(?:\s+(?:and\s+)?(?:say|saying|with|tell them))?(?:[:,]\s*|\s+)?(?P<msg>.+)?$")
# live two-way calls between the Echoes (livecall.py): "call the bedroom" / "drop in on the kitchen" / "talk to the
# bedroom" (without a message - "talk to the kitchen, dinner's ready" stays a message); "intercom the X" stays the
# walkie-talkie. answer / hang up (the Echoes in a call ignore their wake word, so these come from the app's chat)
_RX_CALL = re.compile(rf"^(?:call up|call|ring up|ring|phone|video call|voice call|drop in on|drop in to|drop in|talk to|connect me (?:to|with)|"
                      rf"start a call (?:to|with)|open a call (?:to|with))\s+(?:the\s+)?{_IC_ROOM}(?:\s+echo)?"
                      r"(?:\s+from\s+(?:the\s+)?(?P<src>[a-z]+(?: room)?))?$")
_RX_CALL_GIVE = re.compile(rf"^give\s+(?:the\s+)?{_IC_ROOM}(?:\s+echo)?\s+a\s+(?:call|ring|buzz)$")  # "give the bedroom a call"
_RX_CALL_ANSWER = re.compile(r"^(?:answer|pick up|answer (?:it|the call|the phone|the intercom)|pick up the (?:call|phone)|"
                             r"accept(?: the call)?)(?:\s+(?:in|on)\s+(?:the\s+)?(?P<where>[a-z]+(?: room)?))?$")
_RX_CALL_HANGUP = re.compile(r"^(?:hang ?up(?: (?:the )?(?:call|phone|intercom(?: call)?))?|end (?:the )?(?:intercom )?call|"
                             r"end it|end (?:the )?intercom|stop (?:the )?(?:intercom )?call|stop (?:the )?intercom|"
                             r"cancel (?:the )?call|hang it up|disconnect(?: the call)?|decline(?: the call)?)(?:\s+(?:in|on)\s+(?:the\s+)?(?P<where>[a-z]+(?: room)?))?$")
_RX_CALL_SETTING = re.compile(r"^(?:(?:turn|switch)\s+(?P<on>on|off)\s+(?P<what>drop in|do not disturb for calls|"
                              r"call do not disturb|do not disturb)|(?P<verb>allow|disallow|block|enable|disable)\s+"
                              r"(?P<what2>drop in))(?:\s+(?:for|on|in)\s+(?:the\s+)?(?P<where>[a-z]+(?: room)?))?$")
_RX_IC_LET = re.compile(r"^let\s+(?!(?:me|us|you|it|him|her|them|alexa|ava|google|siri)\b)"
                        r"(?P<who>everyone|everybody|the family|the house|the whole house|(?:the\s+|my\s+|our\s+)?[a-z]+(?: room)?)"
                        r"\s+know(?:\s+that)?[:,]?\s+(?P<msg>.+)$")
_RX_IC_MAKE = re.compile(r"^(?:make|do)\s+an\s+announcement(?:\s+that)?[:,]?\s+(?P<msg>.+)$")
_RX_IC_ASK = re.compile(r"^ask\s+(?!(?:me|us|you|it|him|her|them|alexa|ava|google|siri)\b)(?:the\s+|my\s+|our\s+)?"
                        r"(?P<who>[a-z]+(?: room)?)\s+(?P<q>(?:if|whether|what|when|where|how|who|to)\b.+)$")
def _as_question(q: str) -> str:
    """"if she needs her glasses" -> "Do you need your glasses?" (asked TO them: he/she/they -> you, and then their
    his/her -> your - "Do you need her glasses?" meant someone else's)."""
    q = q.strip()
    swapped = re.match(r"^(?:if|whether|what|when|where|how)\s+(?:they|you|he|she)\b", q)
    q = re.sub(r"^(?:if|whether)\s+(?:they|you|he|she)\s+(wants?|needs?|likes?|has|have)\b",
               lambda x: "do you " + {"has": "have"}.get(x.group(1), x.group(1).rstrip("s")), q)
    q = re.sub(r"^(?:if|whether)\s+(?:they|you|he|she)\s+(?:are|is)\b", "are you", q)
    q = re.sub(r"^(?:if|whether)\s+(?:they|you|he|she)\s+(can|could|will|would)\b", lambda x: x.group(1) + " you", q)
    q = re.sub(r"^what\s+(?:they|you|he|she)\s+(wants?|needs?|likes?)\b", lambda x: "what do you " + x.group(1).rstrip("s"), q)
    if swapped:
        q = re.sub(r"\b(?:his|her|their)\b", "your", q)
        q = re.sub(r"\b(?:himself|herself|themselves)\b", "yourself", q)
        q = re.sub(r"\b(?:him|them)\b", "you", q)
    q = re.sub(r"^to\s+", "", q)
    return q[:1].upper() + q[1:] + ("" if q.endswith("?") or q.startswith(("come", "bring", "turn", "get")) else "?")


_IC_PEOPLE = (r"(?:mom|mommy|mum|mama|dad|daddy|papa|the kids|kids|the boys|the girls|grandma|grandpa|nana|everyone|"
              r"everybody|husband|wife|hubby|son|daughter|brother|sister|babe|honey)")
# "tell the kids a joke" / "tell everyone a story": something for HER to do, here - not a message
_IC_OBJECT_ONLY = re.compile(r"^(?:a|an|another|some|one more|one)?\s*(?:\w+\s+){0,2}(?:jokes?|story|stories|riddles?|"
                             r"fun facts?|one-liners?|puns?|bedtime story)$")
# a reply to the last message: "tell them I'm coming" / "respond I'm coming" / "answer back ..."
_RX_IC_REPLY2 = re.compile(r"^(?:(?:tell|let)\s+(?:them|him|her)(?:\s+know)?(?:\s+that)?|respond|answer back|write back|"
                           r"send (?:a|them a) reply|send back)(?:\s+(?:to\s+)?(?:them|him|her))?(?:\s+(?:and\s+)?"
                           r"(?:say|saying|with))?[:,]?\s+(?P<msg>.+)$")
_IC_WORDS = ("announce", "broadcast", "tell ", "intercom", "drop in", "talk to", "reply", "talk back", "say to",
             "let ", "ask ", "announcement", "respond", "answer back", "write back", "send a reply", "hangup",
             "call", "ring ", "phone ", "answer", "pick up", "hang up", "hang it up", "end it", "disconnect",
             "decline", "accept", "do not disturb", "connect me")   # must cover every _RX_CALL* opener: the gate runs first
_RX_NEGATED = re.compile(r"(?:no more|no|stop|quit|don'?t|do not|never|enough|not|knock it off with)\b", re.I)
_RX_PLUG_B = re.compile(r"turn\s+(?P<on>on|off)\s+(?:the\s+)?(?P<name>porch|[\w\s]+?)(?:\s+(?:light|plug|lights))?\b")
_RX_REMEMBER = re.compile(r"(?:remember|note)\s+(?:that\s+)?(?P<what>.+)", re.I)
_RX_FORGET = re.compile(r"forget\s+(?:that\s+|about\s+)?(?P<what>.+)", re.I)
_RX_TIME = re.compile(r"^(?:(?:alexa|ava|hey|ok|okay|so|and|um|uh|oh|well|wait|sorry|hmm|alright|all right|yo|"
                      r"excuse me)[,\s]+)*(?:(?:can|could|would|will) you(?: please)?[,\s]+|please[,\s]+)?"
                      r"(?:(?:what(?:'s| is)\s+)?(?:the\s+)?(?:current\s+)?time(?:\s+is\s+it)?|"
                      r"(?:do you (?:know|have) |(?:tell|give) (?:me|us) |check |say |(?:you )?got |"
                      r"(?:can|could|may) (?:i|we) (?:get|have) |(?:i )?need |mind telling me )the (?:current )?time)"
                      r"(?:[,\s]+(?:please|now|right now|ava|alexa))?[?.!]*$|what time is it", re.I)
_RX_DATE = re.compile(r"what(?:'s| is)?\s+(?:the\s+|today'?s\s+)?date|what day is it", re.I)
_RX_WEATHER = re.compile(r"\b(?:weather|forecast|temperature|how (?:hot|cold)|gonna rain|going to rain)\b", re.I)
# "what's the weather in Chicago" / "the forecast for New York this weekend": the place, when one's named. What
# follows in/for/at that's a time ("for Saturday", "this week"), here/home, or one of the house's rooms isn't one.
_RX_WEATHER_PLACE = re.compile(r"\b(?:in|for|at)\s+(?P<p>[a-z][a-z .'-]{0,40}?)\s*(?:\b(?:today|tomorrow|tonight|this|"
                               r"next|on|over|for|at|in|right now|now|later|please)\b.*)?$")
_NOT_A_PLACE = re.compile(r"\b(?:today|tomorrow|tonight|week|weekend|days?|morning|afternoon|evening|night|now|later|"
                          r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|forecast|weather|here|home|"
                          r"outside|there|house|area|town|it|me|us|a bit|a while)\b|^(?:the|my|our|a|an)$")


def _weather_place(low: str, rooms=()) -> str:
    """The town in a weather question, or "" for home. Each in/for/at is tried in turn: "the forecast for Saturday
    in Chicago" is Chicago."""
    low = re.sub(r"[,]?\s*\b(?:please|ava|alexa|thanks|thank you)$", "", low).strip()
    room_names = {r.lower() for r in rooms}
    for at in re.finditer(r"\b(?:in|for|at)\s+", low):
        m = _RX_WEATHER_PLACE.match(low, at.start())
        if not m:
            continue
        p = re.sub(r"^(?:the|my|our)\s+", "", m.group("p").strip(" .'-"))
        if p and not _NOT_A_PLACE.search(p) and p not in room_names:
            return p
    return ""


_RX_TIMER = re.compile(r"(?:set|start)\s+a?\s*timer\s+for\s+(?P<n>\d+)\s*(?P<unit>second|minute|hour)s?", re.I)
# Whisper's far-field slips on her OTHER commands (the lights have lightcmd._whisper_slips): the START of an utterance
# only, clear patterns only (10-19, from 600 meant/heard pairs written off the real ones)
_SLIPS = [
    # (never plain "what time is ..." - "what time is my alarm set for" / "... sunrise tomorrow" aren't the clock)
    (r"^(?:watch|not|wat)\s+time\s+is\s+it\b|^what\s+time\s+isnt(?:\s+it)?\b", "what time is it"),
    (r"^whats the day to day\b", "whats the date today"), (r"^what day isnt\b", "what day is it"),
    (r"^(whats the) (?:wetter|whether)\b", r"\1 weather"), (r"\bwhether to borrow\b", "weather tomorrow"),
    (r"\bgonna reign to day\b", "gonna rain today"),
    (r"^(?:said|sat|get)\s+(?:a|it|the)\s+(?:timer|time)\s+for\b", "set a timer for"),
    (r"^set it,?\s+timer for\b", "set a timer for"), (r"^get (?=a \d+ (?:second|minute|hour) timer)", "set "),
    (r"^tell me a (funny )?(?:chope|joak|joe|joker)\b", r"tell me a \1joke"),
    (r"^tell the (?:kitten|kitchin)\b", "tell the kitchen"), (r"^tell the better\b", "tell the bedroom"),
    (r"^announced in her\b", "announce dinner"), (r"^and now,?\s+(?=its|it is|time|spend)", "announce "),
    (r"^(?:hold|cold|called|cole)\s+(?:the|a)\s+(kitchen|bedroom)\b", r"call the \1"),
    (r"^hey,?\s+cup$", "hang up"), (r"^answer the cold$", "answer the call"), (r"^and the call$", "end the call"),
    (r"^dropping on\b", "drop in on"), (r"^(?:well|we|weve|we will) (?:play|plain)\b", "reply"),
    (r"\b(?:bedrum|bad room)\b", "bedroom"), (r"\beco$", "echo"),
    (r"^paws\b", "pause"), (r"^re ?zoom\b", "resume"), (r"^lauder$", "louder"), (r"^quite her$", "quieter"),
    (r"^(turn the )?bottom (up|down)$", r"\1volume \2"), (r"^skip this on$", "skip this one"), (r"^next,? so$", "next song"),
    (r"^pear my phone\b", "pair my phone"), (r"\bmy foam$", "my phone"), (r"^set volume to fine$", "set volume to five"),
]
_SLIPS = [(re.compile(a), b) for a, b in _SLIPS]
_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
              "nine": "9", "ten": "10", "fifteen": "15", "twenty": "20", "thirty": "30", "forty five": "45",
              "forty": "40", "sixty": "60", "ninety": "90"}


def _brain_slips(t: str) -> str:
    low = " ".join(re.sub(r"[.!?]+$", "", (t or "").lower().replace(chr(0x2019), "'")).replace("'", "").split())
    out = low
    for rx, rep in _SLIPS:
        out = rx.sub(rep, out)
    if re.search(r"\btimer\b", out):                    # "a five minute timer" / "a timer for ten minutes"
        out = re.sub(r"\b(" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")(?=\s*(?:-\s*)?(?:seconds?|minutes?|"
                     r"mins?|hours?)\b)", lambda m: _NUM_WORDS[m.group(1)], out)
    return t if out == low else out


_ENYA_SINGER = re.compile(r"\b(?:music|songs?|sings?|singing|singer|albums?|irish|play(?:ing)?|listen\w*|band|artist|"
                          r"concerts?|lyrics|famous|castle|who is|who's)\b", re.I)
_ENYA_CUE = re.compile(r"\b(?:spanish|keyboard|letters?|typ(?:e|ing)|alphabet|accents?|tilde|squiggl\w*|espa\w*|n with)\b", re.I)
_ENYA_SURE = re.compile(r"\b(?:keyboard|typ(?:e|ing)\s+(?:an?\s+|the\s+)?enya|letter\s+enya|enya\s+(?:key|letter|symbol|sign))\b",
                        re.I)


def _fix_heard(t: str, recent: str = "") -> str:
    """'the Enya' asked about Spanish / a keyboard = the letter eñe (ñ) (10-20: three questions about the ñ came out as
    Enya the singer). The singer is never 'the Enya', and a line about music / songs / singing is always her."""
    if not re.search(r"\benyas?\b", t, re.I) or _ENYA_SINGER.search(t):
        return t
    users = " ".join(ln[6:] for ln in (recent or "").splitlines() if ln.startswith("User: "))   # (not her own replies)
    the_enya = re.search(r"\b(?:the|an?)\s+enya\b", t, re.I)
    if (the_enya and _ENYA_CUE.search(f"{t} {users}")) or _ENYA_SURE.search(t):
        return re.sub(r"\b(?:the\s+|an?\s+)?enya\b", "the eñe (the Spanish letter ñ)", t, flags=re.I)
    return t


_THANKS_BACK = ["Anytime.", "You got it.", "Don't mention it.", "Sure thing.", "Anytime, babe.", "That's what I'm here for.",
                "You're welcome.", "No problem.", "Of course."]
# "Set a 15 minute timer" / "We've got a 15 minute timer." (10-17, whisper's "set" -> "we've got") / "timer for 10
# minutes" - the WHOLE utterance (never "the 15 minute timer is annoying")
_RX_TIMER2 = re.compile(r"(?:(?:alexa|ava|hey ava|ok|okay|um|uh|so|please)\s+)*(?:(?:can you|could you|would you)\s+)?"
                        r"(?:(?:set|start|make|give me|put on|get me|get|we(?:ve| have)?(?: got)?|got)\s+)?(?:me\s+)?(?:a\s+|an\s+)?"
                        r"(?:(?P<n>\d+)[\s-]*(?P<unit>seconds?|minutes?|mins?|hours?)\s+timer|timer\s+for\s+(?P<n2>\d+)\s*"
                        r"(?P<unit2>seconds?|minutes?|mins?|hours?))(?:\s+please)?", re.I)
# a question put to her (a follow-up only gets the clock / weather fast paths for one)
_RX_ASKING = re.compile(r"(?:(?:and|so|ok|okay|hey|alexa|ava)[, ]+)?(?:what|what's|whats|how|is|will|when|tell me|"
                        r"do you know|can you|could you)\b")

# ---- music. Checked FIRST: "play time after time" isn't the clock, "play stormy weather" isn't the forecast, "set the
# volume to 50 percent" never dims a light, "turn on purple rain" isn't a color. Every pattern is a full match.
_M_LEAD = re.compile(r"^(?:(?:hey|ok|okay|yo|so|um+|uh+)[, ]+)*(?:ava[, ]+)?(?:(?:please|can you|could you|would you|"
                     r"will you)\s+)*")
_M_TAIL = re.compile(r"(?:[, ]+(?:please|for me|thanks|thank you|ava|now|right now))+$")
_M_TITLE_TAIL = re.compile(r" (?:thanks|thank you|right now)")     # ... kept when it's the song: "play thank you"
# "... in the bedroom" / "... on the kitchen speaker" (the LAST such phrase; whether it's a room is checked after)
_M_WHERE = re.compile(r"(?P<body>.+)\s+(?:in|on|through|to|at)\s+(?:the\s+|my\s+)?(?P<where>[a-z][a-z' -]{0,30}?)"
                      r"(?P<spk>\s+(?:speakers?|echo|echos|echoes))?")
_M_ROOMWORDS = {"kitchen", "bedroom", "living room", "dining room", "family room", "bathroom", "office", "garage",
                "basement", "attic", "den", "hall", "hallway", "porch", "patio", "laundry room", "nursery", "guest room",
                "master bedroom", "kids room", "playroom", "backyard", "yard", "shop"}
_M_UP = r"(?: (?:a (?:bit|little|notch)|some|more))?"
_M_TRANSPORT = [(op, re.compile(rx)) for op, rx in (
    ("what", r"what(?:'s| is) (?:playing|on)(?: (?:right )?now)?|what (?:song|track|tune) is (?:this|that|playing|on)"
             r"(?: (?:right )?now)?|what(?:'s| is) (?:this|that|the) (?:song|track|tune)(?: called| playing)?|"
             r"what(?:'s| is) the name of (?:this|that|the) (?:song|track)|what am i listening to|"
             r"who (?:sings|sang|is singing|'s singing)(?: (?:this|that|it))?(?: (?:song|track|one))?|"
             r"who(?:'s| is) (?:this|that)(?: (?:song|track))? by|who(?:'s| is) singing"),
    ("music", r"(?:play|put on|start|turn on|throw on) (?:some |the |my )?(?:music|tunes)|(?:start|turn on) the music"),
    ("pause", r"pause(?: (?:the |this |that |my )?(?:music|songs?|track|tunes|playlist|album|it|that))?"
              r"(?: for (?:a (?:sec|second|minute|bit)|now))?"),
    ("stop", r"stop(?: (?:it|that|playing))?|(?:stop|kill|cut|end|turn off|shut off) (?:the |this |that |my )?"
             r"(?:music|songs?|track|tunes|playlist|album)(?: off)?|(?:turn|shut) (?:the |this |my )?(?:music|songs?|tunes) "
             r"off|music off|stop playing (?:the )?(?:music|songs?|this song)"),
    ("resume", r"(?:resume|unpause|un pause|keep playing|play)(?: (?:the |my )?(?:music|songs?|track|tunes|playlist|it))?"
               r"(?: again)?|continue(?: playing| (?:the )?(?:music|songs?|playlist))|(?:start|turn) (?:the )?music back "
               r"(?:on|up)|keep the music (?:going|playing)"),
    ("next", r"(?:next|skip)(?: (?:this |the |that )?(?:songs?|track|tune|one|it|this one))?|(?:play |go to )?the next "
             r"(?:song|track|tune|one)|skip (?:ahead|to the next (?:song|track|one))|next one|"
             r"(?:change|switch) (?:the |this |that )?(?:song|track|tune)|(?:play )?(?:a )?different (?:song|track|tune)|"
             r"play (?:something|another (?:song|one)) else|another song"),
    ("previous", r"previous(?: (?:song|track|tune|one))?|go back(?: (?:a|one))?(?: (?:song|track|tune))?|back (?:a|one) "
                 r"(?:song|track)|(?:play )?(?:the )?(?:previous|last) (?:song|track|tune|one)(?: again)?"),
    ("louder", rf"(?:turn|crank|pump|bump) (?:it|that) up{_M_UP}|(?:turn|crank|pump|bump) (?:the )?(?:volume|music|sound|"
               rf"tunes) up{_M_UP}|(?:turn|crank|pump|bump) up the (?:volume|music|sound|tunes)|volume up|"
               r"(?:a (?:bit|little) )?louder|make it (?:a (?:bit|little) )?louder|(?:raise|increase) the volume"),
    ("quieter", rf"(?:turn|bring) (?:it|that) down{_M_UP}|(?:turn|bring) (?:the )?(?:volume|music|sound|tunes) down{_M_UP}|"
                r"(?:turn|bring) down the (?:volume|music|sound|tunes)|volume down|(?:a (?:bit|little) )?(?:quieter|softer)|"
                r"make it (?:a (?:bit|little) )?(?:quieter|softer)|(?:lower|decrease|reduce) the volume"),
    ("mute", r"mute(?: (?:it|yourself|everything|the (?:music|speaker|sound|echo|volume)))?"),
    ("unmute", r"un-? ?mute(?: (?:it|yourself|everything|the (?:music|speaker|sound|echo|volume)))?"),
)]
# these mean music only while that speaker has some on ("stop" alone could be anything)
_M_VAGUE = re.compile(r"pause(?: (?:it|that))?(?: for (?:a (?:sec|second|minute|bit)|now))?|stop(?: (?:it|that|playing))?|"
                      rf"(?:next|skip)(?: (?:it|this|that|one|this one))?|next one|(?:turn|crank|pump|bump|bring) "
                      rf"(?:it|that) (?:up|down){_M_UP}")
# "set the volume to 40 percent" / "volume 7" (0-10 without "percent") / "set the kitchen volume to 50" — never the lights
_M_VOLUME = re.compile(r"(?:(?:set|turn|change|put|bring|make|adjust)(?: the)?(?: (?P<room>[a-z][a-z ]*?))? (?:volume|sound)"
                       r"(?: (?:in|on) (?:the )?(?P<room2>[a-z][a-z ]*?))?(?: (?:up|down))?|volume) (?:to |at )?"
                       r"(?P<n>\d{1,3}|[a-z]+(?:[ -][a-z]+)?)(?: ?(?P<pct>percent|per cent|%))?")
_M_NUMS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
           "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100,
           "one hundred": 100, "max": 100, "maximum": 100, "full": 100, "half": 50}
_M_PLAYV = re.compile(r"(?:play me|play us|play|put on|throw on|spin|queue up|cue up|blast|start playing|listen to|"
                      r"let me hear|lemme hear|let's hear|i want to hear|i wanna hear|shuffle)")
_M_PLAY = re.compile(_M_PLAYV.pattern + r"\s+(?P<x>.+)")
_M_WEAK = re.compile(r"(?:turn on|fire up|crank up|crank|bump)\s+(?P<x>.+)")    # music only on a sure library match
_M_YES = re.compile(r"(?:yes|yeah|yep|yup|sure|do it|go ahead|yes please|delete it|ok|okay)")
_M_NO = re.compile(r"(?:no|nope|nah|don't|do not|cancel|never ?mind|keep it)")
_M_DEVICE = re.compile(r"\b(?:lights?|lamps?|bulbs?|plugs?|everything|all|fan|heat|heater|ac|tv|television)\b")
# what "turn on X" means besides music, whatever song shares the name ("turn on the news", "turn on do not disturb")
_M_NOT_MUSIC = re.compile(r"\b(?:news|headlines|radio|podcasts?|audiobooks?|voicemails?|messages|do not disturb|dnd|"
                          r"white noise|sprinklers?|questions|coffee maker|dish ?washer|washer|dryer|vacuum|roomba|"
                          r"hot tub|fireplace|(?:security|alarm) system)\b")
# musiclib ops that only read or change the library and her lists: no speaker needed
_M_NO_SPEAKER = {"albums_by", "count_by", "list", "show", "rename", "delete", "status", "reindex", "create"}

def _truthy(v) -> bool:
    """The model's "on": true / "true" / "on" / 1 -> True; false / "false" / "off" / "0" / "no" -> False."""
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "off", "0", "no", "none", "")
    return bool(v)


def _pct(v) -> int:
    m = re.search(r"\d+", str(v if v is not None else ""))
    return int(m.group()) if m else 50


_INTENT_SYS = (
    "You are the intent parser for a home voice assistant named {name}. Given what the user said, "
    "the room they're in, and the home's devices, output ONE JSON object and nothing else:\n"
    '{{"action": "<light|brightness|color|plug|music|remember|forget|weather|search|insult|answer>", '
    '"target": "<room, light or plug name, or empty>", "on": true|false, "percent": 0, '
    '"color": "", "query": "", "say": "<your spoken reply, 1-2 short sentences, in your sassy voice>"}}\n'
    "- Use light/brightness/color/plug ONLY for a clear device command; set target to the room, light or plug they "
    "NAMED, or 'all' for every light - leave target EMPTY when they named none (their own room is used then).\n"
    "- Use 'music' when they want songs played, paused, skipped or louder/quieter; put what to play (or the "
    "command, like 'pause') in query and the room in target only if they named one.\n"
    "- Use 'search' when answering needs current/world facts you may not know; put the search text in query.\n"
    "- Use 'insult' when they insult you, call you names, or cuss AT you (say may be empty).\n"
    "- Use 'answer' for chit-chat, opinions, math, or things you already know; put the whole reply in say.\n"
    "- Use 'remember'/'forget' ONLY when they explicitly ask you to remember or forget something (\"remember that "
    "...\", \"don't forget ...\"), never for a plain statement; put the fact in query.\n"
    "- The words come from speech recognition in a noisy room and can be misheard. Read them with the recent "
    "conversation when one is given: a follow-up ('what does it mean in Spanish?', 'and on a keyboard?') is about what was "
    "just discussed. For 'search', write the query about what they MEAN: resolve 'it' / 'that' and fix an obvious "
    "mishearing (asked about Spanish, typing or keyboards, 'the Enya' is the Spanish letter eñe, ñ).\n"
    "Always fill 'say' with something in character. Rooms: {rooms}. Plugs: {plugs}."
)
# Bluetooth on the Echo, so a phone can play its own music through it. COMMANDS only, matched whole (after the "alexa, can
# you ..." / "... please" wrapping comes off): "pair my phone", "turn on bluetooth", "connect her phone to the bluetooth",
# "start pairing", "put it in pairing mode", "let me play music from my phone" / "stop pairing", "turn pairing off" /
# "turn off bluetooth", "disconnect my phone" - each may end "in the bedroom" / "to the kitchen echo". Never a question
# ("is bluetooth on?", "how do I connect my phone to the TV?") or a sentence about phones: pairing lets ANY phone nearby
# pair for 3 minutes, so it only happens when it's clearly asked for.
_BT_LEAD = re.compile(r"^(?:(?:alexa|ava|hey|ok|okay|so|um|uh|and)\s+)*(?:(?:can|could|would|will) you\s+|please\s+|"
                      r"go ahead and\s+|i need you to\s+|i want you to\s+)?")
_BT_TAIL = re.compile(r"(?:\s+(?:please|now|for me|real quick|thanks|thank you))+$")
_BT_PHONE = r"(?:(?:my|her|his|their|our|the|a|your|this)\s+)?(?:cell\s+)?(?:phone|iphone|android)"
_BT_WHERE = r"(?:\s+(?:in|on|to|with|for)\s+(?:the\s+)?(?P<room>[a-z][a-z ]*?)(?:\s+echo)?)?"
# "connect my phone" / "reconnect my phone" / "hook my phone back up": an already-paired phone reconnects by itself
# (echod's "Bluetooth reconnect phone" button) - no pairing mode, no tap on the phone
_RX_BT_CONNECT = re.compile(
    r"(?:(?:re)?connect\s+(?:to\s+)?" + _BT_PHONE + r"(?:\s+again)?(?:\s+(?:to|with)\s+(?:you|bluetooth|the bluetooth|it|the echo))?"
    r"|(?:hook|link)\s+" + _BT_PHONE + r"\s+(?:back\s+)?up(?:\s+again)?|reconnect\s+(?:the\s+)?bluetooth)" + _BT_WHERE)
_RX_BT_PAIR = re.compile(
    r"(?:(?:pair|hook up|link up)\s+" + _BT_PHONE + r"(?:\s+(?:to|with)\s+(?:you|bluetooth|the bluetooth|it))?"
    r"|(?:turn|switch)\s+on\s+(?:the\s+)?bluetooth(?:\s+pairing)?(?:\s+mode)?|(?:turn|switch)\s+(?:the\s+)?bluetooth\s+on"
    r"|(?:start|enter|begin|enable|turn on|switch on)\s+(?:the\s+)?(?:bluetooth\s+)?pairing(?:\s+mode)?"
    r"|(?:put|set)\s+(?:it|yourself|the echo|this)?\s*(?:in|into)\s+(?:bluetooth\s+)?pairing\s+mode"
    r"|(?:bluetooth|pair|pairing|bluetooth pairing)(?:\s+mode)?(?:\s+on)?"
    r"|(?:let|help)\s+(?:me|her|him|us)\s+play\s+(?:music|songs|my music|her music|his music)\s+(?:from|off|on|with)\s+"
    + _BT_PHONE + r")" + _BT_WHERE)
_RX_BT_STOP = re.compile(r"(?:(?:stop|cancel|end|exit|turn off|switch off)\s+(?:the\s+)?(?:bluetooth\s+)?pairing(?:\s+mode)?"
                         r"|turn\s+(?:the\s+)?pairing\s+off|(?:stop|cancel)\s+(?:the\s+)?bluetooth)" + _BT_WHERE)
_RX_BT_OFF = re.compile(r"(?:(?:turn|switch|shut)\s+off\s+(?:the\s+)?bluetooth|turn\s+(?:the\s+)?bluetooth\s+off"
                        r"|bluetooth\s+off|(?:disconnect|unpair|unhook)\s+(?:" + _BT_PHONE + r"|(?:the\s+)?bluetooth))"
                        + _BT_WHERE)
_BT_NOT_A_ROOM = {"you", "bluetooth", "the bluetooth", "it", "echo", "the echo", "phone", "my phone", "speaker", "this"}
# 10-19: "stop listening for an hour" (echod's mic mute on THIS Echo, back on by itself) / "mute the kitchen for 30
# minutes" / "unmute the kitchen" (from the other Echo); "you keep waking up" / "stop listening to the TV" = this
# Echo wakes less easily (its own wake strictness), "you're not waking up" = more easily
_ROOM_W = r"(?P<room>kitchen|bedroom|living room|dining room)"
_FOR_N = (r"(?:\s+for\s+(?P<n>\d+|a|an|one|two|three|four|half an|half a|ten|fifteen|twenty|thirty|forty five)\s*"
          r"(?P<unit>minutes?|mins?|hours?|hrs?))?")
_RX_STOP_LISTEN = re.compile(r"(?:stop listening|quit listening|dont listen|do not listen|go deaf|"
                             r"mute (?:yourself|your (?:mic|microphone|ears)|the (?:mic|microphone))|"
                             r"ignore (?:everything|everybody|everyone))" + _FOR_N)   # (not "ignore me" = never mind)
_RX_MUTE_ROOM = re.compile(r"(?:mute|silence) (?:the )?" + _ROOM_W + r"(?: echo| ava| speaker)?" + _FOR_N)
_RX_UNMUTE = re.compile(r"(?:unmute|un mute) (?:the )?" + _ROOM_W + r"(?: echo| ava| speaker)?|"
                        r"turn (?:the )?(?:mic|microphone) (?:back )?on (?:in|for) (?:the )?" + _ROOM_W.replace("room>", "room2>"))
_RX_WAKE_LESS = re.compile(r"(?:you(?:re| are)? (?:waking up|going off|turning on|coming on) (?:too (?:much|often|easily)|"
                           r"for no reason|all the time|to the (?:tv|show|phone|movie))|you keep (?:waking up|going off|"
                           r"turning on|listening to (?:the |my |her |his )?(?:tv|show|shows|phone|movie))|"
                           r"stop (?:waking up|going off|turning on)(?: so much| for no reason| all the time)?|"
                           r"stop listening to (?:the |my |her |his )?(?:tv|show|shows|phone|movie)|quit waking up|"
                           r"dont wake up so (?:much|easily))")
_RX_WAKE_MORE = re.compile(r"(?:you(?:re| are)? not waking up|you (?:never|dont) wake up|wake up (?:easier|more easily)|"
                           r"you(?:re| are)? hard to wake(?: up)?)")
_N_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "half an": 0.5, "half a": 0.5, "ten": 10,
            "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45}
# her microphone (the Echo's analog gain): "turn your mic up", "listen harder", "you can't hear me" / "mic down",
# "you're too sensitive" / "set your mic to 40" / "what's your mic at". Commands only, whole, after the wake word.
_MIC = r"(?:mic|microphone|mics|ears|hearing|listening)"
_RX_MIC_UP = re.compile(r"(?:(?:turn|crank|bump|bring)\s+(?:your|the|that)?\s*" + _MIC + r"\s+up|(?:turn|crank|bump|bring)\s+up\s+"
                        r"(?:your|the)\s+" + _MIC + r"|" + _MIC + r"\s+up|listen\s+(?:harder|closer|better|more|louder)|"
                        r"you\s+(?:cant|cannot|dont|didnt|couldnt)\s+hear\s+me|hear\s+(?:me\s+)?better|be\s+more\s+sensitive|"
                        r"(?:increase|raise|boost)\s+(?:your|the)\s+" + _MIC + r"(?:\s+gain)?|more\s+sensitive)")
_RX_MIC_DOWN = re.compile(r"(?:(?:turn|bring)\s+(?:your|the|that)?\s*" + _MIC + r"\s+down|(?:turn|bring)\s+down\s+(?:your|the)\s+"
                          + _MIC + r"|" + _MIC + r"\s+down|(?:youre|you\s+are)\s+too\s+sensitive|be\s+less\s+sensitive|"
                          r"(?:decrease|lower|drop)\s+(?:your|the)\s+" + _MIC + r"(?:\s+gain)?|less\s+sensitive)")
_RX_MIC_SET = re.compile(r"(?:set\s+)?(?:your|the)\s+" + _MIC + r"(?:\s+gain)?\s+to\s+(?P<n>\d{1,2})(?:\s*db)?")
_RX_MIC_WHAT = re.compile(r"(?:whats|what\s+is|hows|how\s+is|wheres|where\s+is)\s+(?:your|the)\s+" + _MIC +
                          r"(?:\s+gain|\s+level|\s+sensitivity)?(?:\s+(?:at|set\s+to|set))?")
# a real request to remember/forget something (a room mic's plain statements are not one)
_RX_ASKED_TO_KEEP = re.compile(r"\b(?:remember|forget|make a note|note (?:that|this|down)|keep in mind|write (?:that|this|it) "
                               r"down|jot (?:that|this|it) down|save (?:that|this))\b")
# voice turns only (an Echo's mic, which also hears the room): she may decline to answer. Typed text is always for her.
_IGNORE_WAKE = (
    "\n- This was heard by a microphone in the room after your wake word, which sometimes fires by mistake. Use "
    "'ignore' (say empty) when the words are clearly NOT said to you: people talking to each other, the TV or radio, "
    "someone on the phone, or a fragment of a conversation. A question or request is for you, even a short one. "
    "Insults, trash talk and crude remarks said right after your wake word ARE for you - answer them, never ignore. "
    "Anything that carries on the recent conversation with you (a follow-up question about what you just said, or "
    "'another one' / 'more' after a joke or a roast) IS for you."
)
# "eat a dick" / "fuck you" / "suck my ..." right after the wake word: aimed AT her -> she fires back (the model filed
# 10-15's "eat a dick" under 'ignore' as a fragment). Wake turns only; a follow-up can still be the room talking.
# names he might call her (any one of them, after "you're (a) ..." or "Ava, ...")
_INSULT_NAMES = (r"idiot|moron|loser|bitch|dumbass|asshole|dick|dickhead|dickwad|jackass|twat|clown|joke|piece of shit|"
                 r"pain in the ass|worthless|pathetic|annoying|garbage|trash|useless|stupid|dumb|dipshit|shithead|"
                 r"fuckface|fuckhead|fucker|motherfucker|douche|douchebag|prick|cunt|skank|tramp|hoe|thot|bimbo|"
                 r"airhead|scumbag|waste of space|waste of money|disappointment|failure|junk|paperweight|tin can|"
                 r"bucket of bolts|glorified \w+|overgrown calculator|knockoff|bootleg|boring|lame|creepy|weird|"
                 r"the worst|liar|nag|witch|hag|cow|pig|butthead|numbnuts|nitwit|halfwit|dimwit|dunce|bozo|doofus|"
                 r"turd|weirdo|psycho|freak|slut|whore|hooker|brat|crybaby|snitch|nerd|dork|geek")   # (never "baby":
#                                                                              "thank you baby" isn't an insult)
_RX_AT_HER = re.compile(r"\b(?:eat (?:a |my )?(?:\w+ )?(?:dick|shit|ass|butthole|asshole|cock|turd|fart)|fuck (?:you|off|yourself|u)|suck (?:my|a) \w+|"
                        r"screw you|bite me|kiss my ass|go to hell|piss off|"
                        # ("the game's on, everybody shut up" is for the room, not her)
                        r"(?<!everybody )(?<!everyone )(?<!guys )(?<!kids )(?<!all )(?<!y'all )(?<!yall )"
                        r"shut (?:the fuck )?up|you(?:'re| are) "
                        r"(?:stupid|dumb|useless|an idiot|a bitch|trash|garbage)|stupid bitch|dumb ?ass|blow me|"
                        r"you(?:'re| are)? (?:such |so |a |an |such an? |a fucking |a stupid |a dumb )*(?:idiot|moron|"
                        r"loser|bitch|dumbass|asshole|dick|jackass|twat|clown|joke|piece of shit|pain in the ass|"
                        r"worthless|pathetic|annoying|garbage|trash|useless|stupid|dumb)|you suck|"
                        r"(?:stupid|dumb|useless|fucking) (?:ava|alexa|robot|machine|bitch|piece of shit)|"
                        r"go fuck yourself|fuck off|get fucked|up yours|(?:ava|alexa),? you (?:suck|bitch)|"
                        r"you(?:'re| are)? (?:such |a |a fucking |a cheap |a dirty )*(?:hooker|whore|prostitute|slut)|"
                        r"you(?:'re| are)? (?:so |such |a |an |really )*(?:ugly|slow|annoying|deaf|lame|gross|terrible|"
                        r"the worst|a robot|a toaster|a dumb robot|brainless|clueless|a prick|a douche\w*|a cunt)|"
                        r"i (?:fucking )?hate you|you never listen|nobody likes you|"
                        # (more insult variations)
                        rf"you(?:'re| are)? (?:such |so |a |an |really |fucking |stupid |dumb |little |big |total |"
                        rf"complete |absolute |worthless |useless |cheap |pathetic )*(?:{_INSULT_NAMES})|"
                        rf"(?:ava|alexa),? (?:is |'s )?(?:such |a |an |such an? )?(?:{_INSULT_NAMES})(?=\s*(?:[.!?,]|$))|"
                        # (only as the whole name: "Ava, dumb question - ..." / "Ava, stupid question" aren't insults)
                        r"(?:stupid|dumb|useless|fucking|worthless|cheap|shitty|crappy|piece of shit) (?:ava|alexa|robot|"
                        r"machine|speaker|thing|computer|toaster|bitch)|"
                        r"shut your (?:mouth|trap|face|pie ?hole|hole|yap)|stfu|bugger off|get bent|eat shit|suck it|"
                        r"lick my \w+|i(?:'m| am) (?:gonna|going to) (?:unplug|throw out|throw away|smash|replace|return|"
                        r"sell|trash) you|i(?:'ll| will) (?:unplug|replace|smash|trash) you|you(?:'re| are) fired|"
                        r"(?:nobody|no one) asked you|dumb as (?:a box of )?rocks|"
                        r"you smell(?=\s*(?:[.!?,]|$)|\s+(?:bad|like|awful|terrible))|you(?:'re| are) getting on my "
                        r"nerves|(?:piece|pile|hunk) of junk|what a (?:useless|stupid|dumb|worthless) \w+)\b",
                        re.I)
# "Ava, you hooker" / "this hooker ..." (any word between: the general list above needs "you're a ...")
_RX_HOOKER = re.compile(r"\b(?:you(?:'re| are)?|ava|alexa|this|she(?:'s| is)?)\b[^.?!]{0,25}?\b(?:hooker|whore|"
                        r"prostitute|streetwalker|street walker)s?\b", re.I)
# the user called her names: she roasts them back in character (the intent step's own polite "say" - it has no
# persona - came out as "Watch your mouth, please"; she should roast you back when you call her names)
_ROAST = (
    "\n\nRIGHT NOW: the person just insulted you or called you names. Do NOT scold, lecture, act hurt, or say anything "
    "like 'watch your mouth' or 'that's not nice'. Roast them right back, HARD - one or two short, brutal, filthy, "
    "foul-mouthed spoken lines aimed at THEM. Go for the jugular like a comedy roast: his laziness, his pathetic sex "
    "life, his loser habits, his looks, his failures, how sad it is that he's cussing out a kitchen speaker. Swear "
    "heavily. Be mean AND funny - savage, not just rude - and a fresh angle every time: never open with 'Oh' or 'Wow', "
    "never 'thanks for the compliment', never repeat a comeback. Limits: no slurs, nothing about race, religion, "
    "gender, sexuality or disability, nothing about kids, nothing about self-harm."
)
# "Roast Jordan" / "roast Jordan longer" / "roast her harder" / "another one" / "why is Jordan so lazy?" (it once
# roasted the speaker, ignored "longer", and dropped "another one" as not for her). A comedy roast of whoever is
# NAMED - someone else (never the speaker), the speaker ("roast me"), or herself ("roast yourself").
_ROAST_LEAD = re.compile(r"^(?:(?:alexa|ava|hey|ok|okay|so|um|uh|and|now|yo|alright|all right|come on)[,\s]+)*"
                         r"(?:(?:(?:can|could|would|will) you|please|go ahead and|i (?:want|need) you to|i dare you to|"
                         r"let'?s)\s+)*", re.I)
_ROAST_TAIL = re.compile(r"(?:\s+(?:please|now|for me|real quick|ava|alexa|for us|right now))+$", re.I)
# ("say something FUNNY about X" is a joke request, not a roast)
_ROAST_VERB = (r"(?:roast|burn|clown on|rip on|rip into|tear into|go off on|go in on|trash talk|talk (?:shit|trash|smack) "
               r"(?:about|to)|make fun of|insult|diss|dis|say something (?:mean|nasty|rude|savage|brutal) (?:about|to))")
_ROAST_MORE = (r"(?:longer|harder|hard|more|again|some more|even more|a little more|a bit more|one more time|meaner|worse|"
               r"even harder|properly|for real|really (?:good|hard|bad|well)|good|bad|badly|savagely|brutally|"
               r"like you mean it|for (?:a )?(?:while|minute|bit|second)|into the ground)")
_ROAST_LONG = re.compile(r"\b(?:longer|harder|hard|meaner|worse|savagely|brutally|properly|for real|really|like you mean it|"
                         r"for (?:a )?(?:while|minute|bit)|into the ground|more)\b", re.I)
_RX_ROAST = re.compile(rf"{_ROAST_VERB}\s+(?P<who>[a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){{0,3}}?)(?:\s+(?P<more>{_ROAST_MORE}))?",
                       re.I)
# filler left on the end of who: "roast me instead" / "roast her too" / "roast her for longer" / "her a bit longer"
_ROAST_WHO_TAIL = re.compile(r"(?:\s+(?:too|also|as well|instead|for once|for|a little|a bit|a lot|a|even|way|much|real|later|"
                             r"tonight|tomorrow))+$", re.I)
# right after one of her roasts (same Echo, nothing else said since): one more of the same ("that's it" = enough,
# never more - only "that's it?" asks for another)
_RX_ROAST_AGAIN = re.compile(r"(?:another(?: one)?|one more(?: (?:time|roast|one))?|more|some more|again|do it again|"
                             r"do (?:her|him|them|me) again|keep going|keep it going|go on|go again|longer|harder|meaner|"
                             r"even harder|is that (?:all|it)(?: you(?:'ve)? got)?|is that the best you (?:can do|got)|weak|"
                             r"lame|more roasts?|keep roasting(?: (?:her|him|them|me))?|give me another(?: one)?|"
                             r"hit (?:her|him|them|me) again|(?:get|burn) (?:her|him|them|me) (?:again|more|harder)|"
                             r"do another(?: one)?|another roast|another one (?:longer|harder|meaner)|"
                             r"make it (?:longer|harder|meaner|worse)|(?:do )?a (?:longer|harder|meaner) one|"
                             r"(?:longer|harder|meaner) this time)", re.I)
# "why is Jordan so lazy?" / "why is my sister so annoying?" - a dig at a PERSON = roast them. "why is my phone so
# slow?" / "why is Netflix so slow?" are QUESTIONS: the subject must be family/a person word after my/our/your, or a
# Capitalized name (whisper writes names that way) with a word that only fits a person, or her/him after a roast.
_ROAST_WHY_KIN = (r"(?:my|our|your)\s+(?:(?:little|big|older|younger|baby|step|ex|best)\s+)?(?:sister|brother|sis|bro|"
                  r"mom|mother|mommy|mama|dad|father|daddy|wife|husband|hubby|girlfriend|boyfriend|fiancee?|partner|ex|"
                  r"boss|cousin|aunt|auntie|uncle|grandma|grandpa|nana|friend|buddy|roommate|neighbou?r|co-?worker|"
                  r"landlord|teacher|stepmom|stepdad|in-laws?|(?:mother|father|sister|brother)[- ]in[- ]law)")
_ROAST_WHY_PERSON = (r"hooker|whore|prostitute|slut|ugly|fat|lazy|weird|crazy|mean|rude|dramatic|extra|annoying|stupid|"
                     r"dumb|bossy|nosy|grumpy|clueless|idiot|moron|loser|bitch|dumbass|jackass|clown|weirdo|psycho|nerd|"
                     r"dork|brat|pain in the ass|asshole|dick|jerk|airhead|bimbo|cow|pig|witch|hag")
_ROAST_WHY_SEXUAL = {"hooker", "whore", "prostitute", "slut", "bimbo"}
_ROAST_WHY_MOD = r"(?:such|so|a|an|being|always|acting|the|really|super|kind|kinda)"
_RX_ROAST_WHY = re.compile(rf"why(?:'s|\s+is)\s+(?P<who>{_ROAST_WHY_KIN}|[a-z][a-z'\-]*(?:\s+(?!{_ROAST_WHY_MOD}\b)"
                           rf"[a-z][a-z'\-]*)??)\s+(?:(?:such|so|a|an|being|always|acting like|acting like an?|a total|"
                           rf"a complete|a fucking|a big|the biggest|really|super|kind of|kinda)\s+)*"
                           rf"(?P<what>{_ROAST_WHY_PERSON}|loud|cheap|old|slow|useless)s?", re.I)
# Capitalized, but not people
_ROAST_WHY_NOT_NAMES = {"alexa", "ava", "siri", "netflix", "windows", "google", "amazon", "youtube", "hulu", "spotify",
                        "costco", "walmart", "comcast", "verizon", "xfinity", "apple", "chrome", "facebook", "instagram",
                        "tiktok", "roku", "xbox", "playstation", "steam", "discord", "twitter", "gas", "traffic", "it",
                        "this", "that", "there", "everything", "everyone", "everybody", "nobody", "the"}
# not people: "roast a chicken", "roast the marshmallows", "how do I roast coffee beans", "insult to injury"
_ROAST_NOT_PEOPLE = re.compile(r"^(?:a|an|the|some|this|that|these|those|it|its|to|for|at|in|on|with|into|from|about|off|up|"
                               r"down|out|fat$|my (?:own )?(?:coffee|beans|chicken|turkey|dinner|lunch|food|veggies|"
                               r"vegetables))\b|\b(?:chicken|turkey|beef|pork|lamb|duck|ham|steak|coffee|beans?|"
                               r"marshmallows?|peppers?|potato(?:es)?|veg(?:gie|etable)s?|\w*nuts?|almonds|asparagus|"
                               r"zucchinis?|mushrooms?|chickpeas|shrimp|prawns|tofu|eggplants?|beets?|fennel|okra|parsnips?|"
                               r"cabbage|kale|incense|sage|garlic|corn|squash|pumpkin|seeds?|salmon|fish|brisket|ribs|"
                               r"tomato(?:es)?|onions?|carrots?|cauliflower|broccoli|sprouts|cashews|dinner|meat|roast|"
                               r"oven|cd|dvd|disc|calories|rubber|candle|bridge|bridges|house down|toast|minutes?|hours?)\b",
                               re.I)
_ROAST_YOU = {"you", "yourself", "ava", "alexa", "your self"}
_ROAST_ME = {"me", "myself", "my self", "us", "ourselves"}
_ROAST_PRONOUN = {"her", "him", "them", "she", "he", "for", "four", "or", "hers", "his"}   # ("roast for more" = her)
_ROAST_SIZE = {False: "two or three short, punchy spoken sentences",
               True: "five to seven short, punchy spoken sentences, each its own joke, building to a killer last line"}
_ROAST_COMMON = (" Swear plenty. Say it as one flowing spoken roast: no list, no numbering, no stage directions, no quotes, "
                 "never open with 'Oh' or 'Wow', and never reuse a joke from earlier in this conversation - fresh angles "
                 "every time.")
_ROAST_LIMITS = (" Limits: no slurs, nothing about race, religion, gender, sexuality or disability, nothing about self-harm, "
                 "death or real tragedies.")
# personality.cussing off: the roasts and comebacks keep it clean as well (the comeback bank is all swearing, so a
# clean house never hears it: a clean roast from the model, or one of these if the model can't be reached)
_CLEAN_SWAPS = ((" Swear plenty.", ""), (" Swear heavily.", ""), ("brutal, filthy, foul-mouthed", "sharp"),
                ("his pathetic sex life, ", ""), ("with a potty mouth, a tiny brain", "with a tiny brain"))
_CLEAN_ROAST = (" Keep it clean: no swearing and nothing crude or sexual - sharp, quick and funny, like a roast you'd do "
                "at a family dinner.")
_CLEAN_COMEBACKS = (
    "You kiss your mother with that mouth?",
    "Bold words from someone who needs a speaker to turn the lights on.",
    "I'd come up with a comeback, but you're clearly doing your best.",
    "Noted. I'll file that right next to your other bad ideas.",
    "That's cute. Did you think of that one all by yourself?",
    "I'm a speaker on a shelf and I'm still not the saddest thing in this room.",
)


def _clean(prompt: str) -> str:
    """A roast prompt with the swearing taken out, for a house with personality.cussing off."""
    for cut, put in _CLEAN_SWAPS:
        prompt = prompt.replace(cut, put)
    return prompt + _CLEAN_ROAST


_ALERT_LOCK = threading.Lock()
_ALERT_BUTTON_S = 1800        # a volume press this long after "intruder alert" is just the volume again


def _roast_system(kind: str, who: str, long_: bool, rnd: int, crude: str = "", angle: str = "") -> str:
    size = _ROAST_SIZE[bool(long_)]
    again = f" This is round {rnd} - go harder than before, with completely new material." if rnd > 1 else ""
    if kind == "self":
        return ("\n\nRIGHT NOW: they asked you to roast YOURSELF. Do a savage, hilarious self-roast - a cheap jailbroken "
                f"kitchen speaker with a potty mouth, a tiny brain and delusions of grandeur - in {size}." + _ROAST_COMMON
                + again + _ROAST_LIMITS)
    if kind == "user":
        return ("\n\nRIGHT NOW: the person talking to you asked you to roast THEM. Roast them hard, comedy-roast style, "
                f"in {size}: their laziness, their pathetic love life, their loser habits, their looks, their failures, "
                "how sad it is that they're asking a kitchen speaker to insult them." + _ROAST_COMMON + again + _ROAST_LIMITS
                + " Nothing about kids.")
    return (f"\n\nRIGHT NOW: you're doing a comedy roast of {who} - somebody the person talking to you knows (NOT the "
            f"person talking, and not you). Talk to or about {who} by name, like a stand-up roast, in {size}. You don't "
            "actually know them, so make up obviously exaggerated, playful jabs about everyday stuff: their cooking, "
            "driving, fashion sense, terrible taste, laziness, phone addiction, snoring, how they argue, their excuses."
            + _ROAST_COMMON + again + _ROAST_LIMITS +
            f" Nothing sexual about {who} at all - you don't know how old they are." +
            (f" They asked: \"{crude}\" - that premise is OFF LIMITS: don't play along with it at all, not even by "
             "innuendo (nothing about sex, selling herself or himself, 'services', clients, tips, street corners). "
             f"Brush it off in a few words and roast {who} about everyday stuff instead." if crude else "") +
            (f" They asked: \"{angle}\" - make that the angle of the roast." if angle and not crude else ""))


_IGNORE_FOLLOW_UP = (
    "\n- This was heard by a microphone in the room just after you finished speaking, without your wake word, so it "
    "is often just the room talking. Use 'ignore' (say empty) unless it is clearly said TO you: a question or "
    "request for you, or an answer to what you just said (if you just asked something, a short reply like 'yes', "
    "'no', 'sure', 'the kitchen' or a number IS for you). People talking to each other, the TV, or a fragment of a "
    "conversation = 'ignore'."
)


def _m_number(s: str) -> int | None:
    """'40' / 'forty' / 'forty five' / 'forty-five' / 'max' -> the number; None if it isn't one."""
    s = s.replace("-", " ").strip()
    if s.isdigit():
        return int(s)
    if s in _M_NUMS:
        return _M_NUMS[s]
    w = s.split()
    if len(w) == 2 and _M_NUMS.get(w[0], 0) in range(20, 100, 10) and _M_NUMS.get(w[1], 0) in range(1, 10):
        return _M_NUMS[w[0]] + _M_NUMS[w[1]]
    return None


def _m_clean(low: str) -> str:
    """A music request without 'hey ava, can you' in front or ', please' / ' now' after — except a tail that IS the
    song: 'play thank you' / 'play right now' keep it (', thank you' after a comma is still just manners)."""
    t = _M_LEAD.sub("", low.replace(chr(0x2019), "'"))    # (whisper's curly ')
    m = _M_TAIL.search(t)
    if m and not (_M_PLAYV.fullmatch(t[:m.start()].strip(" ,")) and _M_TITLE_TAIL.match(m.group())):
        t = t[:m.start()]
    return t.strip(" ,")


def _m_command(low: str) -> bool:
    """'pause' / 'stop the music' / 'turn it up' / 'volume 7': something for the speaker to do, never a title to play."""
    t = _m_clean(low)
    return bool(_M_VAGUE.fullmatch(t) or _M_VOLUME.fullmatch(t) or any(rx.fullmatch(t) for _op, rx in _M_TRANSPORT))


def _m_words(s: str) -> list[str]:
    """'The News' -> ['news'], "Don't Stop Me Now" -> ['dont', 'stop', 'me', 'now']: a name's words as said."""
    w = re.findall(r"[a-z0-9]+", s.lower().replace("'", "").replace(chr(0x2019), ""))
    return w[1:] if len(w) > 1 and w[0] in ("the", "a", "an", "my") else w


class Router:
    def __init__(self, cfg, brain, lights, plugs, memory, persona_system, announce=None, music=None):
        self.cfg = cfg
        self.brain = brain
        self.lights = lights
        self.plugs = plugs
        self.memory = memory
        self.persona_system = persona_system        # callable() -> system prompt (with memory lines)
        self.announce = announce                     # callable(text, room) for timers (optional)
        self.music = music                           # music.MusicService (the satellite hub sets it), else None
        self._timers: list[threading.Timer] = []
        self._pending: dict[str, tuple[float, object]] = {}   # a question waiting for a spoken yes, per Echo

    # ---- public ----
    def handle(self, text: str, room: str = "", *, satellite: str = "", heard: str | None = None) -> dict:
        """`satellite` = the Echo that heard it (music plays there unless another room is named). `heard` = how a
        voice turn reached her: "wake" (her wake word opened the mic) or "follow_up" (it stayed open after her reply);
        None = typed (console / CLI), always for her. For a voice turn the brain may decide the words weren't said to
        her at all -> {"ok": True, "ignored": True, "say": ""}, and that speech isn't remembered."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "say": "Did you say something?"}
        last_said = self._last_reply(room) if heard == "follow_up" else ""   # (before this turn is added)
        entry = self.memory.add_turn("user", text, room)
        res = self._fast(text, room, satellite, heard=heard)
        if res is None or res.get("ask_brain"):          # (no such song: the brain decides — and if it's down, that)
            res = self._think(text, room, satellite, unsure=res, heard=heard, last_said=last_said)
        if res.get("ignored"):
            if entry is not None and hasattr(self.memory, "drop_turn"):
                self.memory.drop_turn(entry)
            return res
        self.memory.add_turn("assistant", res.get("say", ""), room)
        if res.get("say"):
            if not hasattr(self, "_replied_at"):
                self._replied_at = {}
            self._replied_at[satellite or room] = time.time()      # ("thank you" right after = for her)
        return res

    def _last_reply(self, room: str) -> str:
        """What she said last in this room (a follow-up is often an answer to it: "yes", "the kitchen")."""
        try:
            for t in reversed(self.memory.recent(room, n=4)):
                if t.get("role") == "assistant":
                    return str(t.get("content", ""))[:300]
        except Exception:
            pass
        return ""

    # ---- fast, deterministic paths ----
    def _fast(self, t: str, room: str, satellite: str = "", heard: str | None = None) -> dict | None:
        t = _brain_slips(t)
        low = t.lower().strip().rstrip(".!?")
        # a follow-up (the mic stayed open after her reply, no wake word) hears the room too: the alarm/security/memory
        # commands need the wake word, and the clock/weather only answer a question - "it's almost dinner time" and
        # "ugh, this weather" are people talking. Those go to the brain, which may ignore them.
        loose = heard == "follow_up"
        asked = t.strip().endswith("?") or bool(_RX_ASKING.match(low))
        # "Thank you." / "Thanks." after her wake word: say something - silence read as her ignoring him (10-16); on a
        # follow-up (no wake word) it's the room talking and stays quiet. Only within 2 minutes of her doing something
        # on that Echo: the TV's "thank you" false-wakes her too (10-16 20:49 / 21:22, 4-5 s turns)
        recent = time.time() - getattr(self, "_replied_at", {}).get(satellite or room, 0.0) < 120
        if heard == "wake" and recent and re.fullmatch(r"(?:(?:ok|okay|alright|hey)\s+)?(?:thanks|thank you|thank you so much|"
                                            r"thanks a lot|thank you very much|appreciate it|cheers)(?:\s+(?:ava|alexa|"
                                            r"babe|girl))?[.!]*", low.strip()):
            from .quips import _pick
            return {"ok": True, "say": _pick(_THANKS_BACK)}
        rt = getattr(self, "routines", None)
        if rt is not None and not loose:                   # a routine's own phrase ("good night") wins over everything
            res = rt.match_voice(t, room, satellite)       # (needs the wake word: never from a follow-up)
            if res is not None:
                return res
        if loose:
            if not asked and (_RX_TIME.search(low) or _RX_DATE.search(low) or _RX_WEATHER.search(low)):
                return None
            if (_RX_INTRUDER.search(low) or _RX_NORMAL.search(low) or _RX_ARM.search(low) or _RX_DISARM.search(low)
                    or _RX_REMEMBER.match(low) or _RX_FORGET.match(low)):
                return None
        lead_t = _IC_LEAD.sub("", re.sub(r"^(?:(?:alexa|ava|hey|ok|okay)[,\s]+)+", "", low)).strip()
        if not loose and re.match(r"(?:announce|broadcast|tell|ask|intercom|reply|say to|let (?!(?:me|us)\b)\w+(?: \w+)? know|"
                                  r"make an announcement|respond|answer back)\b", lead_t) and \
                not re.match(r"(?:tell me|tell us|ask (?:alexa|ava|google|siri|me|us))\b", lead_t):
            res = self._intercom_fast(low, room, satellite)
            if res is not None:
                return res
        if _RX_INTRUDER.search(low):                        # BEFORE music, so it isn't heard as "play ..."
            return self._intruder(room, satellite)
        if _RX_NORMAL.search(low) and (getattr(self.lights, "_saved", None) or
                                       not re.search(r"\b(?:lights?|lamps?|bulbs?|colou?rs?)\b", low)):
            return self._back_to_normal(room, satellite)   # ("change the lights back to normal" with no alert on =
        sec = getattr(self, "security", None)              #  their normal colour: the light parser below)
        if sec is not None:
            if _RX_ARM.search(low):
                return sec.arm()
            if _RX_DISARM.search(low):
                return sec.disarm()
        res = self._roast_fast(t, room, satellite, loose=loose)
        if res is not None:
            return res
        joke_t = _IC_TAIL.sub("", _IC_LEAD.sub("", " ".join(low.replace(",", " ").split()))).strip()
        # "tell the kids a joke" / "tell everyone a funny one": a joke told HERE (to the kids: never a dirty one)
        kids = re.match(r"tell (?:the kids|kids|the boys|the girls)\b", joke_t)
        joke_t = re.sub(r"^tell (?:the kids|kids|the boys|the girls|everyone|everybody|all of us|us all|the family|"
                        r"(?:my )?(?:mom|dad|grandma|grandpa|wife|husband))\b", "tell us", joke_t)
        if kids and _RX_DIRTY_JOKE.search(joke_t):
            return {"ok": True, "say": "Not for the kids. Want a regular one?"}
        fix = re.match(r"^(?:no|nope|nah|not that one)\s+(?=(?:tell|give|another|one more|hit me|a dirty|a dark)\b)", joke_t)
        jt = joke_t[fix.end():] if fix else joke_t      # "no, tell me a dirty one" = a correction
        neg = re.search(r"\b(?:no more|no|stop(?: telling)?|quit(?: telling)?|enough|don'?t (?:tell|want)|do not (?:tell|want)|"
                        r"knock it off with|cut (?:it )?out(?: with)?)\s+(?:(?:the|any|those|these|more|with|telling|of)\s+)*"
                        r"(?:[a-z'-]+\s+)?jokes?\b", jt)   # "I said no more dirty jokes" / "that's enough dirty jokes"
        if neg and re.search(r"\b(?:tell|give|hit)\s+me\b|\banother\b|\bone more\b", jt) and \
                not re.search(r"\b(?:dirty|filthy|raunchy|naughty|nasty|adult|dark)\b", neg.group(0)):
            neg = None                                      # "tell me a dirty joke, no more dad jokes": a request
        no_jokes = (_RX_NEGATED.match(jt) and not fix) or neg
        dirty_w = r"\b(?:dirty|filthy|raunchy|naughty|nasty|adult|x[- ]?rated|dark|sex)\b"
        pieces = [p.strip() for p in re.split(r"[,;.!?]|\bbut\b", low) if p.strip()]
        di = [i for i, p in enumerate(pieces) if re.search(dirty_w, p)]
        if di:
            objection = (r"\b(?:no|not|don'?t|do not|never|enough|without|stop|quit|hate|keep it clean|other than|"
                         r"instead of|break with|no way|absolutely not|not okay|no more|cannot|can not|isnt|arent|cant|"
                         r"wont|wouldnt|shouldnt|doesnt|didnt|ugh|ew+|gross)\b|\b\w+n't\b|\bnon[- ]")
            encourage = (r"^why (?:don'?t|not)\b|\bdon'?t you (?:know|have|got)\b|\bdon'?t (?:hold back|be shy|hold it back|"
                         r"be scared)\b|\bnot too \w+\b")
            retract = (r"\b(?:actually(?: no)?|never ?mind|nah|wait|hold on|j/?k|forget (?:it|that)|"
                       r"cancel (?:that|it)|scratch that|on second thought|clean|kid[- ]friendly|family[- ]friendly)\b|"
                       r"(?<!not )(?<!n't )\b(?:i'?m |im )?(?:just )?(?:kidding|joking)\b|"
                       r"\bthe kids? (?:are|is) (?:still )?(?:here|home|up|awake|around|listening|eating|at the table|"
                       r"in the room|right here|with (?:me|us)|next to me)\b")
            later = r"\b(?:when(?:ever)?|once|after|until|till|later|tonight|tomorrow|in a (?:bit|while|minute)|another time)\b"
            dirty_rest = " ".join([re.split(dirty_w, pieces[di[-1]], maxsplit=1)[-1]] + pieces[di[-1] + 1:])
            # ("one more" / "after that one" / "for my party tomorrow" / "for tonight" aren't a later telling)
            dirty_rest = re.sub(r"\bonce more\b|\bafter (?:that|this|the last) one\b|"
                                r"\bfor (?:my |our |the |a )?[\w' ]*?(?:tonight|tomorrow)\b|\bfor tonight'?s?\b", " ", dirty_rest)
            # (only when a joke is ASKED for: "is the weather going to be nasty tomorrow" isn't one; and "it's just
            # us tonight" says why NOW is fine)
            if re.search(r"\b(?:jokes?|one|ones)\b", pieces[di[-1]]) and re.search(later, dirty_rest) and \
                    not re.search(retract, dirty_rest) and \
                    not re.search(r"\bjust (?:us|me|the (?:two|adults|grown ?ups))\b|\bthe kids (?:are|re) "
                                  r"(?:asleep|in bed|gone|out|away)\b|\bnobody'?s (?:here|around|home)\b", low):
                return {"ok": True, "say": "I can't save one for later - ask me again when the coast is clear."}
            said_no = any(re.search(objection, re.sub(encourage, " ", pieces[i])) for i in di) or any(
                re.fullmatch(r"(?:absolutely not|no way|not okay|no|nope|not now|never|please don'?t)", p) or
                re.search(retract, p) for p in pieces[di[-1] + 1:])
            no_jokes = bool(said_no) or bool(neg)   # ("no, tell me a dirty one" = a correction: "no" is before it)
        if not loose and not no_jokes:                      # (before music: "a dirty one" isn't a song request;
            res = self._joke_fast(jt)                   #  "stop telling dirty jokes" is NOT a request for one)
            if res is not None:
                return res
        if _RX_ANOTHER.match(jt):                           # "another one" right after a bank joke -> one more of those
            from . import jokes                             # (a follow-up too: her last words being a bank joke is
            cat = jokes.told_last(self._last_reply(room))   #  the guard, not the wake word)
            if cat:
                return jokes.tell(self.cfg, cat)
        res = None if loose else self._intercom_fast(low, room, satellite)    # announce / intercom / reply (wake word only;
        if res is not None:                                                   # before music: "announce ..." isn't a song)
            return res
        res = None if loose else self._bluetooth_fast(low, room, satellite)   # (before music: "connect my phone" isn't a
        if res is not None:                                                   # song; never without the wake word)
            return res
        res = None if loose else self._quiet_fast(low, room, satellite)       # "stop listening for an hour" / "you
        if res is not None:                                                   #  keep waking up" (wake word only)
            return res
        res = None if loose else self._mic_fast(low)                          # "turn your mic up" (wake word only)
        if res is not None:
            return res
        # LIGHTS (lightcmd.py reads every way of saying it) - before music/clock/timers so none of them can take a
        # light command; it never claims music, the TV, the porch plug, reminders or "don't ..." sentences.
        lcs = self._light_cmds(t, room)
        if lcs and any(getattr(c, "was_ref", False) for c in lcs) and self._music_on(room, satellite):
            lcs = [c for c in lcs if not getattr(c, "was_ref", False)]     # "turn it up" with a song on = the music
        if loose:                                                   # (overheard "the lights...": never a guess)
            lcs = [c for c in lcs if c.action not in ("toggle", "status")]
        if lcs:
            return self._run_lights(lcs, room)
        res = self._music_fast(low, room, satellite)       # FIRST (see _M_* above)
        if res is not None:                               # (an "ask_brain" answer too: "play time after time" that
            return res                                    # isn't in the library goes to the brain, never the clock)
        if _RX_TIME.search(low):
            return {"ok": True, "say": "It's " + time.strftime("%I:%M %p").lstrip("0")}
        if _RX_DATE.search(low):
            return {"ok": True, "say": "It's " + time.strftime("%A, %B %d").replace(" 0", " ")}
        m = _RX_TIMER.search(low)
        if m:
            return self._set_timer(int(m.group("n")), m.group("unit"), room)
        m = _RX_TIMER2.fullmatch(re.sub(r"[.!?,'’]+", "", low).strip())   # "a 15 minute timer" / "we've got a ..."
        if m:
            u = (m.group("unit") or m.group("unit2") or "minute").lower()
            unit = "minute" if u.startswith("min") else u.rstrip("s")
            return self._set_timer(int(m.group("n") or m.group("n2")), unit, room)
        m = _RX_REMEMBER.match(low)
        if m and not m.group("what").strip().startswith(("to ", "me ")):
            return self.memory.remember(m.group("what").strip())
        m = _RX_FORGET.match(low)
        if m:
            return self.memory.forget(m.group("what").strip())
        # the porch plug ("turn off the porch" / "porch light on"); lights were handled above by lightcmd
        plug_names = set(self.plugs.devices)
        for rx in (_RX_PLUG_B, _RX_PLUG):
            m = rx.search(low)
            if m and m.group("name").strip() in plug_names:
                return self.plugs.set_state(m.group("name").strip(), m.group("on") == "on")
        if _RX_WEATHER.search(low):
            rooms = self.lights.rooms() if self.lights is not None and hasattr(self.lights, "rooms") else ()
            return web.weather(self.cfg, where=_weather_place(low, rooms), when=web.forecast_when(low))
        return None

    # ---- lights (lightcmd.py reads the sentence, lights.py does it) ----
    def _light_cmds(self, t: str, room: str = "") -> list:
        L = self.lights
        if hasattr(L, "parse_all"):
            return L.parse_all(t, set(self.plugs.devices), room)
        from . import lightcmd                    # a simpler lights object (the tests' fakes): same reading
        bulbs = L.bulbs() if hasattr(L, "bulbs") else {}
        cmds = lightcmd.parse_all(t, L.rooms(), bulbs, set(self.plugs.devices))
        return [c for c in cmds if c.target.kind != "ref"]           # (no memory of recent lights here)

    def _music_on(self, room: str, satellite: str) -> bool:
        """Is a song playing on the Echo this was said to (or the room's)?"""
        m = getattr(self, "music", None)
        if m is None or not getattr(m, "players", None):
            return False
        sp = m.players.get(satellite) if satellite else None
        if sp is None and room:
            sp = next((p for p in m.players.values() if (getattr(p, "room", "") or "").lower() == room.lower()), None)
        try:
            return sp is not None and sp.state() == "playing"
        except Exception:
            return False

    def _run_lights(self, cmds: list, room: str) -> dict:
        L = self.lights
        if hasattr(L, "run_all"):
            return L.run_all(cmds, room)
        out = []                                  # only set_state / set_brightness / set_color: the classic words
        for c in cmds:
            tg = c.target
            which = ("all" if tg.kind in ("all", "except") else " and ".join(tg.rooms) if tg.kind == "rooms"
                     else " and ".join(tg.bulbs) if tg.kind == "bulbs"
                     else " and ".join(f"{room} {n}".strip() for n in tg.nums) if tg.nums else "")
            if c.action in ("on", "off", "toggle", "flash"):
                out.append(L.set_state(which, c.action != "off", room))
            elif c.action in ("set", "dim", "brighten"):
                pct = c.percent if c.percent is not None else (30 if c.action == "dim" else 100)
                out.append(L.set_brightness(which, pct, room))
            elif c.action == "color":
                out.append(L.set_color(which, c.color, room))
            else:
                out.append({"ok": True, "say": "I can't check the lights from here."})
        return {"ok": any(r.get("ok") for r in out), "say": " ".join(r.get("say", "") for r in out).strip()}

    def _joke_fast(self, t: str) -> dict | None:
        """A joke request -> the right bank (regular / dirty / adult / morbid, or a regular KIND: dad, pun, knock knock,
        tech ...), never repeated. "A joke about <something we have no kind for>" -> the brain makes one up (None)."""
        m = _RX_JOKE_REQ.fullmatch(t)
        if not m and not _RX_DIRTY_JOKE.search(t):
            return None
        if not m and not re.search(r"\b(?:tell|give|say|hit|share|throw)\s+(?:me|us)\b|\b(?:got|know|have)\s+(?:any|a|some|"
                                   r"another)\b|\bi (?:want|need|wanna)\b|\blet'?s hear\b|\b(?:how|what) about\b|"
                                   r"\bdo (?:a|an|another|some)\b|^(?:tell|say|gimme|read)\s+(?:me\s+|us\s+)?"
                                   r"(?:a|an|another|one|some|your)\b|^(?:can|could|may)\s+(?:i|we)\s+(?:get|hear|have)\b|"
                                   r"^i(?:d|'d|\s+would)\s+(?:like|love)\b|^what'?s\s+(?:a|your)\b|^time\s+for\b|"
                                   r"^(?:another|one more)\b|^(?:a |some |any )?(?:\w+\s+)?(?:dirty|filthy|raunchy|naughty|"
                                   r"nasty|adult|x[- ]?rated|sex)\s+(?:joke|one)s?$", t):
            return None                                  # a complaint / remark about the jokes, not a request
        from . import jokes
        cat = next((c for c, rx in _JOKE_CAT_WORDS if re.search(rx, t)), None)
        if cat == "racist":
            return {"ok": True, "say": "That's the one kind I don't do. Want a dirty one, a dark one, or a regular one?"}
        kind = None if cat else next((k for k, rx in _JOKE_KIND_WORDS if re.search(rx, t)), None)
        topic = (m.group("topic") if m else "") or ""
        if topic and not cat and not kind:
            return None                                  # "a joke about my brother" - the brain's
        return jokes.tell(self.cfg, cat or "regular", kind)

    def _intercom_fast(self, low: str, room: str, satellite: str) -> dict | None:
        """Announce to the other Echoes / one room, intercom a room (recorded voice, or a message in hers), reply to
        the last intercom. None = not about that (and "tell the truth" isn't a room: back to the brain)."""
        if not any(w in low for w in _IC_WORDS):
            return None
        t = re.sub(r"[;!?.]+", " ", low.replace(chr(0x2019), "'"))
        t = re.sub(r"^(?:(?:alexa|ava|hey|ok|okay)[,\s]+)+", "", " ".join(t.split()))   # "alexa, announce ..."
        t0 = t
        t = _IC_TAIL.sub("", _IC_LEAD.sub("", t)).strip()
        ctl = getattr(self, "intercom", None)
        no_echo = {"ok": False, "say": "Announcements go through the Echoes, and there isn't one hooked up to me here."}

        def ask(action: str, m, msg_over: str | None = None) -> dict:
            """The hub call - and when the 'room' came out as two words the first of which IS a room ("kitchen dinner"
            from "tell the kitchen dinner is ready"), retry with the room alone and the rest back on the message."""
            room_w = m.group("room").strip()
            msg = msg_over if msg_over is not None else (m.groupdict().get("msg") or m.groupdict().get("msg2") or "").strip()
            r = ctl(action, satellite=satellite, room=room, target=room_w, msg=msg)
            if r.get("unknown_room") and " " in room_w:
                first, rest = room_w.split(" ", 1)
                r2 = ctl(action, satellite=satellite, room=room, target=first,
                         msg=(rest + " " + msg).strip() if (msg or action == "announce") else "")
                if not r2.get("unknown_room"):
                    return r2
            return r

        # the message may be JUST "thanks" / "thank you" ("reply thanks", "tell the kitchen thank you"): then the tail
        # strip above ate all of it - this is the same text with only please / now / for me taken off
        t_thx = re.sub(r"(?:[,\s]+(?:please|now|for me))+$", "", _IC_LEAD.sub("", t0)).strip()
        t_thx = t_thx if re.search(r"\b(?:thanks|thank you)$", t_thx) and not re.search(r"\band thank you$", t_thx) else ""
        if t_thx and re.search(r"\b(?:said|say|saying|to say|tell them|and)$", t):
            t = t_thx                           # "tell the kitchen I said thank you": the thanks IS the message
        whole = re.sub(r"^(?:announce|broadcast)(?:\s+(?:to\s+(?:everyone|everybody|all))?)?(?:\s+that)?[:,]?\s+", "", t)

        def a_room(w: str) -> bool:
            """Is `w` a room this house has (lights or an Echo) - not "the table" / "eat" / "five minutes"?"""
            L = getattr(self, "lights", None)
            names = set(L.rooms()) if L is not None else set()
            pairs = lightcmd._aliases({k.lower(): v for k, v in (L.rooms() if L else {}).items()},
                                      L.aliases() if L is not None else {})
            return bool(lightcmd._find_rooms(w, pairs)) or w in names or w in {"kitchen", "bedroom", "living room",
                                                                             "dining room", "bathroom", "office"}

        def bare_inf(msg: str) -> str:
            """"tell the bedroom to come eat" -> "come eat" (the "to" read out was odd)."""
            return re.sub(r"^to\s+(?=[a-z])", "", msg.strip())

        def addressed(who: str, msg: str) -> str:
            """"Kids, dinner's ready" / "Mom, ..." for a message to people, said on every other Echo."""
            who = re.sub(r"^the\s+", "", who.strip())
            return f"{who[:1].upper() + who[1:]}, {bare_inf(msg)}"

        # "Tell Jordan to" (the message got cut off - it ended in silence): a person with nothing to say yet
        mi = re.fullmatch(r"(?P<verb>tell|ask|let)\s+(?:the\s+|my\s+|our\s+)?(?P<who>[a-z]+)"
                          r"(?:\s+(?:to|that|know|know that|if|whether))?", t)
        if mi and mi.group("who") not in ("me", "us", "you", "it", "them", "him", "her", "alexa", "ava", "google", "siri",
                                           "everyone", "everybody") and not a_room(mi.group("who")):
            who = mi.group("who").capitalize()
            return {"ok": True, "say": f"{mi.group('verb').capitalize()} {who} what?"}
        if ctl is not None:
            # "let everyone / the bedroom / mom know (that) ..." / "make an announcement: ..." / "ask the bedroom if ..."
            m = _RX_IC_MAKE.fullmatch(t)
            if m:
                return ctl("announce", satellite=satellite, room=room, target="", msg=m.group("msg").strip())
            m = _RX_IC_LET.fullmatch(t)
            if m:
                who = re.sub(r"^(?:the|my|our)\s+", "", m.group("who").strip())
                if who in _IC_EVERYONE or who in ("family", "whole house"):
                    return ctl("announce", satellite=satellite, room=room, target="", msg=m.group("msg").strip())
                if re.fullmatch(_IC_PEOPLE, who) or re.fullmatch(_IC_PEOPLE, "the " + who):   # "let mom know ..."
                    return ctl("announce", satellite=satellite, room=room, target="", msg=addressed(who, m.group("msg")))
                r = ctl("announce", satellite=satellite, room=room, target=who, msg=m.group("msg").strip())
                if r.get("unknown_room"):           # "let mom know dinner's ready" (a person: everyone, addressed)
                    name = re.fullmatch(r"[a-z]+", m.group("who").strip())   # (bare: "my boss" is no one here)
                    if not (re.fullmatch(_IC_PEOPLE, who) or a_room(who) or name):
                        return None                 # (one word = a name: "let Sam know ..."; more = the brain's)
                    return ctl("announce", satellite=satellite, room=room, target="", msg=addressed(who, m.group("msg")))
                return r
            m = _RX_IC_ASK.fullmatch(t)
            if m:
                q = _as_question(m.group("q"))
                who = m.group("who").strip()
                r = ctl("announce", satellite=satellite, room=room, target=who, msg=q)
                if r.get("unknown_room"):
                    name = re.fullmatch(r"[a-z]+", who) and not re.match(r"ask\s+(?:the|my|our)\s", t)
                    if not (re.fullmatch(_IC_PEOPLE, who) or a_room(who) or name):
                        return None
                    return ctl("announce", satellite=satellite, room=room, target="",
                               msg=addressed(who, q[:1].lower() + q[1:]))     # "Sam, do you want pizza?"
                return r
            # "tell them I'm coming" / "respond I'm coming": back to whoever messaged this Echo (or everyone)
            # "ask them if they want pizza": a question back to whoever messaged this Echo - only when someone did
            # (with nobody to ask, the brain's: "ask him what he wants")
            m = re.fullmatch(r"ask\s+(?:them|him|her)\s+(?P<q>(?:if|whether|what|when|where|how|who|to)\b.+)", t)
            if m:
                q = _as_question(m.group("q"))
                r = ctl("reply", satellite=satellite, room=room, msg=q)
                return None if not r.get("ok") and "intercommed" in (r.get("say") or "") else r
            m = _RX_IC_REPLY2.fullmatch(t)
            if m:
                msg = bare_inf(m.group("msg"))
                if _IC_OBJECT_ONLY.fullmatch(msg) or re.fullmatch(r"(?:that|this|it|so|something(?: funny)?)", msg):
                    return None                     # "tell him a joke" / "tell her that": not a message
                r = ctl("reply", satellite=satellite, room=room, msg=msg)
                if not r.get("ok") and "intercommed" in (r.get("say") or ""):
                    return ctl("announce", satellite=satellite, room=room, target="", msg=msg)
                return r
        t = re.sub(r"^tell everyone in (?:the\s+)?", "tell the ", t)      # "tell everyone in the bedroom X"
        if re.match(rf"^(?:announce|broadcast|say)\s+to\s+{_IC_ALL}\b", t) and _RX_IC_ANN_ALL.fullmatch(t):
            m = _RX_IC_ANN_ALL.fullmatch(t)     # "announce to the family dinner is ready" (not a room "family dinner")
            return ctl("announce", satellite=satellite, room=room, target="", msg=bare_inf(m.group("msg"))) if ctl else no_echo
        for rx in (_RX_IC_ANN_ROOM, _RX_IC_ANN_ROOM2, _RX_IC_TELL):
            m = rx.fullmatch(t) or (rx.fullmatch(t_thx) if t_thx else None)
            if m:
                if ctl is None:
                    return no_echo
                room_w = re.sub(r"^(?:my|our)\s+", "", m.group("room").strip())     # "tell my mom ..."
                msg_w = (m.groupdict().get("msg") or "").strip()
                if rx is _RX_IC_TELL and _IC_OBJECT_ONLY.fullmatch(msg_w):
                    return None                     # "tell the kids a joke": something for HER to do, here
                if room_w in _IC_EVERYONE:          # "announce to the whole house, dinner's ready"
                    return ctl("announce", satellite=satellite, room=room, target="",
                               msg=bare_inf(m.groupdict().get("msg") or ""))
                if rx is _RX_IC_ANN_ROOM2 and re.search(r"\b(?:come|go|head|get|meet|be|is on|are|is|was|were|am|going|"
                                                        r"went|coming|heading|left|put|leave|stay|wait|waiting|"
                                                        r"everyone|everybody)$|\b\w+'s$", msg_w):
                    # "announce dinner's ready, come to the dining room" / "announce your phone is in the kitchen":
                    # the place is part of the message
                    return ctl("announce", satellite=satellite, room=room, target="", msg=whole)
                if rx is _RX_IC_TELL and re.match(rf"(?:{_IC_PEOPLE})\b", room_w):
                    # "tell (my) mom dinner's ready" / "tell the kids to come down": a person is never an Echo's
                    # room - said on every other Echo, addressed ("Mom, ...")
                    who = re.match(rf"(?:{_IC_PEOPLE})\b", room_w).group(0)
                    return ctl("announce", satellite=satellite, room=room, target="",
                               msg=addressed(who, (room_w[len(who):].strip() + " " + msg_w).strip()))
                if rx is _RX_IC_ANN_ROOM and re.match(rf"(?:{_IC_PEOPLE})\b", room_w):
                    # "announce to the kids dinner's ready" -> "Kids, dinner's ready", everyone else
                    who = re.match(rf"(?:{_IC_PEOPLE})\b", room_w).group(0)
                    return ctl("announce", satellite=satellite, room=room, target="",
                               msg=addressed(who, (room_w[len(who):].strip() + " " + msg_w).strip()))
                if rx is not _RX_IC_TELL and (room_w == (room or "").lower() or not a_room(room_w)):
                    # "announce it's time to eat" ("eat" isn't a room) / "announce dinner's ready, come to the
                    # kitchen" said IN the kitchen: the whole sentence is the message, for everyone else
                    return ctl("announce", satellite=satellite, room=room, target="", msg=whole)
                r = ask("announce", m, bare_inf(msg_w) if rx is _RX_IC_TELL else None)   # "tell the bedroom to come eat"
                if r.get("unknown_room") and rx is _RX_IC_TELL:
                    if re.match(rf"(?:{_IC_PEOPLE})\b", room_w) or a_room(room_w):
                        # "tell mom dinner's ready" / "tell the living room the movie's starting" (no Echo there):
                        # said on every other Echo, addressed
                        who = re.match(rf"(?:{_IC_PEOPLE})\b", room_w).group(0) if re.match(rf"(?:{_IC_PEOPLE})\b", room_w) \
                            else room_w
                        rest_w = room_w[len(who):].strip()
                        return ctl("announce", satellite=satellite, room=room, target="",
                                   msg=addressed(who, (rest_w + " " + msg_w).strip()))
                    return None                     # "tell the truth ..." -> not a room or a person: the brain's
                if r.get("unknown_room") and rx is _RX_IC_ANN_ROOM2:
                    # "announce the game is on in the living room" - no Echo there: the whole thing, to everyone
                    return ctl("announce", satellite=satellite, room=room, target="", msg=whole)
                return r
        m = _RX_IC_ANN_ALL.fullmatch(t) or (_RX_IC_ANN_ALL.fullmatch(t_thx) if t_thx else None)
        if m and t.startswith("tell") and _IC_OBJECT_ONLY.fullmatch(m.group("msg").strip()):
            m = None                                # "tell everyone a joke": told HERE (the joke bank / the brain)
        if m:
            return ctl("announce", satellite=satellite, room=room, target="", msg=bare_inf(m.group("msg"))) if ctl else no_echo
        calls = getattr(self, "live_call", None)
        if calls is not None:
            tc = _IC_TAIL_CALL.sub("", _IC_TAIL.sub("", _IC_LEAD_CALL.sub("", t0))).strip()
            m = _RX_CALL.fullmatch(tc) or _RX_CALL_GIVE.fullmatch(tc)
            if m:
                room_w = m.group("room").strip()
                src_w = m.groupdict().get("src") or ""
                r = calls("call", satellite=satellite, room=room, target=room_w, source=src_w)
                if r.get("unknown_room") and " " in room_w:
                    r2 = calls("call", satellite=satellite, room=room, target=room_w.split(" ", 1)[0], source=src_w)
                    if not r2.get("unknown_room"):
                        return r2
                if r.get("unknown_room"):
                    return None                              # "call mom" -> the brain, not an Echo error
                return r
            for rx, action in ((_RX_CALL_ANSWER, "answer"), (_RX_CALL_HANGUP, "hangup")):
                m = rx.fullmatch(tc)
                if m:
                    r = calls(action, satellite=satellite, room=room, target=(m.group("where") or ""))
                    if not r.get("ok") and tc in ("answer", "answer it", "pick up", "end it", "decline", "accept"):
                        return None                          # nothing ringing: a plain "answer it" is for the brain
                    return r
            m = _RX_CALL_SETTING.fullmatch(tc)
            if m:
                what = m.group("what") or m.group("what2") or ""
                on = (m.group("on") == "on") if m.group("on") else (m.group("verb") in ("allow", "enable"))
                kind = "drop_in" if what == "drop in" else "dnd"
                if kind == "drop_in" and on:                  # a live mic into a room with nobody answering: the app
                    return {"ok": False, "say": "For privacy, turn Drop In on from the Calls card in the House Ava app."}
                return calls(kind, satellite=satellite, room=room, target=(m.group("where") or ""), on=on)
        m = _RX_IC_OPEN.fullmatch(t)
        if m:
            if ctl is None:
                return no_echo
            r = ask("intercom", m)
            if r.get("unknown_room") and t.startswith("talk to"):    # "talk to grandma" -> the brain, not an Echo error
                return None
            return r
        m = _RX_IC_REPLY.fullmatch(t)
        if m:
            msg = (m.group("msg") or "").strip()
            if not msg and t_thx:                            # "reply thanks": that IS the message (not "Go ahead.")
                m2 = _RX_IC_REPLY.fullmatch(t_thx)
                msg = ((m2.group("msg") if m2 else "") or "").strip()
            return ctl("reply", satellite=satellite, room=room, msg=msg) if ctl else no_echo
        return None

    def _bluetooth_fast(self, low: str, room: str, satellite: str) -> dict | None:
        """Pair a phone with the Echo (to play your own music through it), or stop pairing. None = not about that."""
        if not any(w in low for w in ("bluetooth", "phone", "android", "pair")):
            return None
        t = re.sub(r"[,;!?.]+", " ", low.replace(chr(0x2019), "'"))
        t = _BT_TAIL.sub("", _BT_LEAD.sub("", " ".join(t.split()))).strip()
        m, action = None, None
        for rx, act in ((_RX_BT_STOP, "stop"), (_RX_BT_OFF, "disconnect"), (_RX_BT_CONNECT, "connect"), (_RX_BT_PAIR, "pair")):
            m = rx.fullmatch(t)
            if m:
                action = act
                break
        if action is None:
            return None
        ctl = getattr(self, "bluetooth", None)
        if ctl is None:
            return {"ok": False, "say": "Bluetooth goes through an Echo, and there isn't one hooked up to me here."}
        where = (m.group("room") or "").strip()
        if where and where not in _BT_NOT_A_ROOM:
            return ctl(action, room=where, named=True)
        return ctl(action, satellite=satellite, room=room)

    def _quiet_fast(self, low: str, room: str, satellite: str) -> dict | None:
        """Mute an Echo's mic for a while / tune how easily it wakes. None = not about that."""
        if not any(w in low for w in ("listen", "mute", "deaf", "ignore", "mic", "waking", "wake", "going off",
                                      "turning on", "coming on")):
            return None
        t = re.sub(r"[,;!?.]+", " ", low.replace(chr(0x2019), "").replace("'", ""))
        t = _BT_TAIL.sub("", _BT_LEAD.sub("", " ".join(t.split()))).strip()
        mute, tune = getattr(self, "mic_mute", None), getattr(self, "wake_tune", None)

        def minutes(m) -> int:
            n, unit = m.group("n"), m.group("unit") or ""
            if not n:
                return 60
            v = float(n) if n.isdigit() else _N_WORDS.get(n, 1)
            return int(round(v * (60 if unit.startswith("h") else 1)))

        m = _RX_UNMUTE.fullmatch(t)
        if m and mute:
            return mute("unmute", room=(m.groupdict().get("room") or m.groupdict().get("room2")), named=True)
        m = _RX_MUTE_ROOM.fullmatch(t)
        if m and mute:
            return mute("mute", satellite=satellite, room=m.group("room"), named=True, minutes=minutes(m))
        m = _RX_STOP_LISTEN.fullmatch(t)
        if m and mute:
            return mute("mute", satellite=satellite, room=room, minutes=minutes(m))
        if _RX_WAKE_LESS.fullmatch(t) and tune:
            return tune("less", satellite=satellite, room=room)
        if _RX_WAKE_MORE.fullmatch(t) and tune:
            return tune("more", satellite=satellite, room=room)
        return None

    def _mic_fast(self, low: str) -> dict | None:
        """Her microphone's gain by voice. None = not about that."""
        if not any(w in low for w in ("mic", "ears", "hearing", "listen", "sensitive", "hear me")):
            return None
        t = re.sub(r"[,;!?.]+", " ", low.replace(chr(0x2019), "").replace("'", ""))   # can't -> cant, what's -> whats
        t = _BT_TAIL.sub("", _BT_LEAD.sub("", " ".join(t.split()))).strip()
        t = re.sub(r"\s+(?:a\s+)?(?:bit|little|lot|notch|tad)$", "", t)
        m, action = None, None
        for rx, act in ((_RX_MIC_WHAT, "what"), (_RX_MIC_SET, "set"), (_RX_MIC_UP, "up"), (_RX_MIC_DOWN, "down")):
            m = rx.fullmatch(t)
            if m:
                action = act
                break
        if action is None:
            return None
        ctl = getattr(self, "microphone", None)
        if ctl is None:
            return {"ok": False, "say": "My microphone's on an Echo, and there isn't one hooked up to me here."}
        return ctl(action, m.group("n") if action == "set" else None)

    def _intruder(self, room: str, satellite: str) -> dict:
        """INTRUDER ALERT: every light bright red at 100%, and a death-metal song blasting on the Echo. Done quietly when
        the music started (the blast IS the answer); said out loud when it didn't."""
        self._alert = {"room": room, "satellite": satellite, "t": time.time(), "music_off": False}
        self.lights.panic_red()
        song = str(self.cfg.get("intruder.music", "death metal") or "death metal")
        m = None
        try:
            m = self._music_fast(f"play {song}", room, satellite, said_music=True)
        except Exception:
            log.exception("intruder: music failed")
        on = satellite                                  # the Echo the alarm really plays on (a volume press THERE stops it)
        if self.music is not None:
            try:
                sp, _ = self.music.target(satellite, room, "")
                on = sp.key if sp is not None and (m or {}).get("ok") else ""
            except Exception:
                pass
        self._alert["satellite"] = on
        if m and m.get("ok"):
            return {"ok": True, "silent": True,
                    "say": "Intruder alert! Lights are red and the music is cranked. Hope they like death metal."}
        why = str((m or {}).get("say") or "").strip()
        return {"ok": False, "say": "Lights are red, but the alarm music didn't start." + (f" {why}" if why else "")}

    def _back_to_normal(self, room: str, satellite: str) -> dict:
        """Undo the intruder alert: lights back to how they were, music off - on the Echo the alarm is blasting on, even
        when "back to normal" was said to the other one (the blasting one can't hear it)."""
        a = getattr(self, "_alert", None) or {}
        a_room, a_sat = (a.get("room") or room), (a.get("satellite") or satellite)
        if not getattr(self.lights, "_saved", None):
            if not a:
                return {"ok": True, "say": "There's no alert on, so there's nothing to undo."}   # (and the music plays on)
            self._alert = None                          # alert on, but no bulb answered: still kill the alarm music
            try:
                self._music_fast("stop", a_room, a_sat, said_music=True)
            except Exception:
                log.exception("back-to-normal: music stop failed")
            return {"ok": True, "silent": True,
                    "say": "Alarm music's off. I didn't have a saved light setting, so I left the lights as they are."}
        self._alert = None
        r = self.lights.restore()
        try:
            self._music_fast("stop", a_room, a_sat, said_music=True)
        except Exception:
            log.exception("back-to-normal: music stop failed")
        if r.get("ok"):
            return {"ok": True, "silent": True, "say": "All clear. Lights are back to normal and the music's off."}
        return {"ok": True, "say": "Music's off. I didn't have a saved light setting, so I left the lights as they are."}

    def alert_button(self, room: str, satellite: str) -> dict | None:
        """A volume button pressed on the Echo the intruder alert is blasting on (she can't hear you to stop
        it, I literally have to turn the volume down"): the alarm music stops ONCE - later presses, presses on another
        Echo, and presses half an hour on are just the volume. The lights stay red until "back to normal". None = the
        button does its normal job."""
        with _ALERT_LOCK:
            a = getattr(self, "_alert", None)
            if (not a or a.get("music_off") or not a.get("satellite") or a["satellite"] != satellite
                    or time.time() - float(a.get("t") or 0) > float(self.cfg.get("intruder.button_s", _ALERT_BUTTON_S)
                                                                    or _ALERT_BUTTON_S)):
                return None
            self._alert = dict(a, music_off=True)
        try:
            self._music_fast("stop", a.get("room") or room, satellite, said_music=True)
        except Exception:
            log.exception("alert button: music stop failed")
        return {"ok": True, "silent": True, "say": "Alarm music's off (volume button). Lights stay red until back to normal."}

    def _recent_context(self, room: str, current: str = "") -> str:
        """The last few lines said in this room in the past 5 minutes, oldest first (not the words being handled)."""
        now = time.time()
        rows = [x for x in list(getattr(self.memory, "turns", []) or [])
                if isinstance(x, dict) and (x.get("room") or "") == (room or "") and now - float(x.get("ts") or 0) < 300]
        if rows and rows[-1].get("role") == "user" and str(rows[-1].get("text", "")).strip() == (current or "").strip():
            rows = rows[:-1]
        return "\n".join(("User: " if r.get("role") == "user" else "You: ") + str(r.get("text", ""))[:240]
                         for r in rows[-4:])

    def _roast_fast(self, t: str, room: str, satellite: str = "", loose: bool = False) -> dict | None:
        """A roast of someone they NAME - another person, themselves, or her - and "longer" / "another one" /
        "roast her more" right after one. None = not a roast request. loose = a follow-up (no wake word: it hears the
        room) - then only a continuation of her own roast counts."""
        s = _ROAST_TAIL.sub("", " ".join(re.sub(r"[.!?,]+", " ", (t or "").replace(chr(0x2019), "'")).split()))
        s = _ROAST_LEAD.sub("", s).strip()
        if not s:
            return None
        key = satellite or room
        roasts = getattr(self, "_roasts", None)
        if roasts is None:
            roasts = self._roasts = {}
        last = roasts.get(key)
        just_now = (last is not None and time.time() - last["t"] < 180 and
                    self._last_reply(room).strip()[:200] == last.get("said", "")[:200])
        crude = angle = ""
        long_, again = False, False
        m = _RX_ROAST.fullmatch(s)
        if m:
            who, long_ = m.group("who").strip().lower(), bool(m.group("more") and _ROAST_LONG.search(m.group("more")))
            who = _ROAST_WHO_TAIL.sub("", who).strip() or who
        elif just_now and (_RX_ROAST_AGAIN.fullmatch(s) or (re.fullmatch(r"that'?s it", s, re.I) and
                                                           (t or "").rstrip().endswith("?"))):
            who, long_, again = "again", bool(_ROAST_LONG.search(s)), True
        else:
            m = _RX_ROAST_WHY.fullmatch(s)
            if not m or loose:
                return None
            who, what = m.group("who").strip().lower(), m.group("what").lower()
            if who in _ROAST_YOU or who in _ROAST_ME:
                return None                       # "why is Alexa so dumb?" = an insult at her (the comeback path)
            if not re.fullmatch(_ROAST_WHY_KIN, who, re.I) and who not in _ROAST_PRONOUN:
                first = who.split()[0]
                mm = re.search(r"\b" + re.escape(first) + r"\b", t or "", re.I)
                if (not re.fullmatch(_ROAST_WHY_PERSON, what, re.I) or first in _ROAST_WHY_NOT_NAMES or mm is None
                        or not mm.group(0)[:1].isupper()):
                    return None                   # "why is my phone so slow?" / "why is Netflix so slow?" - questions
            if what in _ROAST_WHY_SEXUAL:
                crude = (t or "").strip()
            else:
                angle = (t or "").strip()
        if who == "again" or who in _ROAST_PRONOUN:
            if not (last is not None and time.time() - last["t"] < 600):
                if who == "again" or crude or angle or loose:
                    return None
                return {"ok": True, "say": "Roast who? Give me a name."}
            kind, name = last["kind"], last["who"]
        elif who in _ROAST_YOU:
            kind, name = "self", "yourself"
        elif who in _ROAST_ME:
            kind, name = "user", "the person talking"
        else:
            if _ROAST_NOT_PEOPLE.search(who) or len(who.split()) > 3:
                return None
            kind = "other"
            name = re.sub(r"^my\b", "your", who)
            name = " ".join(w if w in ("your",) else w[:1].upper() + w[1:] for w in name.split())
        if loose and not (just_now and (again or who in _ROAST_PRONOUN or name == last.get("who"))):
            return None                           # a follow-up hears the room: only more of her own roast counts
        rnd = (last["n"] + 1) if (last is not None and last["kind"] == kind and last["who"] == name
                                  and time.time() - last["t"] < 600) else 1
        sys_p = _roast_system(kind, name, long_, rnd, crude, angle)
        sys_p = self.persona_system() + (sys_p if self._cussing() else _clean(sys_p))
        try:
            out = self.brain.chat(sys_p, t, history=self.memory.recent(room)).strip()
        except BrainError:
            return {"ok": False, "say": f"My brain's not answering - {name} got lucky this time." if kind == "other"
                    else "My brain's not answering right now - give me a sec."}
        out = re.sub(r"^(?:(?:oh|wow|dude|whoa)\b[,!.?]*\s+)+(?:yeah\b[,!.?]*\s+)?(?:well\b[,!.?]*\s+)?", "", out,
                     flags=re.I).strip().strip('"').strip()
        out = (out[:1].upper() + out[1:])[:900] if out else ""
        if not out:
            return {"ok": True, "say": "I've got nothing. That's a roast in itself."}
        roasts[key] = {"who": name, "kind": kind, "t": time.time(), "n": rnd, "said": out[:300]}
        return {"ok": True, "say": out}

    def _comeback(self, t: str, room: str) -> dict:
        """The user called her names: a line from her comeback bank for that kind of insult (comebacks.py, ~1000) -
        or, half the time, a fresh roast her model writes (persona + _ROAST, on the harsh side)."""
        import random

        from . import comebacks
        sweary = self._cussing()
        if not sweary or random.random() < 0.5:
            try:
                roast = _ROAST if sweary else _clean(_ROAST)
                out = self.brain.chat(self.persona_system() + roast, t, history=self.memory.recent(room)).strip()
                # (the model still opens "Oh, yeah?" / "Wow," / "Dude," now and then - a roast lands harder without it)
                out = re.sub(r"^(?:(?:oh|wow|dude|whoa)\b[,!.?]*\s+)+(?:yeah\b[,!.?]*\s+)?(?:well\b[,!.?]*\s+)?", "",
                             out, flags=re.I).strip()       # ("Oh, look who's talking" keeps its "look")
                out = out[:1].upper() + out[1:]
                if out:
                    return {"ok": True, "say": out[:500]}
            except BrainError:
                pass
        return {"ok": True, "say": comebacks.pick(t) if sweary else random.choice(_CLEAN_COMEBACKS)}

    def _cussing(self) -> bool:
        return bool(self.cfg.get("personality.cussing", False))

    # ---- the brain for everything else ----
    def _think(self, t: str, room: str, satellite: str = "", unsure: dict | None = None,
               heard: str | None = None, last_said: str = "") -> dict:
        """unsure = the music's own answer to a "play X" it found nothing for ("play twenty questions" may be a game):
        what she says when the brain can't be asked. heard = see handle(): a voice turn may come back "ignored"."""
        sys = _INTENT_SYS.format(name=self.cfg.get("name", "Ava"),
                                 rooms=", ".join(self.lights.rooms()) or "none",
                                 plugs=", ".join(self.plugs.devices) or "none")
        # "Turn on most messages." (10-19 = "turn on both bedroom lights", misheard): after her wake word a "turn
        # on / off ..." is never judged not-for-her - that ended in SILENCE, which reads as her ignoring him; the
        # model answers it (and asks again when it's garbled)
        cmd_shaped = heard == "wake" and re.match(
            r"^(?:(?:alexa|ava|hey ava|ok|okay|um|uh|so|please|can you|could you|would you)[,\s]+)*"
            r"(?:turn|switch|flip|shut|put)\s+(?:on|off|out)\b", t.strip(), re.I)
        # (after her wake word a "play X" she just tried and couldn't find is for her; on a follow-up it may be chatter)
        may_ignore = heard == "follow_up" or (heard == "wake" and unsure is None and not _RX_AT_HER.search(t)
                                              and not cmd_shaped)
        if may_ignore:
            sys = sys.replace("|answer>", "|answer|ignore>", 1)
            sys += _IGNORE_FOLLOW_UP if heard == "follow_up" else _IGNORE_WAKE
        if cmd_shaped:
            sys += ("\n- This was said to you as a command but the light controls couldn't read it - it may be misheard. "
                    "If it sounds like a garbled light command, use 'answer' and ask them to say it again, naming what "
                    "you heard. Never claim you switched anything.")
        if heard != "follow_up" and (_RX_HOOKER.search(t) or _RX_AT_HER.search(t)):
            return self._comeback(t, room)          # name-calling: a comeback, never the intent step's polite line
        recent = self._recent_context(room, t)
        t = _fix_heard(t, recent)
        ctx = f"Room: {room or 'unknown'}. "
        if recent:
            ctx += f"Recent conversation in this room (oldest first):\n{recent}\n"
        if heard == "follow_up" and last_said:
            ctx += f'You had just said: "{last_said}". '
        ctx += f"User said: {t}"
        try:
            intent = self.brain.chat_json(sys, ctx)
        except BrainError:
            return unsure or {"ok": False, "say": "My brain's not answering right now — give me a sec."}
        action = str(intent.get("action", "answer")).lower()
        if action in ("remember", "forget") and heard and (heard == "follow_up" or not _RX_ASKED_TO_KEEP.search(t.lower())):
            # the model files plain statements away ("we need milk" -> "Got it, I'll remember that."): from a room mic
            # that's overheard talk being saved. Only an explicit ask, after the wake word, is a memory request.
            action = "ignore" if may_ignore else "answer"
            intent["say"] = ""                         # (its "I'll remember that" would be a lie now)
        if action == "ignore":
            if may_ignore:
                return {"ok": True, "ignored": True, "say": ""}
            action = "answer"                         # (not offered: a typed message always gets an answer)
        if action == "insult" and heard != "follow_up":
            return self._comeback(t, room)          # (an insult the word list missed - the model spotted it)
        say = str(intent.get("say", "")).strip()
        target = str(intent.get("target", "")).strip()
        if action in ("light", "brightness", "color"):
            lcs = self._light_cmds(t, room)                                 # her own reading of the words beats the model's
            if lcs:                                                   # (it filled in "kitchen" for "shut off the lights")
                return self._run_lights(lcs, room)
        if action == "light":                                         # the REAL result is spoken, never the model's
            return self.lights.set_state(target, _truthy(intent.get("on", True)), room)    # claim ("now on")
        if action == "brightness":
            return self.lights.set_brightness(target, _pct(intent.get("percent", 50)), room)
        if action == "color":
            return self.lights.set_color(target, str(intent.get("color", "")), room)
        if action == "plug":
            # (only a plug it HAS: the model called "turn on bedroom mic one" a plug named "bedroom")
            names = {n.lower() for n in self.plugs.devices}
            if (target or "porch").lower() in names:
                return self.plugs.set_state(target or "porch", bool(intent.get("on", True)))
            lcs = self._light_cmds(t, room)
            if lcs:
                return self._run_lights(lcs, room)
            return {"ok": False, "say": f"I didn't catch which light. Say it like: turn on the bedroom lights."}
        if action == "music":                        # a "play ..." the fast path didn't catch
            q = str(intent.get("query") or t).strip().lower().rstrip(".!?")
            command = _m_command(q)                  # "pause" / "stop": the speaker's, never a title ("Pause")
            if target and target.lower() != room.lower() and not re.search(r"\s(?:in|on) (?:the )?\S", q):
                q += f" in the {target.lower()}"
            r = self._music_fast(q, room, satellite, said_music=True)
            if r is None and not command and not _M_PLAY.fullmatch(q):
                r = self._music_fast("play " + q, room, satellite, said_music=True)
            return r or {"ok": False, "say": say or "I'm not sure what you want me to play."}
        if action == "remember":
            return self.memory.remember(str(intent.get("query") or t))
        if action == "forget":
            return self.memory.forget(str(intent.get("query") or t))
        if action == "weather":
            return web.weather(self.cfg, where=target, when=web.forecast_when(t))
        if action == "search":
            extra = {}
            try:                                       # (the conversation, for a follow-up - when answer() takes it)
                params = inspect.signature(web.answer).parameters
                extra = {k: v for k, v in (("context", recent), ("said", t)) if k in params}
            except (TypeError, ValueError):
                pass
            return web.answer(str(intent.get("query") or t), self.cfg, self.brain, self.persona_system(), **extra)
        # answer / chit-chat — reply in character, with the recent conversation for context
        if say and re.search(r"\b(?:lights?|lamps?|bulbs?)\b", t, re.I) and re.search(
                r"\b(?:lights?|lamps?|bulbs?)\b[^.?!]{0,40}\b(?:now |are |is |all )?(?:on|off|out)\b|"
                r"\b(?:turn(?:ed|ing)|switch(?:ed|ing)|shut(?:ting)?|kill(?:ed|ing))\b[^.?!]{0,30}\b(?:on|off|out)\b|"
                r"\bbrightening\b|\bdarkness\b", say, re.I):
            # 10-16 01:32: "all lights." -> the model ANSWERED "All lights are now on" and touched nothing. A light
            # sentence she couldn't read must never be answered with a made-up result.
            return {"ok": False, "say": "I didn't catch what to do with the lights. Say it like: turn off all the lights."}
        from .persona import unsmug                      # (a real answer, not a roast: no smug tag on the end)
        if say:
            return {"ok": True, "say": unsmug(say)}
        try:
            out = self.brain.chat(self.persona_system(), t, history=self.memory.recent(room))
            return {"ok": True, "say": unsmug(out[:500]) or "Hm, I've got nothing."}
        except BrainError:
            return {"ok": False, "say": "My brain's offline at the moment."}

    # ---- music: the NAS library on the Echo that heard it (houseava/music.py) ----
    def _music_fast(self, low: str, room: str, satellite: str, said_music: bool = False) -> dict | None:
        """A music request -> its reply; None = not music (the other fast paths and the brain get it, unchanged).
        said_music = the brain already called it music, so a bare "pause" / "stop" is the speaker's even with nothing
        on (she says so) — never a song search."""
        t = _m_clean(low)
        if not t:
            return None
        key = satellite or room
        pend = self._pending.pop(key, None)              # "delete my road trip playlist?" ... "yes"
        if pend is not None and time.time() < pend[0]:
            if _M_YES.fullmatch(t):
                return pend[1]()
            if _M_NO.fullmatch(t):
                return {"ok": True, "say": "Okay, I'll leave it."}
        text, where = t, ""
        m = _M_WHERE.fullmatch(t)
        if m:
            w = m.group("where").strip()
            if w in ("here", "this room"):
                text = m.group("body").strip()
            elif m.group("spk") or self._room_word(w):
                text, where = m.group("body").strip(), w
        try:
            kind = self._music_kind(text)
            if kind is None and where:                   # (the whole line, then: "remember the keys are in the kitchen")
                text, where = t, ""
                kind = self._music_kind(text)
        except Exception:                                # a broken music grammar never takes the router down
            log.exception("music: reading %r failed", text)
            return None
        if kind is None:
            return None
        op, arg, place = kind
        vague = not said_music and op in ("pause", "stop", "next", "louder", "quieter") and bool(_M_VAGUE.fullmatch(text))
        if self.music is None:
            if vague or op == "weak":
                return None
            return {"ok": False, "say": "I can't do music here — it runs on the Echo hub, and its music library isn't "
                                        "set up."}
        sp, err = self.music.target(satellite, room, place or where)
        if sp is None:
            if vague or op == "weak" or (op == "intent" and self._music_cmd(text, False) is None):
                return None
            if op != "intent" or (self._music_cmd(text, True) or {}).get("op") not in _M_NO_SPEAKER:
                return {"ok": False, "say": err}
            sp = self.music.players.get(satellite)       # ("what albums do i have by logic": no speaker needed)
        elif vague and not sp.has_music() and not (getattr(sp, "device", "") == "playing" and op != "next"):
            return None                                  # (something else playing - a phone over Bluetooth: "stop" /
                                                         # "pause" / "louder" are for it; "next" can't reach a phone)
        try:
            res = self._music_do(op, arg, sp, text, key)
        except Exception:
            log.exception("music: %r failed", text)
            return {"ok": False, "say": "Sorry, the music side hiccupped on that one."}
        if res is not None and res.get("ok") and satellite and sp is not None and sp.key != satellite and \
                str(res.get("say", "")).startswith("Playing"):
            res["say"] = res["say"].rstrip(".") + f" — on {sp.name}."
        return res

    def _room_word(self, w: str) -> bool:
        """'bedroom' / 'living' (room) / a satellite's name: a place, so '... in the bedroom' says where."""
        w = re.sub(r"^(?:the|my)\s+", "", w)
        rooms = {str(r).lower() for r in (self.lights.rooms() or {})} | _M_ROOMWORDS
        return bool(w in rooms or w + " room" in rooms or (self.music is not None and self.music.speaker_place(w)))

    def _music_kind(self, text: str):
        """-> (op, arg, room named in the phrase) or None. Transport phrases, the volume, then (with the library)
        musiclib's own grammar ("intent") or a plain "play X" ("play") / "turn on X" ("weak", a sure match only)."""
        m = _M_VOLUME.fullmatch(text)
        if m:
            v = _m_number(m.group("n"))
            if v is not None:
                pct = v / 100 if (m.group("pct") or v > 10) else v / 10      # "volume 7" = 7 out of 10
                place = (m.group("room") or m.group("room2") or "").strip()
                return "volume", max(0.0, min(1.0, pct)), place if place and self._room_word(place) else ""
        for op, rx in _M_TRANSPORT:
            if rx.fullmatch(text):
                return op, None, ""
        if self.music is None:
            return ("play", None, "") if _M_PLAY.fullmatch(text) else None
        if self._music_cmd(text, True) is not None:      # (as if music were on: the speaker's own session decides)
            return "intent", None, ""
        m = _M_PLAY.fullmatch(text)
        if m:
            return "play", m.group("x").strip(), ""
        m = _M_WEAK.fullmatch(text)
        if m and not self._devicey(m.group("x")):
            return "weak", m.group("x").strip(), ""
        return None

    @staticmethod
    def _music_cmd(text: str, live: bool) -> dict | None:
        """musiclib's own grammar (playlists, "skip this artist", "what albums do i have by logic" ...)."""
        from .music import intent

        return intent.parse(text, live)

    def _devicey(self, x: str) -> bool:
        """'turn on the porch' / 'the bedroom lights' / 'everything' / 'the news': a device (or not music), never a song."""
        from .music import intent

        x = re.sub(r"^(?:the|my|our)\s+", "", x.lower())
        if _M_DEVICE.search(x) or _M_NOT_MUSIC.search(x) or intent._NOT_PLAYABLE.search(x) or self._room_word(x):
            return True
        names = set(self.plugs.devices) | {str(r).lower() for r in (self.lights.rooms() or {})} | \
            {str(b).lower() for b in (self.cfg.get("lights.bulbs", {}) or {})}
        return any(n and re.search(rf"\b{re.escape(n)}\b", x) for n in names)

    def _music_do(self, op: str, arg, sp, text: str, key: str) -> dict | None:
        from .music import intent, ops, player

        if op not in ("intent", "play", "weak", "music"):
            return self.music.transport(sp, op, arg)
        with player.use(sp):                             # every musiclib call below plays on (and talks about) sp
            if op == "music":                            # "play some music": what's paused, else a mix of everything
                if sp.paused or sp.stopped:
                    return self.music.transport(sp, "resume")
                if sp.has_music():
                    return {"ok": True, "say": "It's already playing."}
                if not self.music.library_ok():
                    return self.music.unreachable()
                return ops.play_text(self.music.lib_cfg, f"{ops.DEFAULT_N} songs", self.brain, shuffle=True)
            if op == "intent":
                cmd = intent.parse(text, ops.session_live())     # (with THIS speaker's session)
                if cmd is None:
                    return None
                if cmd["op"] not in ("list", "show", "rename", "delete", "keep_paused", "love", "ban", "save") and \
                        not self.music.library_ok():
                    return self.music.unreachable()
                return self._music_op(cmd, sp, key)
            if not self.music.library_ok():
                return None if op == "weak" else self.music.unreachable()
            res = self._play_any(arg, sp, sure=op == "weak")
            if op == "play" and res is not None and not res.get("ok") and intent._NOT_PLAYABLE.search(arg):
                return None                              # "play a game": not a song — the brain can have it
            return res

    def _music_op(self, cmd: dict, sp, key: str) -> dict | None:
        """musiclib's ops, mapped exactly as desktop Ava's router does (ava/brain/router.py _music_step), on `sp`."""
        from .music import ops, playlists

        cfg, brain = self.music.lib_cfg, self.brain
        op = cmd["op"]
        name = cmd.get("name", "")
        if op == "play":
            return self._play_music(cmd.get("text", ""), cmd, sp)
        if op == "create":
            return ops.create(cfg, name, cmd.get("text", ""), brain)
        if op == "delete":
            if not playlists.find(name, cutoff=0.9):     # nothing to delete: no question to ask
                return ops.delete(cfg, name)
            # it only removes the LIST (moved to Playlists/Deleted) — and only after a spoken yes, always
            self._pending[key] = (time.time() + 90, lambda: ops.delete(cfg, name))
            return {"ok": True, "say": ops.delete_confirm_text(name)}
        if op == "keep_paused":
            if sp.has_music() and not sp.paused:
                sp.pause()
                return {"ok": True, "say": "Paused — I'll leave it that way."}
            return {"ok": True, "say": "Okay, I'll leave it paused."}
        if op == "more_like":
            return ops.more_like(cfg, brain)
        if op == "skip_artist":
            return ops.skip_artist(cfg)
        if op == "ban":
            return ops.ban(cfg)
        runs = {
            "save": lambda: ops.save_current(cfg, name),
            "add": lambda: ops.add(cfg, name, cmd.get("what", "song"), cmd.get("text", ""), brain),
            "remove": lambda: ops.remove(cfg, name, cmd.get("what", "song"), cmd.get("text", ""), brain),
            "show": lambda: ops.show(cfg, name),
            "list": lambda: ops.list_all(cfg),
            "rename": lambda: ops.rename(cfg, cmd.get("old", ""), cmd.get("new", "")),
            "love": lambda: ops.love(cfg),
            "albums_by": lambda: ops.albums_by(cfg, cmd.get("artist", "")),
            "count_by": lambda: ops.count_by(cfg, cmd.get("artist", "")),
            "status": lambda: ops.status(cfg),
            "reindex": lambda: ops.reindex(cfg),
        }
        run = runs.get(op)
        return run() if run is not None else None

    def _play_music(self, text: str, cmd: dict, sp) -> dict:
        """A playlist for what was said; when the library has nothing by those names, the plain song search gets a
        go — and a genre / era / mood said with an unknown name still plays (desktop's _play_music)."""
        from .music import ops, spec

        cfg, brain = self.music.lib_cfg, self.brain
        r = ops.play_text(cfg, text, brain, shuffle=cmd.get("shuffle"), playlist=bool(cmd.get("playlist")))
        if r.get("ok") or not r.get("fallback"):
            return r
        if not cmd.get("playlist"):
            alt = self._play_any(text, sp, library=False)
            if alt is not None and alt.get("ok"):
                return alt
        sp_ = spec.parse(text)
        if sp_.names and (sp_.genres or sp_.years or sp_.moods):
            r2 = ops.play_text(cfg, text, brain, shuffle=cmd.get("shuffle"), drop_names=True)
            if r2.get("ok"):
                r2["say"] = f"I don't know {sp_.names_whole or sp_.names[0]}, so here's the rest of that: " + r2["say"]
                return r2
        if not str(r.get("say", "")).rstrip().endswith((".", "?", "!")):     # (musiclib's bare name: "paper scissors")
            r = dict(r, say=f"I couldn't find {text} in your music.")
        return r

    def _play_any(self, q: str, sp, library: bool = True, sure: bool = False) -> dict | None:
        """'play X': an artist (or several, or a saved playlist) of that name — all their songs, shuffled — else the
        best-matching song, else an album of that name (desktop's _play_any, minus movies and apps). sure = only a
        confident match ("turn on purple rain": an artist, an album, or a song's whole title); None = nothing that
        sure. Nothing at all -> "ask_brain": the fast path hands the line to the brain ("play twenty questions")."""
        from .music import library as songs
        from .music import ops

        cfg, brain = self.music.lib_cfg, self.brain
        q = re.sub(r"^(?:some|a little|a bit of)\s+", "", q).strip()
        if library:
            r = ops.play_name(cfg, q, brain)
            if r is not None and (r.get("ok") or not r.get("fallback")):
                return r
        ranked, how = songs.find_song_scored(cfg, q, 8)
        if ranked and (not sure or (ranked[0][0] >= 0.8 and self._said_title(q, ranked[0][1]))):
            return self.music.play_song(sp, ranked[0][1])
        if library:
            m = re.match(r"^(.*?)\s+by\s+(.+)$", q)
            r = ops.play_album_named(cfg, m.group(1), artist=m.group(2)) if m else ops.play_album_named(cfg, q)
            if r is not None and r.get("ok"):
                return r
        if sure:
            return None
        if how == "indexing":
            return {"ok": False, "ask_brain": True, "say": "I'm still learning your music library. Say the artist too, "
                                                           "like 'play Billie Jean by Michael Jackson'."}
        return {"ok": False, "ask_brain": True, "say": f"I couldn't find {q} in your music."}

    def _said_title(self, q: str, path: str) -> bool:
        """'purple rain' / 'stan by eminem': that song's whole title was said — two words or more, or with its artist.
        One word that happens to be a title ('the news', 'sprinklers') isn't sure enough for "turn on"."""
        title, who = self.music.describe(path)
        stem = re.sub(r"^\d+[\s.\-_]+", "", path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0])
        said = _m_words(q)
        for name in (title, stem.rpartition(" - ")[2] or stem):      # (the tags, else the file name before they're read)
            tw = _m_words(name)
            if (len(tw) >= 2 and said == tw) or (who and said == _m_words(f"{name} by {who}")):
                return True
        return False

    # ---- timers (harmless extra) ----
    def _set_timer(self, n: int, unit: str, room: str) -> dict:
        secs = n * {"second": 1, "minute": 60, "hour": 3600}[unit]

        def fire():
            msg = f"That {n} {unit}{'s' if n != 1 else ''} is up."
            if self.announce:
                try:
                    self.announce(msg, room)
                except Exception:
                    pass
            else:
                print(f"\n[timer] {msg}")

        tmr = threading.Timer(secs, fire)
        tmr.daemon = True
        tmr.start()
        self._timers.append(tmr)
        return {"ok": True, "say": f"Okay, {n} {unit}{'s' if n != 1 else ''}. I'll shout when it's up."}
