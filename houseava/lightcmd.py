"""Every way people say a light command -> structured commands (the goal: "light commands must work 100%",
"add every variation you can think of").

v3 (after an adversarial review of ~2,000 sentences): a sentence is cut into CLAUSES (". ? ! ;" always, commas and
and/but/then before a new command), each clause is read on its own, and a clause only switches lights when it is
SAID AS A COMMAND - an instruction ("turn off the lights", "kill the bedroom", "dim it in here"), the light-first
form ("bedroom lights off", "lights 50 percent"), the clipped form ("off all lights"), or "I want / I need / make
sure / can I have ... the lights on". Statements and stories ("the lights went out", "Dad turned off the lights",
"come on, let's go to the bedroom", "it's half past ten") never switch anything. Without a light word the clause
may hold NOTHING but command words, the target and filler ("turn on living room" yes, "turn on Hulu in the bedroom"
no). "it/them" means the lights from the clause before, or from a light command said in the last 3 minutes
(Lights.parse_all fills that in).

parse_all(text, rooms, bulbs, plugs, aliases) -> list[LightCmd] (one per clause that is a light command); [] when
there's none. parse_detail() also returns the clauses that weren't light commands (the porch plug gets those).

Targets:  "all"/"every"/"everything"/"whole house"/"everywhere" -> every bulb in the house, from any room
          a room or its nickname ("front room", "lounge", "dining") -> that room (several: "living and dining room")
          "bedroom light one" / "bedroom 2" (a bulb's own name) / "the first bedroom light" -> those bulbs;
          "bedroom light one and dining room light two" -> each room's own number
          "one of the bedroom lights" -> one bulb;  "all except the bedroom / bedroom light one / here",
          "the other rooms" -> the rest;  "... but not the dining room" -> that room taken out
          nothing named ("the lights", "lights out", "in here") -> HERE: the Echo's room, or every bulb when that room
          has none (the kitchen) or there is no room (the app's chat)
Actions:  on / off / toggle (no on/off heard: flips them) / set (%) / dim / brighten (25%, or "by 20 percent") /
          color / status ("are the lights on?") / flash ("flash bedroom light one")
          + when: "in 10 minutes" / "in five" / "after an hour" / "10 minutes from now" / "at 10:30 pm" / "at 7" /
          "at midnight" (later), "for 20 minutes" (now, then back the way they were)
"""

from __future__ import annotations

import copy
import math
import difflib
import re
import time as _time
from dataclasses import dataclass, field

LIGHT_NOUN = r"(?:lights?|lamps?|bulbs?|lighting)"
_NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
              "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
              "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "1st": 1, "2nd": 2, "3rd": 3,
             "4th": 4, "5th": 5, "6th": 6}
DEFAULT_ALIASES = {
    "living room": ["living room", "livingroom", "living", "lounge", "front room", "family room", "sitting room",
                    "den", "tv room", "living rooms"],
    "dining room": ["dining room", "diningroom", "dining", "dinning room", "dinning", "dining area", "dinner room",
                    "dining rooms",
                    # whisper on the Echo mics keeps hearing "dining" as "denim" (seen over and over in real use: "she keeps
                    # hearing denim instead of dining") - the house has no other "denim" anything
                    "denim room", "denim", "denimum", "denum room", "denham room", "dying room", "dyeing room"],
    "bedroom": ["bedroom", "bed room", "master bedroom", "master", "main bedroom", "our bedroom", "my bedroom",
                "bedrooms", "my room", "our room", "my own room", "mom and dad room", "mommy and daddy room",
                "parents room"],
    "bathroom": ["bathroom", "bath room", "restroom", "washroom"],
    "kitchen": ["kitchen"],
}
# whisper's real mishearings of room names (only used right before a light word: "victim light", "dead light")
MISHEARD = {"victim": "bedroom", "bit the": "bedroom", "better": "bedroom", "bed": "bedroom", "bedrum": "bedroom",
            "bad room": "bedroom", "bedroomed": "bedroom", "shining room": "dining room", "dead": "dining room",
            "dyeing room": "dining room", "dying room": "dining room", "diner": "dining room", "dinah": "dining room",
            "denim": "dining room", "denim room": "dining room", "denimum": "dining room",   # (10-17 "Turn off denim light")
            "daniel": "dining room", "danielle": "dining room",               # (10-18 "Turn on Daniel Light, please.")
            "lining room": "living room", "leaving room": "living room", "liven room": "living room",
            "livin room": "living room", "living rum": "living room", "giving room": "living room"}
AMBIGUOUS = {"ring room": ["living room", "dining room"], "ing room": ["living room", "dining room"],
             "in room": ["living room", "dining room"]}
COLORS = {
    "red": "red", "green": "green", "blue": "blue", "white": "white", "warm white": "warm white", "warm": "warm white",
    "soft white": "warm white", "yellow": "yellow", "orange": "orange", "purple": "purple", "violet": "purple",
    "pink": "pink", "cyan": "cyan", "teal": "teal", "cool white": "cool white", "daylight": "cool white",
    "cold white": "cool white", "bright white": "cool white", "magenta": "pink", "lime": "green", "gold": "yellow",
    "amber": "orange", "light blue": "cyan", "sky blue": "cyan", "aqua": "cyan", "turquoise": "teal", "lavender": "purple",
    "warmer": "warm white", "cooler": "cool white", "colder": "cool white", "whiter": "cool white",
}
# things that are NOT lights (singular and plural): a clause about one of these never switches a bulb
_NOT_LIGHTS = (r"music|songs?|tunes?|radio|playlists?|albums?|podcasts?|audiobooks?|tvs?|televisions?|telly|shows?|"
               r"movies?|netflix|hulu|espn|youtube|disney|hbo|spotify|pandora|volume|sound|audio|speakers?|alarms?|"
               r"timers?|reminders?|mics?|microphones?|mike|bluetooth|phones?|screens?|displays?|face|cameras?|fans?|"
               r"heat|heater|heaters|heating|ac|a c|air|aircon|air conditioning|thermostat|temperature|degrees|oven|"
               r"ovens|stove|stoves|burners?|grill|grills|water|faucet|computers?|pc|laptops?|game|games|xbox|"
               r"playstation|intercom|security|siren|router|wifi|wi fi|internet|vacuum|dishwasher|washer|dryer|car|"
               r"cars|garage door|doors?|locks?|news|weather|yourself|noise|echo|echos|echoes|notifications?|"
               r"do not disturb|sleep mode|night mode|humidifier|dehumidifier|purifier|blinds|curtains|shades|"
               r"sprinklers?|kettle|coffee|toaster|microwave|jazz|cotton|battery|charger|grandma|grandpa|"
               r"traffic light|light beer|lighter|lightning|daylight savings")
_FILLER_LEAD = (r"(?:alexa|hey ava|hey alexa|ava|ok ava|okay ava|hey|hi|hello|ok|okay|yo|oh|um+|uh+|uhm|er|hmm|so|and|"
                r"then|well|now|please|pls|plz|just|quickly|quick|real quick|go ahead and|go ahead|alright|all right|"
                r"right|cool(?! (?:(?:the|my|our|this|that) (?!(?:\w+ ){0,3}(?:lights?|lamps?|bulbs?)\b)|it\b|down\b|off\b|"
                r"everything\b|white\b))|great|thanks|thank you|good|nice|sweet|wow|man|dude|hey there|hey girl|ava please|"
                r"can you|could you|would you|will you|can u|could u|would u|can you please|could you please|"
                r"would you please|will you please|would you mind|do you mind|i want you to|i need you to|"
                r"i'd like you to|i would like you to|id like you to|you can|you could|kindly|maybe|if you can|"
                r"if you could|for me|need you to|be a dear and|do me a favor and|do me a favour and|do me a favor|"
                r"do me a favour|give a|"
                r"are you able to|are you able|is it possible to|is it possible for you to|can you go ahead and|"
                r"why dont you|why don't you|how about you|if you dont mind|if you don't mind|"
                r"i(?= (?:turn|switch|shut|flip|dim|set|make)\b))")
_FILLER_TAIL = (r"(?:please|pls|plz|for me|for us|thanks|thank you|thank u|thx|ty|now|right now|asap|real quick|"
                r"quickly|ava|alexa|okay|ok|babe|baby|girl|hun|honey|already|again|as well|would you|will you|"
                r"could you|can you|if you can|if you could|if you don't mind|if you would|when you can|man|dude|lol|"
                r"you know|whatnot|or whatever|and stuff|yall|y all|real fast|real quick|too|also|now please|"
                r"would ya|will ya|for a sec|okay thanks|thanks ava)")
_CMD_VERB = (r"(?:turn|switch|flip|power|put|click|knock|shut|kill|cut|douse|dim|brighten|set|make|change|paint|"
             r"toggle|hit|get|nix|pop|chuck|whack|stick|kick|mash|fire|crank|bring|lower|raise|open|close|"
             r"extinguish|activate|deactivate|enable|disable|illuminate|light up|black|blackout|leave|keep|flash|blink)")
# clause openers that make an instruction out of what follows ("I want the lights off", "make sure the lights are off")
_WANT = (r"(?:i want|i need|id like|i would like|i wanna|we want|we need|make sure|ensure|can i have|could i have|"
         r"can i get|could i get|can we have|could we have|can we get|let me have|lets have|let us have|give me|"
         r"get me|i want to have|we would like|i would love|id love)")


@dataclass
class Target:
    kind: str = "here"                          # here | all | rooms | bulbs | except | ref
    rooms: list = field(default_factory=list)   # canonical room names (except: the rooms left out)
    bulbs: list = field(default_factory=list)   # bulb names ("bedroom 2") (except: the bulbs left out)
    nums: list = field(default_factory=list)    # bulb numbers without a room yet ("light two" -> [2])
    count: int | None = None                    # "one of the bedroom lights" -> 1
    unknown: str = ""                           # a place she has no lights for ("the kitchen", "the garage")
    ask: list = field(default_factory=list)     # "ring room" -> ["living room", "dining room"]: ask which
    except_here: bool = False                   # "the other rooms" / "everywhere except here" / "... but not in here"
    minus: list = field(default_factory=list)   # "... but not the dining room": rooms (or bulbs) taken out
    with_here: bool = False                     # "the lights in this room and the bedroom": the rooms AND here
    others: bool = False                        # said "the rest / everything else / the other lights": after a
    #                                             command naming rooms it means every light except THOSE
    other_one: bool = False                     # "the other light / lamp / one": the room's OTHER bulb
    after_off: bool = False                     # HERE minus the lights just switched off (not a user's "keep X on")
    here_said: bool = False                     # "in here" / "this room" said out loud (never widened to the house)
    place: str = ""                             # "by the window": a landmark - HERE if this room has lights, else asked
    minus_nums: list = field(default_factory=list)   # "... except light one": numbers, bound to a room later


@dataclass
class LightCmd:
    action: str                                 # on | off | toggle | set | dim | brighten | color | status | flash
    target: Target
    percent: int | None = None
    color: str = ""
    delay: int = 0                              # seconds: "in 10 minutes" / "at 10:30"
    duration: int = 0                           # seconds: "for 20 minutes" -> back afterwards
    text: str = ""                              # the cleaned words (for logs / tests)
    step: int = 0                               # dim/brighten "by 20 percent" (0 = the usual 25)
    was_ref: bool = False                       # "it/them" filled in from a recent light command (music may own it)
    turn_on: bool = False                       # a dim that is really "turn ... ON dim" (all of them on, low)
    inherited: bool = False                     # target carried over from the clause before ("..., not too bright")
    #   ^ an inherited / ref target is never "named" by this clause: dimming it never relights bulbs that are off


def _num(word: str) -> int | None:
    word = (word or "").strip().lower().replace("-", " ")
    if word.isdigit():
        return int(word)
    if word in _NUM_WORDS:
        return _NUM_WORDS[word]
    parts = word.split()
    if len(parts) == 2 and parts[0] in _NUM_WORDS and parts[1] in _NUM_WORDS and _NUM_WORDS[parts[0]] >= 20:
        return _NUM_WORDS[parts[0]] + _NUM_WORDS[parts[1]]
    if word in ("a hundred", "one hundred", "hundred"):
        return 100
    return None


_NUMBER = (r"(?:\d{1,3}|a hundred|one hundred|hundred|(?:twenty|thirty|forty|fourty|fifty|sixty|seventy|eighty|ninety)"
           r"(?:[ -](?:one|two|three|four|five|six|seven|eight|nine))?|" +
           "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + ")")


def normalize(text: str) -> str:
    """One clause -> lower case words (no punctuation, fillers off both ends, whisper's slips mended)."""
    t = (text or "").lower()
    t = t.replace("’", "'").replace("‘", "'").replace("“", " ").replace("”", " ")
    t = t.replace("&", " and ")
    t = re.sub(r"(\d)\s*%", r"\1 percent", t)
    t = re.sub(r"(\d+):(\d\d)", r"\1h\2", t)                       # keep "10:30" through the punctuation strip
    t = re.sub(r"[^\w\s'#-]", " ", t)                             # . , ! ? ; : " ( ) -> spaces
    t = re.sub(r"(\d+)h(\d\d)", r"\1:\2", t)
    t = re.sub(r"(?<=\w)-(?=\w)", " ", t)                          # "turn-off" / "living-room" / "twenty-five"
    t = t.replace("-", " ").replace("#", " number ")
    t = re.sub(r"\b(lights?|lamps?|bulbs?)'s?\b", r"\1", t)        # "light's" / "lights'" -> lights
    t = re.sub(r"\b(it|that|what|there|here|let|he|she|who|where|how)'s\b", r"\1s", t)   # "it's" -> its (not "it")
    t = re.sub(r"'s\b", "", t)                                     # "bedroom's lights" -> "bedroom lights"
    t = t.replace("'", "")
    t = re.sub(r"\b(?:lite|lyte)\b", "light", t)
    t = re.sub(r"\b(?:lites|lytes)\b", "lights", t)
    t = re.sub(r"\blight bulbs?\b|\blightbulbs?\b", "bulbs", t)
    t = re.sub(r"\bdelights?\b", "the lights", t)                  # "Turn off delights." = the lights
    t = re.sub(r"\b(?:da|de|dem|them|dose|those) (lights?|lamps?)\b", r"the \1", t)   # "turn off da lights"
    t = re.sub(r"\bold (lights?)\b", r"all \1", t)                 # "Turn off old lights." = all lights
    t = re.sub(r"\b(?:mom and dad|mommy and daddy|mum and dad)s? room\b", "parents room", t)   # (its "and" split it)
    # whisper on the Echo mics drops "room" for a short word: "We're not dining with lights." (10-17, = "turn off
    # dining room lights") / "We're not living on light." (10-15) - only right before a light word
    t = re.sub(r"\b(living|dining)\s+(?:with|on|in|and|run)\s+(?=(?:the\s+)?(?:lights?|lamps?|bulbs?)\b)", r"\1 room ", t)
    # "Turn on bedroom mic one." (10-17 = "bedroom light one") - "light" heard as mic / might / like before a number
    t = re.sub(r"\b(bedroom|living room|dining room)\s+(?:mic|might|like|lite|lyte)\s+(?=(?:number\s+)?(?:one|two|1|2)\b)",
               r"\1 light ", t)
    # "turn off dining room two" = the bulb "dining room 2" (it switched both); "one" only without "the" ("the
    # bedroom one" = the one in the bedroom)
    t = re.sub(r"\b(living room|dining room|bedroom) (two|2)\b(?! (?:more|of|minutes?|hours?|seconds?|percent)\b)",
               r"\1 2", t)
    t = re.sub(r"(?<!the )\b(living room|dining room|bedroom) (one)\b(?! (?:more|of|minutes?|hours?|seconds?|percent|"
               r"and|or)\b)", r"\1 1", t)
    t = re.sub(r"\bdeadlights?\b", "dead light", t)               # "Turn on deadlight." = the dining light
    t = re.sub(r"\b(room|bedroom|living|dining|kitchen|bathroom)\s+(?:lies|lice)\b(?!\s+(?:is|was|are))",
               lambda m: m.group(1) + " lights", t)                                            # "the bedroom lies"
    t = re.sub(r"\b(room|bedroom|living|dining|kitchen|bathroom)\s+(?:life|lifes|lives|lie|likes?|lied|line|lime)\b"
               r"(?!\s+(?:is|was))", lambda m: m.group(1) + " light", t)                   # "Living Room Life"
    t = re.sub(r"\bturnoff\b", "turn off", t)
    t = re.sub(r"\bturnon\b", "turn on", t)
    t = re.sub(r"\bswitchoff\b", "switch off", t)
    t = re.sub(r"\b(turn|switch|shut|flip|power|put|cut|click|knock)\s+of\b", r"\1 off", t)    # whisper's "turn of"
    t = re.sub(r"\b(lights?|lamps?|bulbs?)\s+of\b(?!\s+(?:the|mine|my|our|a|your)\b)", r"\1 off", t)
    t = re.sub(r"\blights? ones\b", "light one", t)                                             # "light ones"
    t = re.sub(r"\ball of a sudden\b", "suddenly", t)
    t = re.sub(r"\ball the way (up|down)\b", r"way \1", t)                                      # not "all the lights"
    t = _whisper_slips(t)
    t = re.sub(r"\s+", " ", t).strip()
    if not re.search(r"\band\b", t):            # "bedroom light too." = two; "the living room and the bedroom light
        t = re.sub(r"\b(light|lamp|bulb) too$", r"\1 two", t)   # too" = ALSO the bedroom
        t = re.sub(r"\b(light|lamp|bulb|number) to(?=$|\s+(?:please|now|thanks|on|off)$)", r"\1 two", t)  # "light to"
    for _ in range(10):                         # leading/trailing politeness, wake-word leftovers ("alexa, can you ...")
        n = re.sub(rf"^{_FILLER_LEAD}\b\s*", "", t)
        if not re.search(r"\bfrom now$", n):    # ("10 minutes from now" keeps its "now")
            n = re.sub(rf"\s*\b{_FILLER_TAIL}$", "", n)
        if n == t:
            break
        t = n.strip()
    # the Echo / whisper eats "turn": "We're on better light one." / "and they're off bedroom light two."
    t = re.sub(r"^(?:were|we are|weare|theyre|they are|there|their|youre|you are|your|and theyre|and there|i|eye)\s+"
               r"(on|off)\b", r"turn \1", t)
    # ("You're not dining room light." 10-19 = "turn on dining room light" - it got roasted as an insult)
    t = re.sub(r"^(?:were|weare|theyre|there|their|youre|you are|your)\s+not\b(?=\s+(?:\w+\s+){0,3}" + LIGHT_NOUN + r")",
               "toggle", t)
    t = re.sub(r"^out the\b", "put out the", t)                                                 # clipped "put out"
    return t


_LITE = r"(?:lights?|lamps?|bulbs?)"


def _whisper_slips(t: str) -> str:
    """Whisper's far-field slips on the house's own words, ONLY in a light context (10-19: 600 meant/heard pairs
    written from the real ones - "denim / Daniel light", "bedroom mic one", "Living Room Life", "You're not dining
    room light" - plus the kept Echo turns). Lowercase, no punctuation."""
    if not re.search(r"\b(?:light|lights|lite|like|likes|lice|lies|life|lifes|night|nights|lamp|lamb|bulb|bowl|ball|"
                     r"bites|room|rooms|broom|rum|run|bedrum|dinner|diner|daniel|dinah|percent|dim|dumb|jim|"
                     r"brighter|writer|bright|brider|bryder|brighton|demure|tim|blew|read|lowe|switch|swish|shut|shot|turn|turned|turning|set|said|which|witch|everything|"
                     r"all)\b", t):
        return t
    # rooms
    t = re.sub(r"\b(?:dinner|diner|dinah|daniel)(?:\s+(?:and|room))?\s+(?=" + _LITE + r"\b)", "dining room ", t)
    t = re.sub(r"\b(?:diner|daniel|dinah|dinning) room\b", "dining room", t)
    t = re.sub(r"\b(?:leaving|liven|livin|lining|giving) room\b|\bliving (?:run|rum|rom)\b", "living room", t)
    t = re.sub(r"\bbed room\b|\bbedrum\b|\bbad room\b", "bedroom", t)
    t = re.sub(r"\b(?:this|his) (?:broom|rum|rooms?)\b", "this room", t)
    t = re.sub(r"\b(" + _LITE + r"|it|them)\s+in (?:hair|year|her|hear)\b", r"\1 in here", t)
    t = re.sub(r"\beveryone light", "every light", t)
    if re.search(r"\b(?:lights?|everything|all)\b", t):
        t = re.sub(r"\baccept\b(?=\s+(?:the|for)\b)", "except", t)          # "all the lights, accept the bad room"
    # the light noun itself
    t = re.sub(r"\b(the|all the|both|these|those|all)\s+(?:likes|lice|lies|lifes|lives|nights|bites)\b", r"\1 lights", t)
    t = re.sub(r"\bthe (?:like|life|lice|night|lamb)\b(?=\s*$|\s+(?:on|off|out|in|please|now)\b)", "the light", t)
    t = re.sub(r"\b(bedroom|room|living room|dining room)\s+(night|bite|bowl|ball|lamb|might|mic)(s?)\b",
               lambda m: m.group(1) + " light" + m.group(3), t)
    t = re.sub(r"\b(bedroom|room)\s+white\s+(?=(?:one|two|1|2)\b)", r"\1 light ", t)
    t = re.sub(r"\b(?:like|life|night|ball|bowl|mic|might)s?\s+(?=number\b)", "light ", t)
    t = re.sub(r"^(?:likes|lice|lies|lifes|life s|like s|lifes)\s+(on|off|out)\b", r"lights \1", t)
    t = re.sub(r"^(?:like|life) son\b", "lights on", t)
    t = re.sub(r"\b(?:lights?|likes?|lice|life)\s+soft\b", "lights off", t)
    t = re.sub(r"\b(lights?)\s+(?:zone|sound|son)\b", r"\1 on", t)
    t = re.sub(r"\b(lights?|room)\s+don$", r"\1 on", t)
    t = re.sub(r"\boften here\b", "off in here", t)
    t = re.sub(r"\b(lights?)\s+often\b", r"\1 off", t)
    t = re.sub(r"\b(" + _LITE + r"|room)\s+writer\b", r"\1 brighter", t)
    t = re.sub(r"\bmake (it|the (?:\w+ ){0,3}?(?:lights?|room)) dinner\b(?!\s+(?:ready|time|plans))", r"make \1 dimmer", t)
    # levels / colours
    t = re.sub(r"\b(?:brider|bryder|brighter|briter|brighton)\b", lambda m: "brighten" if m.group(0) == "brighton"
               else "brighter", t)
    t = re.sub(r"^writer$", "brighter", t)
    t = re.sub(r"\bdemure\b", "dimmer", t)
    t = re.sub(r"^tim (?=the\b)", "dim ", t)
    t = re.sub(r"\b(lights?|lamps?)\s+tim$", r"\1 dim", t)
    t = re.sub(r"\b(to|it|lights?|lamps?)\s+(?:read|fred|wed)\b", r"\1 red", t)
    t = re.sub(r"\b(to|it|lights?|lamps?)\s+blew\b", r"\1 blue", t)
    t = re.sub(r"\bper pull\b", "purple", t)
    t = re.sub(r"\bwarm wide\b", "warm white", t)
    t = re.sub(r"\b(lights?|lamps?)\s+swarmer\b", r"\1 warmer", t)
    t = re.sub(r"^make a (?=(?:blue|red|green|purple|pink|orange|yellow|white|brighter|dimmer|warmer|cooler|darker)\b)",
               "make it ", t)
    t = re.sub(r"\b(to|lights?)\s+lowe\b", r"\1 low", t)
    t = re.sub(r"\brarely low\b", "really low", t)
    t = re.sub(r"\bweight down\b", "way down", t)
    t = re.sub(r"\bhave brightness\b", "half brightness", t)
    t = re.sub(r"\bto have$", "to half", t)
    t = re.sub(r"\bfull bright nest\b", "full brightness", t)
    t = re.sub(r"\bboat (?=(?:\w+ )?(?:" + _LITE + r"|bedroom|living|dining)\b)", "both ", t)
    # "to 75" written as "275" ("Dinner room lights, 275%." / "Living room lights 260."): a 2 + a level 10-99 / 100
    # (never a clock time: "at 230" / "in 245")
    t = re.sub(r"(?<!at )(?<!in )(?<!by )\b2(\d\d)(?=\s*(?:%|percent|per cent)?\s*$)", lambda m: f"to {m.group(1)}"
               if m.group(1) != "00" else m.group(0), t) \
        if re.search(r"\b(?:lights?|room|bedroom)\b", t) or \
        re.fullmatch(r"2\d\d(?:\s*(?:%|percent|per cent))?", t.strip()) else t
    t = re.sub(r"\b(light|lamp|bulb|number) to (?=(?:to\b|up$|down$|on$|off$))", r"\1 two ", t)
    t = re.sub(r"\bbit the room\b", "bedroom", t)
    # "turn" eaten into "we're in the / you're the / they're in the" - only with a light word AND what to do
    t = re.sub(r"^(?:were|youre|theyre|their|there) (?:in )?the (?=.*\b(?:lights?|lamps?|brightness)\b.*\b(?:on|off|up|"
               r"down|red|blue|green|purple|pink|white|percent|dim|dimmer|brighter)\b)", "turn the ", t)
    # numbers
    t = re.sub(r"\bwon\b", "one", t)
    # ("light when" = "light one" only at the END - "turn off the lights when we go to bed" is a when)
    t = re.sub(r"\b(light|lamp|bulb|number)\s+when(?=\s*$|\s+(?:on|off|please|thanks)\s*$)", r"\1 one", t)
    t = re.sub(r"\bnumb or\b", "number", t)
    t = re.sub(r"\btin(?=\s*(?:percent|%))", "ten", t)
    t = re.sub(r"\bone into\b", "one and two", t)
    t = re.sub(r"\bjustin of\b", "just one of", t)
    t = re.sub(r"\bfurs\b(?=\s+(?:\w+\s+){0,2}" + _LITE + r")", "first", t)
    t = re.sub(r"\bsec and\b(?=\s+(?:\w+\s+){0,2}" + _LITE + r")", "second", t)
    # verbs (said at the start)
    t = re.sub(r"^(?:said|sat|sit)\s+(?=(?:the\s+)?(?:\w+\s+){0,4}?to\s+(?:\d|full|max|half))", "set ", t)
    t = re.sub(r"^shot (off|out|down)\b", r"shut \1", t)
    t = re.sub(r"^swish\b", "switch", t)
    t = re.sub(r"^(?:jim|dumb|them|ten|dem)\s+(?=the\b)", "dim ", t)
    t = re.sub(r"^(?:turning|turned) (on|off)\b", r"turn \1", t)
    t = re.sub(r"^(?:which|witch) (on|off)\b", r"switch \1", t)
    t = re.sub(r"^(?:which|witch) the (.+) (on|off)$", lambda m: m.group(0) if re.search(r"\b(?:are|is|was)\b", m.group(1))
               else f"switch the {m.group(1)} {m.group(2)}", t)
    return t


def _aliases(rooms: dict, extra: dict | None) -> list[tuple[str, str]]:
    pairs: dict[str, str] = {}
    for canon, forms in DEFAULT_ALIASES.items():
        for f in forms:
            pairs.setdefault(f, canon)
    for canon in rooms:
        pairs[canon.lower()] = canon.lower()
        pairs.setdefault(canon.lower().replace(" ", ""), canon.lower())
    for f, canon in (extra or {}).items():
        pairs[str(f).lower()] = str(canon).lower()
    return sorted(pairs.items(), key=lambda p: -len(p[0]))


_ON_VERBS = r"(?:turn|switch|flip|power|put|click|knock|light|bring|fire|crank|throw|get|cut|pop|chuck|whack|stick|kick|mash)"
_OFF_ONLY = r"(?:kill|cut|douse|extinguish|deactivate|disable|darken|blackout|black out|nix|close|shut)"
_ON_ONLY = r"(?:activate|enable|illuminate|light up|lighten up|open|fire up|power up)"
_TOGGLE = r"(?:toggle|flip|switch|hit|get|do|click|flick|press|turn)"
_QUESTION = (r"^(?:are|is|was|were|which|what|whats|do|does|did|have|has|how many|any|anything|tell me|check|"
             r"show me|status|can you tell me|let me know|whether|how bright|how dim)\b")
_NOT_A_COMMAND = (r"^(?:who|why|when|how come|how do|how can|how would|how to|how does|how long|how much|where|whose|"
                  r"what happens|what time|whyd|why did|is it (?:ok|okay|alright) (?:to|if)|should i|can i turn|"
                  r"could i turn|do i|in light of|this little light|you light up|light up the grill)\b")
_NEGATED = (r"\b(?:don'?t|do not|dont|never|stop|quit|no need to|not|without)\s+(?:\w+\s+){0,2}?"
            r"(?:turn|switch|shut|touch|change|dim|flip|kill|cut|put|turning|switching)\b")
_STATEMENT = (r"\b(?:went|gone|go|goes|going|came|come|comes|flickered|flickering|died|blew|blown|keeps?|kept|"
              r"turned|switched|shut|left|is|are|was|were|been|look|looks|looking|seems?)\b")


# ---------------------------------------------------------------------------------- clauses
_CONNECT = r"\s+(and then|and|but|then|also|plus|including|as well as|along with|after that)\s+"   # (kept by split)


# the speaker's own room, said as words ("in here" / "this room" / "the lights in here" / "the ones in here")
_HERE_WORDS = (r"(?:for\s+)?(?:the\s+)?(?:(?:ones?|lights?|lamps?|bulbs?)\s+)?(?:in\s+)?"
               r"(?:here|right here|this room|the room|this one|in this room)")
# "the rest" / "everything else" / "the other lights" (-> every light except here, or except the ones just named)
_OTHERS_RX = (r"\b(?:the rest(?: of (?:the |them|it)?(?:lights?|lamps?|rooms?|house)?)?|everything else|all else|"
              r"the others|all the others|the other ones|the other (?:rooms|lights|lamps|bulbs)|all the other|"
              r"every other (?:room|light)|other rooms|everywhere else)\b")
# heat / AC talk, not a light colour ("make it warmer", "cool the bedroom down") - "warm white" / "cool white" ARE colours
_TEMP_RX = (r"\b(?:warmer|cooler|colder|whiter|hotter|chilly|freezing)\b|\b(?:warm|cool|cold|heat)\b(?!\s+white)|"
            r"\b(?:warm|heat|cool)\s+(?:\w+\s+){0,3}?(?:up|down)\b")


def _leave_out(tg: Target, ex_rooms: list, ex_bulbs: list, here: bool) -> None:
    """Take rooms / bulbs / the speaker's room out of a command's target ("... but not the bedroom", "... but leave
    bedroom light one on", "... but not in here")."""
    if (tg.kind == "all" and (ex_bulbs or here)) or (tg.kind == "here" and here and not tg.nums and not tg.count):
        # ("turn off the lights but not in here": here minus here would be nothing - it means every OTHER light)
        tg.kind, tg.rooms, tg.minus = "except", list(tg.minus) + [r for r in ex_rooms if r not in tg.minus], []
        tg.bulbs = list(ex_bulbs)
        tg.except_here = tg.except_here or here
        return
    if tg.kind == "except":
        tg.rooms += [r for r in ex_rooms if r not in tg.rooms]
        tg.bulbs += [b for b in ex_bulbs if b not in tg.bulbs]
        tg.except_here = tg.except_here or here
        return
    if tg.kind == "bulbs":
        keep = [b for b in tg.bulbs if b not in ex_bulbs and b.rsplit(" ", 1)[0] not in ex_rooms]
        if keep:
            tg.bulbs = keep
        return
    tg.minus += [x for x in ex_rooms + ex_bulbs if x not in tg.minus]        # all / rooms / here
    tg.except_here = tg.except_here or here


def _has_direction(c: str) -> bool:
    """Does this piece say what to do (on/off/out/up/down, a verb, a %, a colour, a question)?"""
    return bool(re.search(rf"\b(?:on|off|out|up|down|dim|brighten|brighter|dimmer|percent|{_CMD_VERB})\b", c)
                or re.match(_QUESTION, c) or _color(c))


def clauses_of(text: str, plugs: set | None = None, places: list | None = None) -> list[str]:
    """Raw sentence -> normalized clauses. ". ? ! ;" always end a clause; a comma or and/but/then starts a new one
    only when what follows says what to do by itself ("..., turn on the dining room" / "... and the bedroom off");
    a plain list continues the clause before ("the living room, dining room and bedroom lights") - unless it's about
    something else ("... including the music", "... and the porch light"), which stays its own clause."""
    other = rf"\b(?:{_NOT_LIGHTS}" + "".join(f"|{re.escape(p.lower())}" for p in (plugs or ())) + r")\b"
    colors = "|".join(re.escape(c) for c in sorted(COLORS, key=len, reverse=True))
    # what can end "turn the bedroom and the living room lights ___": on/off, a level, a colour
    dir_tail = (rf"(?:on|off|out|up|down|dim|dimmer|brighter|(?:to\s+)?{_NUMBER}(?:\s*(?:percent|per cent))?|"
                rf"(?:to\s+)?(?:max|maximum|full|half)|(?:to\s+)?(?:{colors}))")
    excl_lead = r"^(?:not|except|excluding|apart from|other than|but not|besides)\b"
    pairs_w = [(w, w) for w in (places or ())]

    def bare_target_cmd(prev: str) -> bool:
        """"turn the bedroom" / "set the living room lights": a verb and a target, nothing said about what to do."""
        m = re.match(r"^(?:turn|switch|flip|flick|put|get|knock|click|pop|power|set|make|change|paint)\s+(.+)$", prev)
        return bool(m) and not _has_direction(m.group(1)) and not re.search(r"\d", m.group(1)) and \
            not re.search(other, prev)

    out: list[str] = []
    for si, sent in enumerate(re.split(r"[.?!;]+(?=\s|$)", text or "")):
        pieces: list[str] = []
        raws: list[str] = []
        # "turn off the lights because I'm going to the bedroom": the reason is not part of the command. A leading
        # reason goes up to its comma ("Since I'm going to bed, turn off the lights" - stripping to the end lost the
        # command); a trailing one up to the next command ("... cause I'm going to bed and turn on the bedroom")
        sent = re.sub(r"^\s*(?:because|cause|cuz|since)\b[^,]*,\s*", "", sent, flags=re.I)
        # (", and the living room" after the reason is the command's afterthought: "turn off the bedroom lights since
        # I'm going to sleep, and the living room")
        sent = re.sub(r"(?<=\w)\s*,?\s*\b(?:because|cause|cuz|since)\s+(?:i|im|i'm|ill|i'll|we|were|we're|the kids|"
                      r"everyone|everybody|he|she|they)\b.*?(?=\s*,?\s+and\s+(?:then\s+)?(?:turn|switch|shut|dim|set|make|"
                      r"kill|put|brighten)\b|\s*,\s*(?:then\s+)?(?:turn|switch|shut|dim|set|make|kill|put|brighten)\b|"
                      r"\s*,\s*(?:and\s+)?(?:also\s+)?(?:the|my|our|all|every)\b|"
                      r"\s+and\s+(?:also\s+)?(?:the|my|our)\s+(?:\w+\s+){0,2}?(?:room|bedroom|kitchen|lights?|lamps?)\b|$)",
                      "", sent, flags=re.I)
        for raw in re.split(r",", sent):           # "..., but the living room, too." - the "too" belongs before it
            if raws and not normalize(raw) and re.fullmatch(r"\s*(?:too|also|as well|only)\W*", raw.lower()):
                raws[-1] += " " + raw.strip()
            else:
                raws.append(raw)
        for raw in raws:
            p = normalize(raw)
            # normalize() drops a trailing "too" / "also" - read it off the raw words ("..., but the bedroom ones too")
            tail = re.search(r"\b(too|also|as well|only)\W*$", raw.strip().lower())
            bits = re.split(_CONNECT, " " + p + " ")
            joins = [""] + bits[1::2]                  # the word each piece was split off with ("and" / "but" ...)
            qs = bits[0::2]
            last = max((j for j, q in enumerate(qs) if q.strip()), default=-1)
            for j, (q, join) in enumerate(zip(qs, joins)):
                q = q.strip()
                if not q:
                    continue
                number_tail = re.match(r"^(?:number|num|no|one|two|three|four|five|six|\d+)\b", q)  # "light, number two"
                # "... and the living room to 20": a piece with its OWN level is its own command, not a list item
                own_level = re.search(rf"\bto\s+(?:{_NUMBER}|max|maximum|full|half)\b(?!\s*(?:minutes?|mins?|hours?|"
                                      r"seconds?|pm|am|o ?clock))", q)        # ("at 10" is a time, not a level)
                # ... but only when the piece before has its OWN level / was an off: "dim the bedroom and the
                # living room to 20" is ONE level for both rooms (round-4 split it: the bedroom only dimmed a step)
                split_level = own_level and pieces and (
                    re.search(rf"\b(?:to|at)\s+(?:{_NUMBER}|max|maximum|full|half)\b|\bpercent\b", pieces[-1]) or
                    re.search(r"\b(?:off|out)\b|^(?:kill|shut|cut|douse)\b|\bon\s*$", pieces[-1]) or _color(pieces[-1]))
                # ("turn the bedroom blue and the living room to 20" / "the bedroom lights on and the living room to
                # 20" are two commands too)
                # (only a TARGET continues the clause: "turn off the lights, I'm going to the living room" named a
                # room in passing and switched IT - round 7)
                # (a time / "completely" / "a little" left over is still a list item: "turn off the living room and
                # the bedroom in 10 minutes" is ONE command - split, the living room went off NOW and the bedroom
                # was a delayed TOGGLE. Only a clause of its own - a subject or verb - is kept apart)
                # (and a remark AFTER the target is just its tail: "... and the dining room lights so I can see" /
                # "... and the dining room when we go to bed" stay one command; the piece is its own clause only when
                # it STARTS with a subject / verb, or names no light and says something is somewhere)
                lead_subj = re.match(r"^(?:(?:and|so|um|uh|well|oh|but)\s+)*(?:i|im|ive|ill|id|we|were|weve|you|youre|"
                                     r"he|hes|she|shes|its|theyre|they|there|theres|is|are|was|am|going|gonna|heading|"
                                     r"coming|leaving|getting|need|needs|want|wants|wanna|have|has|had|will|cant|dont|"
                                     r"doesnt|be|been)\b", q)
                q_core = re.sub(r"\b(?:when|whenever|once|before|after|until|till|so|since|because|cause|cuz|as soon as)"
                                r"\b.*$", "", q)
                says_where = not re.search(rf"\b{LIGHT_NOUN}\b", q) and re.search(
                    r"\b(?:is|are|was|were|am|has|have|had|went|got|gets|goes|sleeping|sitting|eating|playing)\b",
                    _leftover(_drop_to(q_core), pairs_w))
                target_only = places is None or not (lead_subj or says_where)
                fix = re.match(r"^(?:no\s+)?(?:i mean|i meant|actually|sorry|or rather|make that)\s+(.+)$", q)
                if pieces and fix and _find_rooms(fix.group(1), pairs_w) and _find_rooms(pieces[-1], pairs_w) and \
                        not _has_direction(fix.group(1)):
                    # "Turn on the kitchen lights, I mean the dining room": the room said last REPLACES the first
                    prev = pieces[-1]
                    for w in sorted({a for a, _ in pairs_w}, key=len, reverse=True):
                        prev = re.sub(rf"\b(?:the\s+|in\s+the\s+)?{re.escape(w)}\b", " ", prev)
                    pieces[-1] = " ".join((prev + " " + fix.group(1)).split())
                    continue
                if places is not None and pieces and (lead_subj or says_where) and not _has_direction(q) and \
                        not re.search(other, q) and not re.match(r"^(?:and\s+)?(?:i|we) (?:want|need|wanna)\b", q):
                    # a remark after the command ("..., it's dark in here" / "..., I'm going to the living room" /
                    # "..., the kids are asleep") is said in passing - alone it read as "it's dark in here" = lights
                    # ON (from the kitchen, every light in the house)
                    continue
                if pieces and (number_tail or not _has_direction(q) and not re.search(other, q) and
                               not re.match(excl_lead, q) and not split_level and target_only):
                    # "... the living room" + "dining room lights". A "but" keeps its meaning: "every light but the
                    # living room" = EXCEPT (dropping it would switch ONLY the living room - the opposite) - unless
                    # it adds ("but also the bedroom", "but the bedroom too", "but in the living room") or limits
                    # ("but only / just the bedroom", "but the bedroom only")
                    adds = re.match(r"^(?:also|too|even|in|on|at|as well)\b", q) or (j == last and tail and tail.group(1) != "only")
                    only = re.match(r"^(?:only|just|solely|simply)\b", q) or (j == last and tail and tail.group(1) == "only")
                    sep = (" except " if join == "but" and not adds and not only else
                           " and " if join == "and" or (join == "but" and adds) else " ")
                    pieces[-1] = pieces[-1] + sep + q
                elif (pieces and join == "and" and bare_target_cmd(pieces[-1]) and not re.search(other, q) and
                      re.match(rf"(?:the\s+)?(?:\w+\s+){{0,4}}?(?:{LIGHT_NOUN}\s+)?{dir_tail}\b", q) and
                      not re.match(rf"^(?:{_CMD_VERB}|{_TOGGLE})\b", q)):
                    # "turn the living room and dining room lights on / to 50 / blue (in 10 minutes)": ONE command
                    # for both (split, the first half was a toggle - it switched an already-on room OFF). Only when
                    # the first half said nothing but the verb + a room: "turn the bedroom blue and the living room
                    # off" is two
                    pieces[-1] = pieces[-1] + " and " + q
                elif (pieces and join == "" and (bare_target_cmd(pieces[-1]) or (
                        places is not None and not _has_direction(pieces[-1]) and not re.search(other, pieces[-1]) and
                        re.search(rf"\b(?:{LIGHT_NOUN}|{'|'.join(re.escape(w) for w in places)})\b", pieces[-1]) and
                        not _leftover(pieces[-1], pairs_w))) and
                      re.fullmatch(rf"{dir_tail}(?:\s+(?:please|thanks|thank you|now))?", q)):
                    # (also a bare target: "Dining room lights, to 75 percent." / "Bedroom lights, off.")
                    # "Make all the lights, purple." (whisper's comma split the colour off - the first half alone was a
                    # TOGGLE: it switched them OFF)
                    pieces[-1] = pieces[-1] + " " + q
                else:
                    if split_level and not re.match(rf"^(?:{_CMD_VERB}|{_TOGGLE}|{_ON_VERBS})\b", q):
                        q = "set " + q          # "set the bedroom to 50 and the living room to 20" -> two levels
                    pieces.append(q)
        # "Turn off the bedroom lights. And the living room." / "... The living room too." / "Turn off the lights. In
        # the bedroom." / "... Dining room, please." / "... All of them.": whisper's sentence break inside one request -
        # the afterthought goes with the command before it (alone it was a TOGGLE that undid it)
        p0_level = pieces and re.search(rf"\bto\s+(?:{_NUMBER}|max|maximum|full|half)\b", pieces[0])
        if si and out and pieces and not _has_direction(pieces[0]) and not re.search(other, pieces[0]) and \
                not re.match(excl_lead, pieces[0]) and _has_direction(out[-1]) and not p0_level and (
                    re.match(r"\s*(?:and|also|plus|and also)\b", sent.lower()) or
                    re.search(r"\b(?:too|also|as well)\W*$", sent.strip().lower()) or
                    # ONLY a target - "I'm going to the bedroom." / "I'm in the bedroom." name a room in passing
                    (len(pieces[0].split()) <= 5 and not _leftover(pieces[0], [(w, w) for w in (places or ())]) and (
                        re.search(r"\b(?:all of them|every light|all the lights|everything|everywhere|the whole house)\b",
                                  pieces[0]) or
                        any(re.search(rf"\b{re.escape(w)}\b", pieces[0]) for w in (places or ()))))):
            out[-1] = out[-1] + " and " + pieces.pop(0)
        out.extend(pieces)
    # "In 20 minutes, turn off the lights." / "At 7, turn on the lights." / "Turn off the lights. In 10 minutes.":
    # a piece that is ONLY a time belongs to the command next to it (alone it was dropped: the lights switched NOW)
    merged: list[str] = []
    pending_time = ""
    for idx, c in enumerate(out):
        # ("wait 10 minutes, then ..." / "give me five minutes and then ..." are times too)
        c_t = re.sub(r"^(?:wait|give me|give us|hold on|hang on)\s+(?=(?:a|an|\d+|" + _NUMBER + r")\b)", "in ", c)
        c_t = re.sub(r"\s+(?:and|then|and then)$", "", c_t)
        d, du, left = _timing(c_t, None, bare_hour=True)   # ("At 7," alone IS a time - it joins its command)
        if (d or du) and not re.sub(r"\b(?:okay|ok|so|please|um|uh|then|and)\b", "", left).strip():
            if idx + 1 < len(out) or not merged:
                pending_time = (pending_time + " " + c_t).strip()     # the NEXT command's time ("X. In 10 minutes, Y.")
            else:
                merged[-1] = merged[-1] + " " + c_t                     # the last piece: the one before
            continue
        merged.append((c + " " + pending_time).strip() if pending_time else c)
        pending_time = ""
    if pending_time:
        merged.append(pending_time)
    return [c for c in merged if c]


def parse_detail(text: str, rooms: dict, bulbs: dict, plugs: set | None = None, aliases: dict | None = None,
                 now: float | None = None) -> tuple[list[LightCmd], list[str]]:
    """-> (light commands, the clauses that weren't light commands - "the porch on", "play some music")."""
    cmds: list[LightCmd] = []
    rest: list[str] = []
    if re.search(r"\blights? camera\b|\bcamera lights? action\b", normalize(text)):
        return [], [normalize(text)]            # "lights, camera, action" - a saying
    apairs = _aliases({k.lower(): v for k, v in (rooms or {}).items()}, aliases)
    cl = clauses_of(text, plugs, [a for a, _ in apairs if len(a) > 3])
    other = rf"\b(?:{_NOT_LIGHTS}" + "".join(f"|{re.escape(p.lower())}" for p in (plugs or ())) + r")\b"

    def leave_out(part: str) -> bool:
        """Take the rooms / bulbs / "in here" named in `part` out of the last command's target (True = done)."""
        part = part.strip()
        bare = part
        for a, _ in apairs:                     # ("the tv room" IS a room: look for other things outside room names)
            bare = re.sub(rf"\b{re.escape(a)}\b", " ", bare)
        if re.search(other, bare) and not re.search(rf"\b{LIGHT_NOUN}\b", part):
            return False                        # "... but leave the bedroom TV on" is not about the bedroom lights
        here_w = bool(re.fullmatch(_HERE_WORDS, part)) or part == "room"
        ex = _find_rooms(part, apairs)
        exb = _named_bulbs(part, bulbs or {}, apairs, single=True)
        if not (here_w or ex or exb):
            # "... but not light two" / "... but leave light one on": a bulb NUMBER of the room just named (or of
            # here) - it was toggled on its own, in the speaker's room
            nums = _bulb_numbers(part) if re.fullmatch(rf"(?:the\s+)?(?:(?:{LIGHT_NOUN}|number)\s+)?(?:number\s+)?"
                                                       r"(?:\d+|one|two|three|four|five|six)", part) else []
            if not nums:
                return False
            tg = cmds[-1].target
            if tg.kind == "rooms" and len(tg.rooms) == 1:
                _leave_out(tg, [], [f"{tg.rooms[0]} {n}" for n in nums if f"{tg.rooms[0]} {n}" in (bulbs or {})], False)
            elif tg.kind == "bulbs":
                keep = [b for b in tg.bulbs if b.rsplit(" ", 1)[-1] not in {str(n) for n in nums}]
                tg.bulbs = keep or tg.bulbs
            else:
                tg.minus_nums = list(tg.minus_nums) + nums         # Lights.resolve binds them to HERE (or asks)
            return True
        _leave_out(cmds[-1].target, [] if exb else ex, exb, here_w)
        return True

    def keep_part(c: str) -> tuple[str, str, str] | None:
        """"leave the bedroom on" / "keep the bedroom lit" / "leave on the bedroom light" / "the dining room stays on"
        / "leave the light on in the bedroom" -> (the part kept, on|off, c2); None = not that kind of clause."""
        c2 = re.sub(r"\s+(?:for now|for a while|for a bit|for the night|for tonight|tonight|for)$", "", c)
        tail = re.search(r"\s+for\s+((?:the\s+|my\s+|our\s+)?\w+(?:\s+\w+)?)$", c2)
        if tail and not _find_rooms(tail.group(1), apairs):
            c2 = c2[:tail.start()]              # "... leave the living room on for the dog"
        c2 = re.sub(r"\b(?:lit|going|burning)$", "on", c2)
        k = (re.match(r"^(?:but\s+)?(?:leave|keep)\s+(?P<p>.+?)\s+(?P<d>on|off)"
                      r"(?:\s+(?:(?P<h>in here|in this room|here)|(?:in|for)\s+(?P<r>.+)))?$", c2)
             or re.match(r"^(?:but\s+)?(?:leave|keep)\s+(?P<d>on|off)\s+(?P<p>.+)$", c2)
             or re.match(r"^(?:but\s+)?(?P<p>.+?)\s+(?:is\s+|s\s+)?(?:stays?|staying|remains?|remaining|should stay|"
                         r"can stay|stay|remain)\s+(?P<d>on|off)$", c2))
        if not k:
            return None
        part = k.group("p")
        generic = re.fullmatch(rf"(?:the\s+)?(?:{LIGHT_NOUN}|ones?)", part)
        if k.groupdict().get("h") and generic:
            part = "in here"                    # "... but leave the light on in here"
        elif k.groupdict().get("r"):            # "... leave the light on in the bedroom"
            part = k.group("r") if generic else part + " " + k.group("r")
        return part, k.group("d"), c2

    def kept(part: str) -> tuple[list, list, bool] | None:
        """The rooms / bulbs / "here" a kept part names (None = no light place in it, or it's the TV etc.)."""
        part = part.strip()
        bare = part
        for a, _ in apairs:
            bare = re.sub(rf"\b{re.escape(a)}\b", " ", bare)
        if re.search(other, bare) and not re.search(rf"\b{LIGHT_NOUN}\b", part):
            return None
        here_w = bool(re.fullmatch(_HERE_WORDS, part)) or part == "room"
        exb = _named_bulbs(part, bulbs or {}, apairs, single=True)
        ex = [] if exb else _find_rooms(part, apairs)
        return (ex, exb, here_w) if (here_w or ex or exb) else None

    pending = None                              # a leading "leave the bedroom on" (and ...) waiting for its command
    for i, c in enumerate(cl):
        # "leave the bedroom on and turn everything else off": the kept room goes OUT of the command that follows
        if not cmds and pending is None and i + 1 < len(cl):
            kp = keep_part(c)
            got = kept(kp[0]) if kp and re.match(r"^(?:leave|keep)\b", c) else None
            if got:
                pending = (got, kp[1], c)
                continue
        m = re.match(r"^(?:but not|not|except|excluding|apart from|other than|besides)\s+(?:for\s+)?(?:the\s+|in\s+the\s+)?(.+)$", c)
        if m and cmds and leave_out(m.group(1)):    # "... but not the dining room" / "..., not in here"
            continue
        if m and cmds and not re.search(r"\b(?:bright|brighter|dim|dimmer|dark|darker|low|high)\b", c):
            rest.append(c)                      # an exception we can't place is never a command of its own (it was
            continue                            # toggled); "..., not too bright" is still a dimmer
        if m and not cmds and i + 1 < len(cl):  # "Not the bedroom, the living room lights off." - just a correction
            continue
        # "turn off all the lights but leave the bedroom on" / "... keep the bedroom lit" / "... leave on the bedroom
        # light" / "... the dining room stays on": that room is kept OUT (it was switched with the rest - 10-16)
        if cmds and cmds[-1].action in ("on", "off"):
            # "turn the bedroom lights off and on" / "... off and back on (again) (in 5 minutes)": the same lights,
            # the other way
            d0, du0, c_nt = _timing(c, now)
            bd = re.fullmatch(r"(?:then\s+)?(?:back\s+)?(on|off)(?:\s+again)?", c_nt)
            if bd and bd.group(1) != cmds[-1].action:
                cmds.append(LightCmd(bd.group(1), copy.deepcopy(cmds[-1].target), delay=d0, duration=du0, text=c,
                                     inherited=True))
                continue
            kp = keep_part(c)
            c2 = kp[2] if kp else c
            if kp and (kp[1] == "on") != (cmds[-1].action == "on"):
                if re.fullmatch(rf"(?:the\s+)?(?:{LIGHT_NOUN}|ones?)", kp[0]) and \
                        re.search(r"\b(?:everything|whole house|entire house|every room)\b", cmds[-1].text) and \
                        not re.search(rf"\b{LIGHT_NOUN}\b", cmds[-1].text):
                    # "turn everything off but leave the lights on": that's the TV / plugs, NOT the lights - the
                    # whole sentence goes on (it turned every light off - the opposite)
                    rest.extend([cmds.pop().text, c])
                    continue
                if leave_out(kp[0]):
                    continue
            # "turn on the lights but keep them dim / low": on, then down to a night-light level
            if cmds[-1].action == "on" and re.match(r"^(?:but\s+)?(?:keep|leave|have|make)\s+(?:them|it|the lights|those)\s+"
                                                    r"(?:dim|dimmed|low|soft|not too bright|down low)$", c2):
                cmds.append(LightCmd("set", copy.deepcopy(cmds[-1].target), percent=30, text=c, inherited=True))
                continue
        cmd = _parse_one(c, rooms, bulbs, plugs, aliases, now, first=not cmds and i == 0)
        if cmd is None:
            rest.append(c)
            continue
        if pending and not (cmd.action in ("on", "off") and (cmd.action == "on") != (pending[1] == "on")):
            # "keep the bedroom lights on and dim them / set the rest to 20 / make it blue": not an exclusion - the
            # lead is a command of its own, FIRST, so "them / the rest / the other one" bind to it
            lead = _parse_one(pending[2], rooms, bulbs, plugs, aliases, now, first=True)
            if lead is not None:
                cmds.append(lead)
            else:
                rest.append(pending[2])
            pending = None

        def not_those(prev: Target) -> Target:
            """HERE, minus the rooms / bulbs just switched off ("turn off the bedroom and set the lights to 50" from
            the kitchen = the REST of the house at 50, not the bedroom relit)."""
            tg = Target(after_off=True)
            if prev.kind == "rooms":
                tg.minus = list(prev.rooms)
            elif prev.kind == "bulbs":
                tg.minus = list(prev.bulbs)
            return tg

        if cmd.target.kind == "ref" and cmds:   # "turn on the bedroom lights and make them blue"
            if cmds[-1].action == "off" and (cmd.action == "color" and re.search(_TEMP_RX, c) or cmd.action == "brighten"):
                rest.append(c)                  # "turn off the bedroom lights and make it warmer": the heat;
                continue                        # "turn off the lights and turn it up": the volume
            if cmds[-1].action == "off" and cmd.action == "dim":
                cmd.target = not_those(cmds[-1].target)   # "... and make it darker": not the lights just switched off
            else:
                # "turn them back on" / "make them blue" / "turn them back on at 7": the same lights (a copy: a later
                # "but not X" mustn't edit the first command) - keeping an exception said with it ("... except X")
                ph = cmd.target
                cmd.target = copy.deepcopy(cmds[-1].target)
                if ph.minus or ph.except_here:
                    _leave_out(cmd.target, [x for x in ph.minus if x not in (bulbs or {})],
                               [x for x in ph.minus if x in (bulbs or {})], ph.except_here)
                if ph.minus_nums:
                    cmd.target.minus_nums = list(ph.minus_nums)
            cmd.inherited = True
        elif cmd.target.other_one and cmds and cmds[-1].target.kind == "bulbs":
            # "turn off bedroom light one and turn on the other one": the other bulb(s) of THAT room
            cmd.target = _other_bulbs(cmds[-1].target.bulbs, rooms, bulbs)
        elif cmds and cmd.target.others and cmds[-1].target.kind in ("rooms", "bulbs"):
            # "turn on the bedroom lights and turn off the rest / everything else / the other lights": every light
            # except the ones just named (not "except the speaker's room")
            prev = cmds[-1].target
            cmd.target = Target(kind="except", rooms=list(prev.rooms) if prev.kind == "rooms" else [],
                                bulbs=list(prev.bulbs) if prev.kind == "bulbs" else [])
        elif cmds and cmd.target == Target() and cmd.action in ("dim", "brighten", "set", "color") and \
                not re.search(r"\b(?:here|this room|the room|everywhere)\b", c):
            if cmds[-1].action in ("on", "set", "dim", "brighten", "color"):
                # "turn on the bedroom lights, not too bright" = the bedroom, dimmer
                cmd.target = copy.deepcopy(cmds[-1].target)
                cmd.inherited = True
            elif cmds[-1].action == "off":
                # never the lights just switched off: "turn off the bedroom and dim the lights" = HERE (the rest)
                cmd.target = not_those(cmds[-1].target)
        if cmds and cmd.action in ("on", "off") and _bare_verb(c) and i + 1 < len(cl):
            continue                            # "Turn off, turn on the living room light": the correction wins
        if pending and cmd.action in ("on", "off") and (cmd.action == "on") != (pending[1] == "on"):
            (ex_rooms, ex_bulbs, here_w), _, _ = pending
            if cmd.target.others or cmd.target.kind == "all":     # "... and turn everything else off"
                cmd.target = Target(kind="except", rooms=list(ex_rooms), bulbs=list(ex_bulbs), except_here=here_w)
            else:
                _leave_out(cmd.target, ex_rooms, ex_bulbs, here_w)
            pending = None
        cmds.append(cmd)
    if pending:                                 # "leave the bedroom on" with no command to keep it out of: as before
        lead = _parse_one(pending[2], rooms, bulbs, plugs, aliases, now, first=True)
        if lead is not None:
            cmds.insert(0, lead)
        else:
            rest.insert(0, pending[2])
    # "Turn off, turn on living room light": a bare verb with no target, followed by a real command -> drop it
    if len(cmds) > 1 and _bare_verb(cmds[0].text):
        cmds = cmds[1:]
    return cmds, rest


def _other_bulbs(named: list, rooms: dict, bulbs: dict) -> Target:
    """"the other one" after "bedroom light one" -> the room's other bulb(s)."""
    rooms = {k.lower(): v for k, v in (rooms or {}).items()}
    homes = {b.rsplit(" ", 1)[0] for b in named}
    other = [b for r in homes for b in rooms.get(r, []) if b in (bulbs or {}) and b not in named]
    return Target(kind="bulbs", bulbs=other) if other else Target(kind="bulbs", bulbs=list(named))


def parse_all(text: str, rooms: dict, bulbs: dict, plugs: set | None = None, aliases: dict | None = None,
              now: float | None = None) -> list[LightCmd]:
    return parse_detail(text, rooms, bulbs, plugs, aliases, now)[0]


def parse(text: str, rooms: dict, bulbs: dict, plugs: set | None = None, aliases: dict | None = None,
          now: float | None = None) -> LightCmd | None:
    cmds = parse_all(text, rooms, bulbs, plugs, aliases, now)
    return cmds[0] if cmds else None


def _bare_verb(c: str) -> bool:
    return bool(re.fullmatch(rf"(?:{_ON_VERBS}|{_OFF_ONLY}|{_ON_ONLY})(?:\s+(?:on|off|out|up))?", c.strip()))


# ---------------------------------------------------------------------------------- one clause
def _leftover(t: str, alias_pairs: list, extra: str = "") -> str:
    """What's left once the target, command and filler words are taken out (a real command leaves nothing)."""
    rx = (rf"\b(?:{LIGHT_NOUN}|the|a|an|all|of|every|everything|everywhere|whole|entire|house|home|place|my|our|"
          rf"both|in|here|this|that|these|those|room|rooms|and|or|plus|please|now|too|also|just|again|back|"
          rf"{'|'.join(re.escape(a) for a, _ in alias_pairs)}|"
          rf"{'|'.join(re.escape(a) for a in list(MISHEARD) + list(AMBIGUOUS))}|"
          rf"one|two|three|four|five|six|number|\d+|first|second|third|fourth|percent|per cent|"
          rf"{_CMD_VERB}|{_ON_VERBS}|{_OFF_ONLY}|{_ON_ONLY}|{_TOGGLE}|on|off|out|up|down|way|"
          rf"single|only|other|others|rest|else|ones|except|but|not|apart|from|than|besides|including|"
          rf"half|halfway|full|max|maximum|brightness|bright|brighter|dimmer|darker|lighter|low|lowest|"
          rf"possible|as|they|it|them|go|goes|can|setting|level|mode|"
          rf"{'|'.join(re.escape(c) for c in COLORS)}{extra})\b")
    return re.sub(r"\s+", " ", re.sub(rx, " ", t)).strip()


def _drop_to(t: str) -> str:
    """"lights to 70 percent" / "to max" / "to green" -> the "to" (and a spelled number) go, for the leftover check."""
    t = re.sub(rf"\b(?:to|at)\s+{_NUMBER}\b", " ", t)
    return re.sub(rf"\b(?:to|at)\s+(?=(?:max|maximum|full|half|{'|'.join(re.escape(c) for c in COLORS)})\b)", " ", t)


def _is_command(t: str, alias_pairs: list) -> bool:
    """Is this clause said AS a command (not a story / statement / remark)?"""
    if re.match(rf"^(?:{_CMD_VERB}|{_ON_ONLY}|{_OFF_ONLY}|{_TOGGLE})\b", t):
        return True
    if re.match(rf"^(?:on|off)\b", t):                                   # "off all lights" (turn clipped)
        return True
    if re.match(rf"^{_WANT}\b", t):                                      # "I want the lights off"
        return True
    if re.match(r"^(?:dont|don t) (?:leave|keep)\b", t):                # "Don't leave the lights on" (-> off)
        return True
    if re.match(r"^(?:lights?|lamps?|bulbs?) (?:out|on|off|up|down)\b", t):
        return True
    if re.match(rf"^(?:(?:the|my|our|all|every)\s+)?(?:\w+\s+){{0,2}}(?:room\s+)?(?:to|at)\s+{_NUMBER}(?:\s*percent)?$", t) \
            and _find_rooms(t, alias_pairs):
        return True                                                      # "bedroom to 10" / "dining room to forty"
    if re.match(rf"^(?:the\s+|my\s+|our\s+)?(?:\w+\s+){{0,2}}(?:{LIGHT_NOUN}|it|room)\s+(?:are|is|seem|seems|look|looks)\s+"
                r"(?:too|so|really|very|kinda|kind of|a bit|pretty|way too)\s+(?:bright|dim|dark)$", t):
        return True                                                      # "the lights are too dim"
    # light-first: "bedroom lights off", "lights 50 percent", "living room blue", "lights to max", "all lights" (toggle)
    if not _leftover(_drop_to(t), alias_pairs):
        return True
    if re.match(r"^(?:its |it is |its getting |it s )?(?:too|so|really|very|kinda|kind of|a bit|pretty)?\s*"
                r"(?:bright|dark|dim|pitch black|black)\b(?: in here| in the \w+(?: room)?)?$", t):
        return True                                                      # "it's too bright in here"
    if re.match(r"^(?:more|less|some|softer|brighter|dimmer|darker|lighter|less bright) (?:light|lighting|"
                r"lights)\b", t) or t in ("more light", "i need light", "i need some light", "i cant see",
                                          "give me some light", "brighter", "dimmer", "darker"):
        return True
    return False


def _parse_one(t: str, rooms: dict, bulbs: dict, plugs, aliases, now, first: bool = True) -> LightCmd | None:
    t = t.strip()
    if not t:
        return None
    rooms = {k.lower(): v for k, v in (rooms or {}).items()}
    plugs = {p.lower() for p in (plugs or set())}
    alias_pairs = _aliases(rooms, aliases)

    has_noun = bool(re.search(rf"\b{LIGHT_NOUN}\b", t))
    room_hits = _find_rooms(t, alias_pairs)
    misheard, ask = _misheard(t, room_hits)
    if misheard:
        room_hits = room_hits + [r for r in misheard if r not in room_hits]
    all_word = bool(re.search(r"\b(?:all|every|everything|everywhere|whole house|entire house|whole home|whole place|"
                              r"the house|every room|all rooms|all the rooms|all over|all of them|both rooms|"
                              r"every single)\b", t))
    house_word = bool(re.search(r"\b(?:everything|everywhere|whole house|entire house|whole home|whole place|"
                                r"every room|all rooms|all the rooms)\b", t))   # the house itself, not "it all"

    # ---- not ours ----
    if any(re.search(rf"\b{re.escape(p)}\b", t) for p in plugs):
        return None                                                  # the porch plug has its own path
    if re.search(rf"\b(?:{_NOT_LIGHTS})\b", t) and not (has_noun and re.search(
            rf"\b(?:{_CMD_VERB}|on|off)\b[^.]*\b{LIGHT_NOUN}\b|\b{LIGHT_NOUN}\b\s+(?:on|off|out)\b", t)):
        return None
    xl = re.search(rf"\bexcept (?:for )?(?:the )?{LIGHT_NOUN}\b(?!\s+(?:number\s+)?(?:\d+|one|two|three|four|five|six)\b)"
                   r"(.*)$", t)                                    # ("except light one" is a bulb, not "the lights")
    if xl and not (_find_rooms(xl.group(1), alias_pairs) or re.search(r"\b(?:here|this room)\b", xl.group(1))):
        return None                     # "turn off everything except the lights (tonight / in 10 minutes / for now)"
        #                                 - only "... except the lights in the bedroom / in here" names a place
    if not has_noun and re.search(_TEMP_RX, t) and (room_hits or not re.search(r"\b(?:it|them|they)\b", t)):
        # heat / AC talk, not the lights: "make the bedroom warmer", "cool the bedroom down", "warm up the bedroom",
        # "make it warmer in the bedroom" (only "make it / them warmer" right after a light command can be - a ref)
        return None
    if re.search(r"\b(?:play|song|music|queue|queue up|shuffle|by)\b", t):
        if re.match(r"^(?:play|queue|shuffle|put on (?!the |all |every |my |our )\w+ by)\b", t) or not has_noun or \
                re.search(r"\bby\s+(?!(?:the|my|our|a|an|\d+|ten|twenty|half|halfway|percent|itself|themselves)\b)"
                          r"[a-z]+", t) and not room_hits:
            return None                                              # "turn on lights by ellie goulding" = a song
    if re.search(_NOT_A_COMMAND, t):
        return None
    if re.match(r"^(?:dont|don t|do not) (?:leave|keep)\b", t):     # "Don't leave the lights on." -> off
        t = re.sub(r"^(?:dont|don t|do not) (?:leave|keep)\b", "turn", t)
        t = re.sub(r"\bon$", "off", t)
    elif re.search(_NEGATED, t) and not re.search(r"\b(?:don'?t|do not|dont) (?:leave|keep)\b", t):
        return None                                                  # "don't turn off the lights" - nothing to do
    if re.search(r"\b(?:remind|reminder|remember|timer|alarm|note|notes|jot|keep in mind|make a note|take a note|"
                 r"write down|forget|memo)\b", t) and not re.search(r"\bforget it\b", t):
        return None                                                  # memory / reminders own these
    if re.search(r"\b(?:tell(?! me\b)|ask|announce|broadcast|intercom|drop in|call|text|message)\b|"
                 r"\btell me (?:when|if (?:anyone|someone|somebody))\b", t):
        return None                                                  # the intercom / brain ("tell me which ..." = status)
    if re.search(r"\b(?:keep|keeps|kept) (?:turning|switching|flicking|going|coming)\b|\bby themselves\b|"
                 r"\bon (?:their|its) own\b", t):
        return None                                                  # "the kids keep turning on the lights"
    if re.match(r"^(?:leave|keep)\b", t) and not first:
        return None                                                  # "turn off the TV but leave the lights on"
    # the time words come off BEFORE anything below ("turn off the bedroom in 10 minutes" has no light word, and
    # "minutes" left over made it not-a-command; "turn them off except the bedroom in 10 minutes" lost its time)
    delay, duration, t = _timing(t, now)
    # "turn off the lights when the movie ends" / "at sunset" / "tomorrow" / "later": a time she can't keep - never
    # act NOW (it switched them right away); say what she can do instead
    # (only when the deferral is ATTACHED to the command and no real time was found; "when you get a chance" / "until
    # morning" / "once more" are not deferrals - and a comma afterthought "..., I'll be back later" isn't either)
    tq = re.sub(r"\bwhen(?:ever)? you (?:get|have) a (?:sec|second|minute|moment|chance)\b|\bwhenever you(?:re| are) ready\b|"
                r"\bonce more\b|\bonce and for all\b|\b(?:until|till)\b.*$", "", t)
    # (up to 3 words may sit between: "make the bedroom dark when the baby falls asleep", "turn the lights on for me
    # later", "... later when I go to bed" - but not a subject: "turn on the lights so I can see when ..." is now)
    d_tgt = (r"\b(?:lights?|lamps?|bulbs?|\w*room|kitchen|here|everything|house|home|everywhere|downstairs|upstairs|"
             r"place|on|off|out|it|them|up|down)")
    d_gap = r"(?:\s+(?!(?:i|im|ill|id|we|were|you|youre|he|she|they|its|and|but|then|so)\b)\w+){0,3}?"
    if (has_noun or room_hits or all_word) and re.search(r"\btomorrow\b", tq):
        return LightCmd("refuse", Target(), text=t)  # ("at 6 tomorrow morning" was timed for 6 PM - and a timer that
        #                                              far off is lost if she restarts)
    if not (delay or duration) and (has_noun or room_hits or all_word) and re.search(
            rf"{d_tgt}{d_gap}\s+(?:(?:when|whenever|once|after|before|as soon as)\s+(?!\d)\w+|"
            r"the (?:minute|second)\s+(?:i|we|you|he|she|they|it|my|our)\b)|"     # (not "the second bedroom light")
            rf"{d_tgt}{d_gap}\s+(?:(?:a (?:little|bit) |sometime )?later\b|soon$|tomorrow\b|in a (?:bit|while)\b|"
            r"at (?:sunset|sunrise|dusk|dawn|bedtime)\b)", tq):
        return LightCmd("refuse", Target(), text=t)
    # "turn them off except the bedroom": "them" with the exception said inside the clause - the room named is the
    # one LEFT OUT (read as the target, it switched ONLY that room: the opposite - 10-16 round 3)
    ref_minus, ref_here, ref_nums = [], False, []
    exm = re.search(r"\b(?:except|but not|besides|apart from|other than|excluding)\s+(?:for\s+)?(?:the\s+|in\s+the\s+)?(.+)$", t)
    if exm:
        before = t[:exm.start()].strip()
        if re.search(r"\b(?:it|them|they)\b", before) and not re.search(rf"\b{LIGHT_NOUN}\b", before) and \
                not _find_rooms(before, alias_pairs) and not re.search(r"\b(?:all|every|everything)\b", before):
            part = exm.group(1)
            exb = _named_bulbs(part, bulbs or {}, alias_pairs, single=True)
            ref_minus = exb or _find_rooms(part, alias_pairs)
            ref_here = bool(re.fullmatch(_HERE_WORDS, part.strip()))
            ref_nums = [] if ref_minus else _bulb_numbers(part)        # "turn them off except light one"
            if ref_minus or ref_here or ref_nums:
                t = before
                has_noun, room_hits, ask, all_word, house_word = False, [], [], False, False
    if re.search(r"\b(?:or off|on or|or not)\b", t) and not re.match(rf"^{_CMD_VERB}\b", t):
        if has_noun or room_hits:
            return LightCmd("status", _target(t, alias_pairs, rooms, bulbs, all_word, room_hits), text=t)
        return None
    lightish = bool(has_noun or room_hits or ask or re.search(r"\b(?:brightness|brighter|dimmer)\b", t))

    def target():
        if ask:                                                      # "ring room" -> living or dining? ask
            return Target(kind="here", ask=list(ask))
        return _target(t, alias_pairs, rooms, bulbs, all_word, room_hits)

    # "we're going to bed turn off the lights" / "do me a favor turn off the lights" / "are the living room lights on
    # turn them off": a question-looking start with a COMMAND after it (no comma) - the command is what was asked
    imp = re.search(rf"\b(?:turn|switch|shut|kill|cut|flip|power|put|dim|brighten|set)\s+(?:\w+\s+){{0,4}}?"
                    rf"(?:on|off|out|up|down|to)\b|\b(?:turn|switch|shut|kill|cut)\s+(?:them|it|those|these)\s+(?:on|off|out)\b", t)
    if imp and imp.start() > 0 and re.search(_QUESTION, t) and \
            not re.search(r"\b(?:turned|switched|shut)\s*$", t[:imp.start()]) and \
            not re.search(r"\b(?:should|can|could|do|did|would|will|shall|to)\s+(?:i|we|you)?\s*$", t[:imp.start()]) and \
            not re.search(r"\b(?:do|does|did|has|have|had)\s+(?:\w+\s+){0,3}(?:ever\s+|always\s+|never\s+)?$",
                          t[:imp.start()]):
        # ^ never a question about someone else: "did the kids turn the lights on again?" switched them (round 7)
        cut = t[imp.start():]                                        # (not "which lights should I turn off")
        named = _find_rooms(t[:imp.start()], alias_pairs)
        if named and re.search(r"\b(?:them|it)\b", cut):            # "... the living room lights on turn them off"
            cut = re.sub(r"\b(?:them|it)\b", f"the {named[0]} lights", cut, count=1)
        res = _parse_one(cut, rooms, bulbs, plugs, aliases, now, first)
        if res is not None and (delay or duration) and not (res.delay or res.duration):
            res.delay, res.duration = delay, duration                # (the time came off before the cut)
        return res
    # ---- status questions ----
    if re.search(_QUESTION, t) or re.search(r"\b(?:status|state of)\b", t):
        if lightish or re.search(r"\b(?:anything|everything)\s+(?:on|off|still on|left on)\b", t):
            if re.search(r"\b(?:on|off|lit|status|state|left on|still on|turned on|turned off|bright|brightness|dim|"
                         r"colou?r|level|percent)\b", t) \
                    or re.match(rf"^(?:which|what) {LIGHT_NOUN}\b", t):   # ("what color is the bedroom light")
                return LightCmd("status", target(), text=t)
        return None
    # ---- said as a command? (statements, stories and remarks never switch anything) ----
    if not _is_command(t, alias_pairs):
        return None
    if not has_noun:
        # no light word: only a real light verb counts ("get out of my bedroom" / "get in the bedroom" aren't lights),
        # and the clause may hold NOTHING but command words, the target(s) and filler
        strong = re.match(r"^(?:turn|switch|flip|power|shut|kill|cut|dim|brighten|douse|nix|toggle|extinguish|darken|"
                          r"on|off|fire up|light up|illuminate|black out|blackout)\b", t) or re.match(r"^(?:set|make|put|bring|change)\b.*(?:\bpercent\b|\bto\s+"
                          rf"{_NUMBER}\b|\b(?:{'|'.join(re.escape(c) for c in COLORS)})\b|\bdark\b|\bbright\b)", t) or \
            (re.match(r"^(?:put|get|pop|click|knock|flick|throw|kick)\b", t) and (room_hits or house_word) and
             re.search(r"\b(?:on|off)\b", t)) or \
            (re.match(r"^put\b", t) and (room_hits or house_word) and re.search(r"\bout\b", t))
        # ^ "put the bedroom on" / "get everything off" / "put the living room out" - but never a bare "it all" / "them
        # all" ("get it all out", "kick them all out", "bring everything out" are not the lights - the 10-16 review)
        # light-first = no verb at the front at all ("bedroom off"); any verb there must be one of the above
        # ("throw it all out" started with a verb the list missed and blacked out the house - 10-16)
        light_first = not re.match(rf"^(?:{_CMD_VERB}|{_TOGGLE}|{_WANT}|{_ON_VERBS}|{_OFF_ONLY}|{_ON_ONLY})\b", t)
        if not (strong or light_first or re.match(r"^(?:its|it is|it s|the\s|my\s|our\s|i need|i cant|give me)\b", t)
                or re.match(r"^(?:make|get) (?:it|the room|this room|everything|the \w+(?: room)?) (?:dark|darker|"
                            r"pitch black|bright|brighter|dimmer)\b", t)):
            return None
        if re.search(r"\b(?:out of|into|in to|go to|to the|from the|toward|towards)\b", t) and not re.search(
                rf"\bto\s+{_NUMBER}\b", t):
            return None                                              # "go to the living room" / "out of my bedroom"
        if _leftover(_drop_to(t), alias_pairs):
            if not re.match(r"^(?:its |it is |it s )?(?:too |so |really |very |pretty )?(?:bright|dark|dim|pitch black)\b"
                            r"|^(?:make|get) (?:it|the room|this room|the \w+(?: room)?) (?:dark|darker|bright|brighter|"
                            r"dimmer|cozy|cosy)\b", t) and t not in ("i need light", "i need some light", "i cant see",
                                                                     "give me some light"):
                return None
    if re.search(rf"^(?:{_CMD_VERB}|{_TOGGLE})\b.*\b(?:it|them|they)\b", t) and not has_noun and not room_hits \
            and not all_word and not re.search(r"\bin here\b|\bthis room\b", t):
        tg_ref = True                                                # "turn them off" -> the lights just mentioned
    else:
        tg_ref = False

    def tgt():
        if tg_ref:
            return Target(kind="ref", minus=list(ref_minus), except_here=ref_here, minus_nums=list(ref_nums))
        return target()

    # ---- dark / bright remarks said to her ----
    if re.match(r"^(?:make|get) (?:it|the room|this room|the \w+(?: room)?|everything) (?:dark|pitch black)\b", t) or \
            re.match(r"^(?:make|get) (?:the )?\w+(?: room)? dark\b", t):
        return LightCmd("off", target(), delay=delay, duration=duration, text=t)
    if re.match(r"^(?:its |it is |it s )?(?:so |really |very |pretty )?(?:dark|pitch black)\b(?: in here)?$", t) or \
            t in ("i need light", "i need some light", "i cant see", "give me some light"):
        return LightCmd("on", target(), delay=delay, duration=duration, text=t)

    # ---- brightness / color / dim ----
    pct = _percent(t)
    others_w = bool(re.search(_OTHERS_RX, t))
    if pct is None and (room_hits or house_word or others_w):   # "set the bedroom to 50" / "... the rest to 20"
        m = re.match(rf"^(?:set|turn|put|bring|change|make)\b.*\bto\s+({_NUMBER})(?:\s*(?:percent|per cent))?$", t)
        n = _num(m.group(1)) if m else None
        if n is not None and 0 < n <= 100:
            pct = n
    if pct is None and (has_noun or room_hits or house_word or others_w) and not re.search(r"\b(?:on|off|out)\b", t):
        m = re.search(rf"\bat\s+({_NUMBER})(?:\s*(?:percent|per cent))?$", t)    # "set the lights at 20" (a level -
        n = _num(m.group(1)) if m else None                                        #  _timing left it: no on/off)
        if n is not None and 0 < n <= 100:
            pct = n
    color = _color(t)
    action = _onoff(t)
    if color and action == "off" and re.search(_TEMP_RX, t):
        color = ""                      # "turn off the lights because it's warm in here": OFF, not warm white
    if color and not has_noun and not room_hits and re.search(_TEMP_RX, t):
        # "make it warmer (in here)" is the HEAT, unless a light command was just said ("turn on the bedroom lights" ...
        # "make it warmer"): only then is it the lights - so it's a ref, and Lights.parse_all drops it without one
        return LightCmd("color", Target(kind="ref"), color=color, delay=delay, duration=duration, text=t)
    if color and (lightish or tg_ref or (re.search(r"\b(?:make|set|change|paint|turn|switch)\b", t) and
                                         (all_word or re.search(r"\b(?:it|them|in here|this room|the room)\b", t)))):
        return LightCmd("color", tgt(), color=color, delay=delay, duration=duration, text=t)
    rel = re.search(rf"\b(?:by|up|down)\s+({_NUMBER})\s*(?:percent|per cent)\b", t)
    if pct is not None and not rel and (lightish or tg_ref or others_w or
                                        re.search(r"\b(?:everything|in here|this room|the room)\b", t)) \
            and action != "off":
        return LightCmd("set", tgt(), percent=pct, delay=delay, duration=duration, text=t)
    # ({0,6}: "turn the lights in the dining room down" - with {0,4} it was missed and "by 20 percent" brightened)
    # (a verbless "living room down" / "lights back up" / "bedroom lights low" too - they fell to a toggle: OFF)
    dimmer = re.search(r"\b(?:dim|dimmer|darker|lower|softer|less bright|too bright|turn down|bring down|tone down|"
                       r"half as bright|"
                       r"down a bit|down a little|down some|turn (?:(?!on\b|off\b)\w+ ){0,6}down|bring (?:\w+ ){0,6}down|"
                       r"take (?:\w+ ){0,6}down|lower (?:\w+ ){0,6}|lights? (?:back )?down|brightness down|less light|"
                       r"less lighting)\b|\b(?:lights?|lamps?|bulbs?|it|them|back|here|\w*room|"
                       r"(?<!my )(?<!our )(?<!his )(?<!her )(?<!their )(?<!your )way)\s+"
                       r"(?:(?:really|very|super|real|pretty|nice and|kinda|kind of|fairly|extra)\s+)?(?:down(?:\s+low)?|low)$|"
                       r"\b(?:on|to|at)\s+(?:but\s+)?(?:(?:really|very|super|real|pretty|nice and|kinda|kind of|fairly|"
                       r"extra)\s+)?low$|\b(?:lights?|lamps?|bulbs?|it|them)\s+go(?:es)?\s+down$", t) or \
        re.search(r"^(?!(?:turn|switch) on\b)(?:turn|put|set)\s+(?:\w+\s+){0,3}?(?:lamps?|lights?|bulbs?)\s+on\s+"
                  r"(?:the|my|her|his|our|their)\s+(?:(?!way\b)\w+\s+){1,4}down$", t)    # "the lamp on her side down"
    # (a bare trailing "down / low / up" only right after the lights / a room / it: "turn on the lights so I can sit
    # down" / "... time to get up" are not levels)
    brighter = re.search(r"\b(?:brighten|brighter|more light|lighter|too dark|too dim|not bright enough|turn up|"
                         r"bring up|raise|up a bit|up a little|up some|"
                         r"turn (?:(?!on\b|off\b)\w+ ){0,6}(?<!wake )(?<!get )(?<!warm )(?<!coming )(?<!getting )up|"
                         r"bring (?:\w+ ){0,6}up|crank (?:\w+ ){0,6}up|full brightness|max brightness|way up|"
                         r"lights? (?:back )?up|brightness up)\b|\b(?:lights?|lamps?|bulbs?|it|them|back|here|\w*room)\s+up$", t)
    if pct is None and lightish and re.search(r"\bhalf(?:way)?$", t):
        return LightCmd("set", tgt(), percent=50, delay=delay, duration=duration, text=t)   # "bedroom lights half"
    if re.search(r"\btoo (?:dim|dark)\b|\bnot bright enough\b", t):
        dimmer = None
    # "dim everything" is the lights; "turn everything up / down" could be the music - needs a real light word
    lightword = re.search(r"\b(?:dim|dimmer|darker|brighten|brighter|lighter|less light|more light)\b", t)
    if (dimmer or brighter or rel) and (lightish or tg_ref or (house_word and lightword) or
                                        re.search(r"\b(?:too bright|too dark|too dim)\b", t)):
        if re.search(r"\b(?:full brightness|max brightness|maximum|way up|as bright as|brightest)\b", t):
            return LightCmd("set", tgt(), percent=100, delay=delay, duration=duration, text=t)
        if action == "off" and (re.search(r"\b(?:turn|switch|shut)\s+(?:\w+\s+){0,4}off\b", t) or
                                re.search(r"\b(?:shut|power|close|black|wind)\b.*\bdown$", t)):
            pass                                                     # "turn the lights down... off" / "shut the lights
            #                                                          down" -> off below
        else:
            step = 0
            if rel:
                step = _num(rel.group(1)) or 0
            elif dimmer and re.search(r"(?<!my )(?<!our )(?<!his )(?<!her )(?<!their )(?<!your )\bway down(?: low)?$", t):
                step = 100          # "all the way down" = as low as they go (only the lights that are on)
            up = bool(brighter) and not (dimmer and not brighter)
            if rel and not dimmer and not brighter:
                d = re.findall(r"\b(up|down)\b", t[:rel.start()])    # "... down by 20 percent" = the word before "by"
                up = (d[-1] == "up") if d else rel.group(0).startswith(("by", "up"))
            # "turn the lights on dim" = switch them ON, low (not "dim the lights that are on")
            turn_on = bool(re.match(r"^(?:turn|switch|put|flip|flick|power|get|click|pop)\s+on\b|"
                                    r"^(?:turn|switch|put|flip|flick|power|get|click|pop)\s+"
                                    r"(?:(?!(?:down|up|brightness|dim|dimmer|low|lower)\b)\w+\s+){1,5}?on\b"
                                    r"(?!\s+(?:the|my|our|his|her|their|your)\s)|"   # ("the lamp on her side down")
                                    r"^lights?\s+on\b", t)) and \
                not re.search(r"\b(?:that|which|are|is|still|left|while|when|whats|thats)\b", t)
            return LightCmd("brighten" if up else "dim", tgt(), delay=delay, duration=duration, text=t, step=step,
                            turn_on=turn_on)

    if action == "on" and re.search(r"\bnight ?lights?\b", t):
        return LightCmd("set", tgt(), percent=30, delay=delay, duration=duration, text=t)   # "the night light" = low
    # ---- on / off / toggle ----
    if not (lightish or tg_ref or ((all_word or re.search(_OTHERS_RX, t) or re.search(r"\bthe other (?:one|light|lamp|bulb)\b", t))
                                   and action) or re.search(r"^lights? (?:out|on|off)$", t)):
        if not (action and re.search(r"\b(?:in here|this room|the room)\b", t)):
            return None
    if re.search(r"\b(?:flash|blink|identify|wink|strobe)\b", t) and (lightish or tg_ref):
        return LightCmd("flash", tgt(), text=t)
    if action is None:
        if _leftover(t, alias_pairs, extra="|toggle|do|hit|get|switch|flip"):
            return None                                              # "the lights look nice" - not a command
        if re.search(r"\b(?:make|set|turn|get|put)\b.*\bbright$", t):
            return LightCmd("set", tgt(), percent=100, delay=delay, duration=duration, text=t)   # "make it bright"
        if re.search(r"\b(?:bright|brightness|brighter|dimmer|darker|lighter|low|lowest|half|full|max|maximum)\b", t):
            return None             # a level word she couldn't read is never a TOGGLE ("make it bright" switched it OFF)
        action = "toggle"
    return LightCmd(action, tgt(), delay=delay, duration=duration, text=t)


def _find_rooms(t: str, alias_pairs: list) -> list[str]:
    found: list[str] = []
    taken = t
    for spoken, canon in alias_pairs:
        rx = rf"\b{re.escape(spoken)}\b"
        if re.search(rx, taken):
            if spoken == "master" and re.search(r"\bmaster\s+(?:bath|bathroom|closet|suite|shower)\b", t):
                continue                        # "the master bathroom" is not the (master) bedroom
            if spoken in ("living", "dining", "master", "den", "dinning") and not re.search(
                    rf"\b{re.escape(spoken)}\s+(?:{LIGHT_NOUN}|room|area|and|or|lights?)\b|\b{re.escape(spoken)}$", taken) and \
                    not re.search(rf"\b(?:the|in|my|our)\s+{re.escape(spoken)}\b", taken):
                continue
            taken = re.sub(rx, " ", taken)
            if canon not in found:
                found.append(canon)
    return found


def _misheard(t: str, room_hits: list) -> tuple[list[str], list[str]]:
    """whisper's room-name mishearings, only right before a light word ("victim light", "dead light")."""
    if room_hits:
        return [], []
    for wrong, opts in sorted(AMBIGUOUS.items(), key=lambda p: -len(p[0])):
        if re.search(rf"\b{re.escape(wrong)}\s+{LIGHT_NOUN}\b", t):
            return [], list(opts)
    for wrong, canon in sorted(MISHEARD.items(), key=lambda p: -len(p[0])):
        if re.search(rf"\b{re.escape(wrong)}\s+{LIGHT_NOUN}\b", t):
            return [canon], []
    return [], []


def _onoff(t: str) -> str | None:
    """on / off / None (no direction heard). With several ("turn off turn on the living room light") the LAST wins."""
    if re.search(r"\b(?:power|shut|close|black|wind) (?:\w+ ){0,3}down\b|\bpower down\b|\bpower everything down\b", t):
        return "off"
    if re.search(r"\b(?:power|fire) (?:\w+ ){0,3}up\b", t) and not re.search(r"\bbrightness\b", t):
        return "on"
    def noun_light(m) -> bool:
        """"the light on the nightstand": that "light" is the NOUN, not the verb "light (it) on" (as the last on/off
        hit it turned "turn off the light on the nightstand" into ON)."""
        return m.group(0).startswith("light") and bool(
            re.search(r"\b(?:the|a|that|this|my|our|his|her|your|their|one|bedroom|room)\s+$", t[:m.start()]))

    hits = [(m.start(), m.group(1)) for m in re.finditer(rf"\b{_ON_VERBS}\s+(on|off|out)\b", t) if not noun_light(m)]
    if len(hits) > 1:
        return "on" if hits[-1][1] == "on" else "off"
    if hits:
        return "on" if hits[0][1] == "on" else "off"
    m = next((x for x in re.finditer(rf"\b{_ON_VERBS}\b(?:\s+\w+){{1,7}}?\s+(on|off|out)\b", t) if not noun_light(x)),
             None)                                                               # "turn the bedroom lights off"
    if m:
        # "turn the light on the desk off": that first "on" was a PLACE ("on the desk") - the off at the end wins
        # (it turned the light ON)
        # (only a short place in between - "turn the lights on the dog needs to go out" is ON)
        last = re.fullmatch(r"\s+(?:the|a|my|our|his|her|their|your)\s+(?:\w+\s+){0,2}?(on|off)", t[m.end():])
        if m.group(1) == "on" and last and not re.search(
                r"\b(?:is|are|was|were|s|has|have|had|needs?|wants?|went|got|going|its|i|im|we|you)\b", last.group(0)):
            return "on" if last.group(1) == "on" else "off"
        return "on" if m.group(1) == "on" else "off"
    if re.search(r"\bshut\b|\bshutdown\b", t):
        return "off"
    m = re.search(r"\b(on|off|out)$", t)                                         # "bedroom lights off" / "lights out"
    if m:
        return "on" if m.group(1) == "on" else "off"
    m = re.match(r"^(on|off)\b", t)                                               # "off all lights" (turn clipped)
    if m:
        return m.group(1)
    if re.search(rf"\b{_OFF_ONLY}\b|\blights? out\b|\bput out\b|\bno (?:more )?lights?\b|\bgo dark\b|\bturn out\b", t):
        return "off"
    if re.search(rf"\b{_ON_ONLY}\b|\blet there be light\b|\bleave (?:\w+ ){{0,4}}on\b|\bkeep (?:\w+ ){{0,4}}on\b", t):
        return "on"
    m = re.search(rf"\b{LIGHT_NOUN}\s+(on|off)\b|\b(on|off)\s+(?:the\s+)?(?:\w+\s+){{0,2}}{LIGHT_NOUN}\b", t)
    if m:
        return "on" if "on" in (m.group(1), m.group(2)) else "off"
    if re.search(r"\boff\b", t):
        return "off"
    if re.search(r"\bon\b(?!\s+(?:the|a|my|our)\s+(?:porch|table|wall|ceiling|floor|desk|nightstand|dresser))", t):
        return "on"
    if re.match(r"^(?:crank|light|fire|illuminate)\b", t):
        return "on"                     # "light the bedroom" / "crank the lights" only ever mean on (a toggle could kill them)
    return None


def _percent(t: str) -> int | None:
    """A brightness - only when it's clearly one: "N percent", "set/dim ... to N", "brightness N", half/max."""
    m = re.search(rf"\b({_NUMBER})\s*(?:percent|per cent|pc)\b", t)
    if m and not re.search(r"\b(?:by|up|down)\s+" + re.escape(m.group(1)) + r"\s*(?:percent|per cent|pc)\b", t):
        n = _num(m.group(1))
        return max(1, min(100, n)) if n is not None else None
    m = re.search(rf"\bto\s+({_NUMBER})\b(?!\s*(?:minutes?|mins?|seconds?|secs?|hours?|hrs?|pm|am|o'?clock|oclock|:|"
                  rf"degrees?|past|after|till|until|before))", t)
    if m and re.search(rf"^(?:set|dim|brighten|make|turn|put|bring|change)\b|\bbrightness\b", t) and \
            re.search(rf"\b(?:{LIGHT_NOUN}|brightness)\b|^(?:dim|brighten)\b", t) or \
            (m and re.match(rf"^(?:(?:the|my|our|all|every)\s+)?(?:\w+\s+){{0,2}}(?:room\s+)?to\s+{re.escape(m.group(1))}$", t)):
        n = _num(m.group(1))
        if n is not None and 0 < n <= 100:
            return n
    m = re.search(rf"\b(?:brightness|at)\s+(?:to\s+|at\s+)?({_NUMBER})\s*(?:percent)\b", t) or \
        re.search(rf"\bbrightness\s+(?:to\s+|at\s+)?({_NUMBER})\b", t)
    if m:
        n = _num(m.group(1))
        if n is not None and 0 < n <= 100:
            return n
    if re.search(r"\b(?:half brightness|half bright|halfway|half way|half power|at half|to half)\b", t):
        return 50
    if re.search(r"\b(?:quarter brightness|a quarter brightness|at a quarter|to a quarter)\b", t):
        return 25
    if re.search(r"\b(?:three quarters? brightness|to three quarters)\b", t):
        return 75
    if re.search(r"\b(?:full brightness|max brightness|maximum brightness|full blast|way up|as bright as|brightest)\b", t) \
            or re.search(r"\b(?:to|at)\s+(?:max|maximum|full)\b", t):
        return 100
    if re.search(r"\b(?:lowest|minimum|min brightness|as low as|as dim as|dimmest)\b", t):
        return 10
    return None


def _color(t: str) -> str:
    for name in sorted(COLORS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", t):
            if name == "white" and re.search(r"\bwhite (?:noise|house|russian|wine|claw)\b", t):
                continue
            if name == "green" and re.search(r"\bgreen bay\b", t):
                continue
            if name in ("warmer", "cooler", "colder") and not re.search(rf"\b{LIGHT_NOUN}\b|\bmake (?:it|them)\b", t):
                continue
            return COLORS[name]
    if re.search(r"\b(?:normal|regular|default|usual|standard|plain) (?:color|colour|lights?|white)\b|"
                 r"\bback to (?:normal|regular|default|the usual|usual)\b|\b(?:normal|regular) again\b", t):
        return "warm white"
    return ""


_WHEN_WORDS = {"midnight": (0, 0), "noon": (12, 0), "midday": (12, 0)}


def _timing(t: str, now: float | None, bare_hour: bool = False) -> tuple[int, int, str]:
    """("in 10 minutes" / "in five" / "after an hour" / "10 minutes from now" -> delay, "for 20 minutes" -> duration,
    "at 10:30 pm" / "at 7" / "at ten tonight" / "at midnight" -> delay), and the text without it."""
    delay = duration = 0
    unit = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}

    def secs(n: str, u: str) -> int:
        k = next((k for k in unit if u.startswith(k)), "minute")
        if n == "half an":
            return 1800 if k == "hour" else 30
        v = _num(n)
        if v is None:
            v = {"a": 1, "an": 1, "a couple": 2, "a couple of": 2, "a few": 3, "few": 3}.get(n, 1)
        return v * unit[k]

    nrx = rf"(?:{_NUMBER}|a couple of|a couple|a few|few|an|a|half an)"
    urx = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
    # "in ABOUT 10 minutes" / "in like 10" / "in ANOTHER 10" / "in 10 MORE minutes" / "in 10 to 15 minutes" (the
    # first) / "in an hour AND A HALF" - any of those words between made the time vanish: off right away
    fill = r"(?:(?:about|around|like|roughly|maybe|another|approximately|just|only)\s+)?"
    more = rf"(?:\s+(?:more|extra|or so|(?:to|or)\s+{_NUMBER}))?"
    half = r"(\s+and a half)?"
    for rx in (rf"\b(?:in|after)\s+{fill}({nrx}){more}\s+{urx}{half}\b",
               rf"\b{fill}({nrx}){more}\s+{urx}{half}\s+from now\b"):
        m = re.search(rx, t)
        if m:
            delay = secs(m.group(1), m.group(2))
            if m.group(3):                      # "an hour and a half" / "2 hours and a half"
                delay += secs("1", m.group(2)) // 2
            t = (t[:m.start()] + t[m.end():]).strip()
            break
    m = re.search(rf"\b(?:in|after)\s+{fill}({_NUMBER}|an|a|one)\s+and a half\s+{urx}\b", t)
    if m and not delay:                         # "in one and a half hours"
        delay = int((_num(m.group(1)) or 1) * secs("1", m.group(2)) + secs("1", m.group(2)) // 2)
        t = (t[:m.start()] + t[m.end():]).strip()
    m = re.search(r"\b(?:in|after) half an hour\b", t)
    if m:
        delay = 1800
        t = (t[:m.start()] + t[m.end():]).strip()
    m = re.search(rf"\bin\s+(?:a|one)\s+(?:sec|second|moment|minute|min)\b", t)
    if m and not delay:
        delay = 60 if re.search(r"minute|min", m.group(0)) else 5
        t = (t[:m.start()] + t[m.end():]).strip()
    m = re.search(rf"\bin\s+({_NUMBER})$", t)                     # "turn off the lights in five" (minutes)
    if m and not delay:
        n = _num(m.group(1))
        if n and n <= 180:
            delay = n * 60
            t = t[:m.start()].strip()
    m = re.search(rf"\bfor\s+{fill}({nrx}){more}\s+{urx}{half}\b", t)
    if m:
        duration = secs(m.group(1), m.group(2)) + (secs("1", m.group(2)) // 2 if m.group(3) else 0)
        t = (t[:m.start()] + t[m.end():]).strip()
    m = re.search(rf"\bat\s+(?:(?:around|about|roughly|approximately|like)\s+)?(midnight|noon|midday|(\d{{1,2}}|{_NUMBER})"
                  rf"(?::(\d\d))?\s*(am|pm|a m|p m|o ?clock|oclock|"
                  rf"tonight|this evening|in the morning|in the evening|at night)?)\b(?!\s*percent)", t)
    if m:
        if m.group(1) in _WHEN_WORDS:
            h, mi = _WHEN_WORDS[m.group(1)]
            ap = "x"
        else:
            h = _num(m.group(2)) if m.group(2) else None
            mi = int(m.group(3) or 0)
            ap = (m.group(4) or "").replace(" ", "")
        # a bare "at 20" with no on/off/time word is a LEVEL ("set the lights at 20", "bedroom lights at 15") - read
        # as a clock it scheduled a blind toggle for 8 PM; "turn off the lights at 10" / "lights out at 10" stay times
        bare = ap == "" and not m.group(3)
        rest_t = t[:m.start()] + t[m.end():]
        if bare and not (bare_hour or re.search(r"\b(?:on|off|out)\b|^(?:turn|switch|shut|kill|cut|power|flip|douse)\b", rest_t)
                         or _color(rest_t) or re.search(r"\bpercent\b|\bto\s+\w+|\b(?:dim|dimmer|darker|brighten|brighter|"
                                                        r"lighter|fire up|light up|make|change)\b", rest_t)):
            h = None                            # ("dim the lights at 9" / "make them blue at 7" stay times)
        if h is not None and 0 <= h <= 23:
            if ap in ("pm", "tonight", "thisevening", "intheevening", "atnight") and h < 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            base = now if now is not None else _time.time()
            lt = _time.localtime(base)
            target = _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
            if ap in ("", "oclock") and target <= base and h < 12:
                target += 12 * 3600                                   # "at 10" in the evening = 10 pm
            while target <= base:
                target += 86400
            delay = int(math.ceil(target - base))     # (int() cut it a second short: "6:59 PM")
            t = (t[:m.start()] + t[m.end():]).strip()
    return delay, duration, re.sub(r"\s+", " ", t)


_STOP = {"the", "all", "my", "our", "these", "those", "this", "that", "every", "both", "off", "on", "out", "turn",
         "switch", "shut", "flip", "put", "kill", "cut", "dim", "brighten", "set", "make", "lights", "light", "some",
         "a", "any", "your", "toggle", "hit", "get", "up", "down", "one", "two", "single", "little", "main", "big",
         "overhead", "ceiling", "red", "blue", "green", "what", "which", "are", "is", "were", "was", "how", "many",
         "still", "left", "of", "in", "here", "there", "to", "at", "and", "or", "no", "lamp", "lamps", "bulb", "bulbs",
         "too", "also", "first", "second", "other", "just", "only", "now", "again", "please", "brighter", "dimmer",
         "status", "check", "anything", "everything", "power", "click", "knock", "bring", "fire", "crank", "throw",
         "do", "flick", "press", "night", "bedside", "table", "floor", "desk", "reading", "string", "fairy",
         "lower", "raise", "leave", "keep", "me", "tell", "show", "did", "give", "brighten", "darken", "soften",
         "change", "paint", "toggle", "activate", "enable", "disable", "deactivate", "illuminate", "douse", "us",
         "it", "them", "they", "we", "you", "i", "can", "could", "would", "will", "should", "let", "please",
         "everything", "house", "home", "every", "whole", "entire", "same", "those", "my", "were", "theyre", "want",
         "need", "sure", "have", "nix", "pop", "chuck", "whack", "stick", "kick", "mash", "open", "close", "more",
         "less", "softer", "warmer", "cooler", "back", "rest", "except", "but", "not", "possible", "half", "full",
         "max", "way", "as", "go", "brightness", "percent", "number"}


# "in here" / "this room" = the speaker's room - unless a place follows ("in the room next to ..."); a trailing
# on/off/up/down is just the command ("turn the lights in the room on")
_HERE_RX = (r"\b(?:in here|right here|(?:in )?this room|in the room|in (?:this|the) one)\b(?!\s+(?:next|across|over there|"
            r"at|by|with|where|that|the (?!lights?|lamps?)|my|our|your|above|below|behind|beside|near|of|upstairs|"
            r"downstairs|down the|up the)\b)")
# a place that isn't any room she has ("the lights in the room next door", "the upstairs lights") - say so instead of
# switching HERE. Only when it's attached to the lights: "turn off the lights, I'm going outside" is still HERE
_FAR_PLACE = (r"(?:the )?room (?:next door|upstairs|downstairs|across the hall|down the hall|above|below|the kids are in)|"
              r"upstairs|downstairs|next door|across the hall|down the hall|outside|out back|out front")
_FAR = (rf"\b{LIGHT_NOUN}\s+(?:(?:on|off|out)\s+)?(?:(?:that are|thats|which are)\s+)?(?:(?:in|out|over|up|down|on)\s+)?"
        rf"(?:the\s+)?({_FAR_PLACE})\b|\b(?:the\s+)?({_FAR_PLACE})\s+{LIGHT_NOUN}\b")
# "the lamp by the couch" -> the room that thing is in (only when that room has lights)
_NEAR_ROOM = [("couch", "living room"), ("sofa", "living room"), ("tv", "living room"), ("television", "living room"),
              ("fireplace", "living room"), ("recliner", "living room"), ("dining table", "dining room"),
              ("table", "dining room"), ("nightstand", "bedroom"), ("night stand", "bedroom"), ("bed", "bedroom"),
              ("dresser", "bedroom")]
# "the room next to the kitchen": a room named only by where it is - never the landmark room itself. "the room" /
# "a room" right before it (NOT "the living room near the window"), and no "by" ("... in the dining room by 10 percent")
_BESIDE = (r"\b(?:the|a)\s+(room (?:next to|beside|near|across from|opposite|behind|above|below)\s+(?:the\s+)?"
           r"(?!\d)[a-z]+(?: room)?)")


def _target(t: str, alias_pairs: list, rooms: dict, bulbs: dict, all_word: bool, room_hits: list) -> Target:
    tg = Target()
    m = re.search(_BESIDE, t)
    if m:                                                          # "the room next to the kitchen": which one?
        tg.kind = "rooms"
        tg.unknown = m.group(1)
        return tg
    # "the other rooms" / "the rest" / "everything else" / "everywhere except here" / "except for the ones in here"
    others = re.search(_OTHERS_RX, t)
    pre = t[:others.start()] if others else ""
    if others and (_find_rooms(pre, alias_pairs) or re.search(_HERE_RX, pre) or re.search(
            rf"\b(?:{LIGHT_NOUN}|this one)\b(?:(?!\b(?:except|but|apart from|other than|besides|not|excluding)\b).)*"
            r"\band\s+(?:all\s+)?$", pre)):
        tg.kind = "all"                 # "the bedroom and everything else" / "in here and the rest of the house" /
        return tg                       # "turn off the lights and everything else" = ALL of them
    if re.search(r"\bthe other (?:light|lamp|bulb|one)\b", t):
        tg.other_one = True             # the room's other bulb - bound to the bulb just named, or asked about
        return tg
    if others or re.search(r"\b(?:everywhere (?:except here|but here)|"
                           r"(?:except|but)(?: for)? (?:(?:the )?(?:ones?|lights?|lamps?|bulbs?) )?(?:in )?"
                           r"(?:here|right here|this room|the room|this one))\b", t):
        tg.kind = "except"
        tg.except_here = True
        tg.others = bool(others)            # (after "turn on the bedroom": the rest = all but the BEDROOM)
        return tg
    exc = re.search(r"\b(?:except|but|apart from|other than|besides|not|excluding)\s+(?:for\s+)?(?:the\s+|in\s+the\s+)?(.+)$", t)
    if exc and (all_word or re.search(rf"\b{LIGHT_NOUN}\b", t[:exc.start()])):
        part = exc.group(1)
        ex_rooms = _find_rooms(part, alias_pairs)
        ex_bulbs = _named_bulbs(part, bulbs, alias_pairs, single=True)    # "except bedroom light one"
        if ex_bulbs:
            ex_rooms = []
        before = t[:exc.start()]
        named_before = _find_rooms(before, alias_pairs)
        if (ex_rooms or ex_bulbs) and not named_before and not all_word and re.search(_HERE_RX, before):
            # "turn off the lights in this room but not the living room": THIS room (said), minus that
            return Target(kind="here", here_said=True, minus=list(ex_rooms) + list(ex_bulbs))
        # "the bedroom lights except light one": a number with no room = a bulb of the room named before it (or of
        # HERE) - it was picked up as THE target, switching only the one bulb asked to be left alone
        ex_nums = [] if ex_rooms or ex_bulbs else _bulb_numbers(part)
        if ex_nums:
            known_before = [r for r in named_before if r in rooms]
            if len(known_before) == 1:
                ex_bulbs = [f"{known_before[0]} {n}" for n in ex_nums if f"{known_before[0]} {n}" in bulbs]
            elif not named_before:
                tg = _target(before, alias_pairs, rooms, bulbs, all_word, [])
                tg.minus_nums = list(ex_nums)                          # Lights.resolve binds them to HERE
                return tg
        if (ex_rooms or ex_bulbs) and named_before and not all_word:
            tg = _target(before, alias_pairs, rooms, bulbs, False, named_before)   # "the living room but not the dining"
            tg.minus = [r for r in ex_rooms if r not in tg.rooms] + list(ex_bulbs) + tg.minus
            return tg
        if ex_rooms or ex_bulbs:
            tg.kind = "except"
            tg.rooms = list(ex_rooms) if not ex_bulbs else []
            tg.bulbs = ex_bulbs
            return tg
        if re.search(r"\b(?:room|mine|ours)\b", part) and not ex_nums:
            # "every light except my son's room": an exception we can't place is never dropped (that switched the
            # very lights asked to be kept) - she says there's no such room instead
            tg.kind = "rooms"
            tg.unknown = part
            return tg
    named = _named_bulbs(t, bulbs, alias_pairs)
    if named:
        tg.kind = "bulbs"
        # "the living room lights and bedroom light two": the WHOLE living room too (it gave no bulb number)
        whole = [r for r in room_hits if r in rooms and not any(b.rsplit(" ", 1)[0] == r for b in named)]
        tg.bulbs = named + [b for r in whole for b in rooms[r] if b in bulbs and b not in named]
        return tg
    nums = _bulb_numbers(t)
    if re.search(rf"\b(?:just|only)?\s*(?:one|a single|1) (?:of (?:the )?)?(?:\w+ ){{0,2}}{LIGHT_NOUN}\b|"
                 rf"\b(?:just|only) one\b", t) and not nums:
        tg.count = 1
    if room_hits:
        known = [r for r in room_hits if r in rooms]
        if not known:
            tg.kind = "rooms"
            tg.unknown = room_hits[0]
            return tg
        if nums:
            tg.kind = "bulbs"
            tg.bulbs = [f"{known[0]} {n}" for n in nums]
            return tg
        tg.kind = "rooms"
        tg.rooms = known
        tg.unknown = next((r for r in room_hits if r not in rooms), "")
        tg.with_here = bool(re.search(_HERE_RX, t))                 # "the lights in this room and the bedroom"
        return tg
    if nums:
        tg.nums = nums
        return tg
    far = re.search(_FAR, t)
    if far:                                                        # "(all) the lights in the room next door / upstairs"
        tg.kind = "rooms"
        tg.unknown = far.group(1) or far.group(2)
        return tg
    if all_word:
        tg.kind = "all"
        return tg
    if re.search(_HERE_RX, t):
        tg.here_said = True
        return tg                                                  # "the lights in this room" = here
    # "the lamp by the couch" / "the light over the sink" / "next to the bed": a light named by what it's near - the
    # room that thing is in, or no lights there (it fell to HERE: from the kitchen, every bulb in the house)
    m = re.search(rf"\b{LIGHT_NOUN}\s+(?:(?:by|next to|beside|near|over|above|under|underneath|below|behind)\s+"
                  rf"(?:the\s+|my\s+|our\s+)?|(?:out\s+)?on\s+(?:the|my|our|his|her)\s+)((?!\d)\w+(?:\s\w+){{0,3}})$",
                  re.sub(r"\s+(?:on|off|out|up|down)$", "", t))    # ("turn the light on the desk off" / "on my side
    #                                                                 of the bed" / "the light out on the patio")
    if m and not re.search(r"\b(?:percent|half|max|low|minutes?|hours?)\b", m.group(1)):
        thing = m.group(1)
        near = next((r for w, r in _NEAR_ROOM if re.search(rf"\b{w}\b", thing) and r in rooms), None)
        on_form = re.search(rf"\b{LIGHT_NOUN}\s+(?:out\s+)?on\s", m.group(0))
        where = re.sub(r"^out\s+", "", m.group(0)[len(re.match(rf"{LIGHT_NOUN}\s+", m.group(0)).group(0)):])
        if near:
            tg.kind, tg.rooms = "rooms", [near]
            return tg
        if on_form and re.search(r"\b(?:patio|deck|balcony|yard|backyard|lawn|driveway|roof|lanai|veranda|terrace|"
                                 r"porch|garage|side of the house)\b", thing):
            tg.kind, tg.unknown = "rooms", where        # "the lights on the patio": none there (it lit the whole house)
            return tg
        sentence = on_form and re.search(r"\b(?:is|are|was|were|s|has|have|had|just|woke|awake|asleep|up|home|here|"
                                         r"coming|going|gone|back|wants?|needs?)\b", thing)
        furniture = re.search(r"\b(?:counter|countertop|desk|dresser|table|nightstand|night stand|shelf|shelves|wall|"
                              r"ceiling|mantel|mantle|side|bed|couch|sofa|piano|cabinet|cabinets|fridge|stove|island|"
                              r"bookcase|bookshelf|vanity|mirror|end table|coffee table|tv stand|chair)\b", thing)
        if on_form and not sentence and not furniture:
            tg.kind, tg.unknown = "rooms", where        # "the light on the stairs / second floor": none there
            return tg
        if not sentence:
            # "the lamp by the window" / "the light on the counter" - a landmark she can't place: HERE when the
            # speaker's room has lights, else (the kitchen) "I don't know which light that is" (Lights.resolve)
            tg.place = where
            return tg
        # ("turn the lights on the baby is awake": that "on" was the command, not a place)
    # "the light in my son's room" / "in mommy and daddy's room": somebody's room she has no bulbs for (3+ words
    # slipped past the 1-2 word guess below and fell to HERE - from the kitchen, the whole house)
    m = re.search(r"\bin (?:the|my|our|his|her|their|your) ((?:\w+ ){1,3}room)\b", t)
    if m:
        heard = MISHEARD.get(m.group(1))                    # "in the bad room / dying room": whisper's slips
        if heard and heard in rooms:
            tg.kind, tg.rooms = "rooms", [heard]
            return tg
        if m.group(1) in AMBIGUOUS:
            tg.ask = list(AMBIGUOUS[m.group(1)])
            return tg
        tg.kind = "rooms"
        tg.unknown = m.group(1)
        return tg
    # "the bedside lamp" / "the couch lamp" / "the nightstand lamp": the room that thing is in; "the table / desk /
    # floor / reading lamp" with no room: HERE if this room has lights (from the kitchen it switched EVERY bulb)
    m = re.search(rf"\b(\w+(?:\s\w+)?)\s+{LIGHT_NOUN}\b", t)
    if m:
        near = next((r for w, r in _NEAR_ROOM + [("bedside", "bedroom")]
                     if w != "table" and re.search(rf"\b{w}$", m.group(1)) and r in rooms), None)   # (a "table lamp"
        #                                                                     could be any room's: HERE / asked)
        if near:
            tg.kind, tg.rooms = "rooms", [near]
            return tg
        if re.search(r"\b(?:table|desk|floor|reading|corner|accent|standing)$", m.group(1)):
            tg.place = m.group(0)
            return tg
    # "the lights in the garage" / "the garage lights" / "the kitchen light": a place with no bulbs
    m = re.search(rf"\b{LIGHT_NOUN}\s+(?:in|out in|over in|down in|up in)\s+(?:the\s+)?(\w+(?:\s\w+){{0,3}})$", t) or \
        re.search(rf"\b(\w+(?:\s\w+)?)\s+{LIGHT_NOUN}\b", t)
    if m:
        words = [w for w in m.group(1).split() if w not in _STOP and not w.isdigit() and w not in COLORS]
        place = " ".join(words)
        if place in MISHEARD and MISHEARD[place] in rooms:          # "the lights in the bad room / dying room"
            tg.kind, tg.rooms = "rooms", [MISHEARD[place]]
            return tg
        if place in AMBIGUOUS:
            tg.ask = list(AMBIGUOUS[place])
            return tg
        if words and not re.search(rf"^(?:{_ON_VERBS}|{_TOGGLE})$", words[-1]):
            guess = _closest_room(" ".join(words), rooms)
            if guess:
                tg.kind = "rooms"
                tg.rooms = [guess]
                return tg
            tg.kind = "rooms"
            tg.unknown = " ".join(words)
            return tg
    return tg


def _named_bulbs(t: str, bulbs: dict, alias_pairs: list, single: bool = False) -> list[str]:
    """"bedroom 2" (a bulb's own name), and "bedroom light one and dining room light two" (each room's own number)."""
    out: list[str] = []
    for b in bulbs:
        if re.search(rf"\b{re.escape(b.lower())}\b(?!\s*(?:percent|per cent|minutes?|hours?|pm|am))", t):
            out.append(b)
    if out:
        return out
    amap = dict(alias_pairs)
    arx = "|".join(re.escape(a) for a, _ in alias_pairs)
    nrx = r"(?:\d+|one|two|three|four|five|six)"
    pairs: list[str] = []
    for m in re.finditer(rf"\b({arx})\s+(?:{LIGHT_NOUN}\s+)?(?:number\s+)?({nrx}(?:\s+(?:and|or|plus)\s+(?:{LIGHT_NOUN}\s+)?"
                         rf"(?:number\s+)?{nrx})*)\b(?!\s*(?:percent|per cent|minutes?|hours?))", t):
        room = amap.get(m.group(1))
        for n in re.findall(nrx, m.group(2)):
            name = f"{room} {_num(n)}"
            if room and name in bulbs and name not in pairs:
                pairs.append(name)
    m2 = re.search(rf"\b{LIGHT_NOUN}\s+({nrx})\s+(?:and|or|plus)\s+{LIGHT_NOUN}\s+({nrx})\s+in\s+(?:the\s+)?({arx})\b", t)
    if m2:                                                         # "light one and light two in the bedroom"
        room = amap.get(m2.group(3))
        for n in (m2.group(1), m2.group(2)):
            name = f"{room} {_num(n)}"
            if room and name in bulbs and name not in pairs:
                pairs.append(name)
    if single:
        return pairs
    return pairs if len(pairs) > 1 or (pairs and len({p.rsplit(' ', 1)[0] for p in pairs}) == 1 and
                                       len(re.findall(arx, t)) > 1) else []


def _closest_room(word: str, rooms: dict) -> str | None:
    """'livin' / 'bedrom' -> the room it's closest to (only a clear winner)."""
    names = list(rooms)
    if not names:
        return None
    scored = sorted(((difflib.SequenceMatcher(None, word, n).ratio(), n) for n in names), reverse=True)
    if scored[0][0] >= 0.75 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.15):
        return scored[0][1]
    return None


def _bulb_numbers(t: str) -> list[int]:
    nums: list[int] = []
    nrx = r"(?:\d+|one|two|three|four|five|six)"
    m = re.search(rf"\b{LIGHT_NOUN}\s+(?:number\s+|num\s+|no\s+)?({nrx}(?:\s*(?:and|,|plus|or|through|thru)?\s*"
                  rf"(?:(?:the\s+)?(?:\w+\s+){{0,2}}?{LIGHT_NOUN}\s+)?(?:number\s+)?{nrx})*)\b"
                  rf"(?!\s*(?:percent|per cent|minutes?|mins?|hours?|seconds?|pm|am))", t)
    if m:
        nums = [_num(x) for x in re.findall(nrx, m.group(1))]
    if not nums:
        m = re.search(rf"\bnumber\s+({nrx})\b", t)
        if m:
            nums = [_num(m.group(1))]
    if not nums:
        m = re.search(rf"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+(?:\w+\s+){{0,3}}?(?:{LIGHT_NOUN}|one)\b", t)
        if m:
            nums = [_ORDINALS[m.group(1)]]
    return [n for n in dict.fromkeys(nums) if n]
