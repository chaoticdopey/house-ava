"""What to play, as said -> a MusicSpec. Pure text work: no files, no index (names are resolved when it runs).

    "some sad 90s rap"                 moods [sad], years [(1990, 1999)], genres [hip hop]
    "an hour of jazz"                  minutes 60, genres [jazz]
    "hip hop but no trap"              genres [hip hop], not_genres [trap]
    "the album adhd by joyner lucas"   album "adhd", album_artist "joyner lucas"
    "all his albums in order"          discography, pronoun
    "joyner lucas's newest songs"      newest, names ["joyner lucas"]
    "songs featuring logic"            featuring "logic"
    "artists like joyner lucas"        like "joyner lucas"
    "best of eminem" / "eminem's hits" best_of, names ["eminem"]
    "my gym playlist"                  saved "gym" (falls back to the mood 'workout' when there's no such playlist)
    "eminem, logic and joyner lucas"   names [...] (the whole string is tried as ONE name first: "earth wind and fire")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import vocab


_RXC: dict = {}


def _rx(p: str):
    """A compiled pattern from this module's own cache (never Python's shared 512-entry re cache, which the
    router's phrasebook already fills)."""
    r = _RXC.get(p)
    if r is None:
        r = _RXC[p] = re.compile(p)
    return r


_NUMW = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty five": 25,
         "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "seventy": 70, "seventy five": 75,
         "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100, "two hundred": 200,
         "a dozen": 12, "dozen": 12, "a few": 5, "few": 5, "a couple": 3, "a couple of": 3, "couple": 3,
         "couple of": 3, "several": 8, "a handful of": 5, "a handful": 5, "a bunch of": 25, "a bunch": 25,
         "a ton of": 100, "a lot of": 50, "lots of": 50}
NUM = (r"(?:\d{1,3}|a hundred|one hundred|two hundred|hundred|twenty five|forty five|seventy five|a dozen|dozen|"
       r"a few|few|a couple of|a couple|couple of|couple|several|a handful of|a handful|a bunch of|a ton of|"
       r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|"
       r"seventy|eighty|ninety)")
_NOUN = r"(?:songs?|tracks?|tunes?|hits|bangers|jams|records|joints|cuts|bops|singles)"
# the music nouns / filler said around what to play (removed; they name no artist)
_NOUNS = {"songs", "song", "music", "musics", "tracks", "track", "tunes", "tune", "stuff", "jams", "jam", "vibes",
          "bangers", "banger", "mix", "mixtape", "playlist", "play list", "radio", "station", "records", "joints",
          "bops", "cuts", "singles", "single", "catalog", "catalogue", "collection", "library", "selection",
          "stuff on", "tunage", "beats"}
_EDGE = {"some", "a", "an", "the", "of", "from", "by", "please", "me", "us", "for", "to", "something", "anything",
         "everything", "all", "more", "good", "nice", "great", "cool", "just", "only", "mostly", "whatever", "my",
         "our", "your", "that", "those", "these", "in", "on", "type", "kind", "sort", "style", "era", "decade",
         "years", "year", "bit", "little", "lil", "bunch", "lot", "lots", "ones", "random", "randomly", "shuffled",
         "mostly", "really", "pretty", "real", "actual", "classic", "nothing", "but", "and", "with", "or", "plus",
         "stuff", "playing", "now", "right", "today", "tonight", "again", "instead", "too", "also", "else",
         "other", "different", "new", "old", "some of", "few", "list", "kinda", "sorta", "like", "whole", "entire",
         "full", "complete", "start", "finish", "favorite", "favourite", "fav", "fave", "best"}
_PRONOUN = re.compile(r"^(?:his|her|their|him|them|this artist|this guy|this dude|this band|this group|this singer|"
                      r"this rapper|that artist|that guy|that band|the same artist|this one'?s artist)$")
_PICK = {"first": "first", "debut": "first", "oldest": "first", "earliest": "first", "newest": "newest",
         "latest": "newest", "last": "newest", "new": "newest", "most recent": "newest", "recent": "newest",
         "second": "second", "third": "third"}
_PICKW = r"(?:first|debut|oldest|earliest|newest|latest|last|new|most recent|recent|second|third)"
_ALB = r"(?:album|albums|record|lp|cd|ep)"
_POSS = r"(?:'s|s'|’s)"


@dataclass
class MusicSpec:
    raw: str = ""
    genres: list = field(default_factory=list)
    not_genres: list = field(default_factory=list)
    years: list = field(default_factory=list)
    not_years: list = field(default_factory=list)
    moods: list = field(default_factory=list)
    names: list = field(default_factory=list)          # each name said (split on and / , / &)
    names_whole: str = ""                              # the names as one string ("earth wind and fire")
    not_names: list = field(default_factory=list)
    featuring: str = ""
    like: str = ""
    best_of: bool = False
    newest: bool = False
    album: str = ""
    album_artist: str = ""
    album_pick: str = ""
    albums: list = field(default_factory=list)
    discography: bool = False
    count: int | None = None
    minutes: float | None = None
    saved: str = ""
    shuffle: bool | None = None
    pronoun: bool = False
    playlist_word: bool = False
    music_noun: bool = False
    song_by: tuple | None = None                       # ("billie jean", "michael jackson"): a song, said plainly
    leftover: str = ""
    maybe_count: int | None = None                     # "20 eminem songs": a count, if "20 eminem" is no artist
    fallback: "MusicSpec | None" = None                # "my workout playlist" when there's no such playlist

    def structured(self) -> bool:
        """Anything beyond one plain name ('play joyner lucas' / 'play thriller' stay the plain library search)."""
        return bool(self.genres or self.years or self.moods or self.featuring or self.like or self.best_of
                    or self.newest or self.album or self.album_pick or self.albums or self.discography
                    or self.count or self.minutes or self.saved or self.not_genres or self.not_years
                    or self.not_names or len(self.names) > 1 or self.playlist_word or self.pronoun
                    or (self.music_noun and self.names))

    def describe(self) -> str:
        """A few words for her reply: 'sad 90s hip hop'."""
        bits = []
        if self.moods:
            bits.append(" and ".join(self.moods))
        for lo, hi in self.years[:2]:
            bits.append(_era_words(lo, hi))
        if self.genres:
            bits.append(" and ".join(vocab.genre_name(g) for g in self.genres))
        out = " ".join(bits).strip()
        if self.not_genres:
            out += " without " + " or ".join(vocab.genre_name(g) for g in self.not_genres)
        return out or "music"


def _era_words(lo: int, hi: int) -> str:
    now = vocab.this_year()
    if lo == hi:
        return str(lo)
    if hi == now and lo >= now - 6:
        return "newer"
    if lo % 10 == 0 and hi == lo + 9:
        return f"{str(lo)[2:]}s" if lo < 2000 else f"{lo}s"
    if lo <= 1900:
        return f"pre-{hi + 1}"
    return f"{lo} to {hi}"


def _num(w: str) -> int | None:
    w = (w or "").strip()
    if w.isdigit():
        return int(w)
    return _NUMW.get(w)


_MINUTES = re.compile(
    r"(?:(?:for|about|around|like|roughly)\s+)?(?:(?P<h>an|a|one|two|three|four|five|1|2|3|4|5|a few|a couple of|"
    r"a couple)\s+hours?(?P<half>\s+and\s+a\s+half)?|(?P<hh>half an|a half|half a|half)\s+hour|"
    r"(?P<m>\d{1,3}|ten|fifteen|twenty|thirty|forty|forty five|forty-five|sixty|ninety)\s+(?:minutes?|mins?))"
    r"(?:\s+(?:of|worth of|long))?(?:\s+(?:music|songs|stuff|tunes))?(?:\s+of)?(?=\s|$)")
_COUNT_LEAD = re.compile(r"^(?:(?:the )?top )?(?P<n>" + NUM + r")\s+(?:(?:more|of|other|different|good|great|random|"
                         r"new|old)\s+)?(?P<mid>(?:[\w'&-]+\s+){0,4}?)" + _NOUN + r"(?:\s+(?:of|from|by))?(?=\s|$)")
_FEAT = re.compile(r"(?:^|\s)(?:(?:that|which|where)\s+)?(?:featuring|feat|ft|feature|features|featured|with a feature from|"
                   r"with features? (?:from|by)|with (?:a )?verse from|that have|that has)\s+(?P<x>.+?)"
                   r"(?:\s+(?:on (?:it|them|the track|the song)|in (?:it|them)))?$")
_LIKE = re.compile(r"^(?:(?:other |more |some |any |similar )?(?:artists?|bands?|singers?|rappers?|groups?|music|songs?|"
                   r"stuff|tracks|something|anything|more|people|acts))?\s*(?:that (?:sounds?|is|are) )?"
                   r"(?:like|similar to|in the style of|kinda like|kind of like|sort of like|along the lines of|"
                   r"in the vein of|that sounds? like|sounding like)\s+(?P<x>.+)$")
_EXCL = re.compile(r"(?P<lead>(?:,\s*|\s+)(?:but\s+|and\s+|just\s+|only\s+)*)(?P<w>no more|no|without|except(?: for)?|"
                   r"not|minus|excluding|leave out|leaving out|skip|skipping|nothing (?:from|by|that'?s)|none of|"
                   r"anything but|other than|instead of)\s+(?P<x>.+)$")


def _strip_edges(s: str) -> str:
    words = s.split()
    while words and (words[0] in _EDGE or words[0] in _NOUNS):
        words = words[1:]
    while words and (words[-1] in _EDGE or words[-1] in _NOUNS):
        words = words[:-1]
    return " ".join(words)


def _clean(s: str) -> str:
    s = (s or "").lower().replace("’", "'")
    s = _rx(r"[\"“”!?.;:]+").sub(" ", s)
    s = _rx(r"\s+").sub(" ", s).strip(" ,")
    # how it's said, not what: "for me", "real quick", "please", "on winamp", "start to finish"
    for _ in range(3):
        b = s
        s = _rx(r"(?:,?\s+)(?:please|for me|for us|real quick|right now|now|thanks|thank you|ava|on winamp|in winamp|"
                   r"on the computer|on my computer|on the pc|from my library|from my collection|from my music|"
                   r"in my library|on shuffle|shuffled|at random|randomly|in random order|mixed up|"
                   r"from start to finish|start to finish|all the way through|front to back|beginning to end|"
                   r"in order|in track order|in album order|in release order|chronologically|oldest to newest|"
                   r"for a while|for a bit|or something|or whatever|or anything|if you can|if you could)$").sub("", s).strip(" ,")
        if s == b:
            break
    return s


def parse(text: str) -> MusicSpec:
    """The words after the play verb ("some sad 90s rap") -> a MusicSpec."""
    raw = (text or "").strip()
    sp = MusicSpec(raw=raw)
    low = (raw or "").lower()
    if _rx(r"(?:^|\s)(?:on shuffle|shuffled|at random|randomly|in random order|mixed up|shuffle)(?:\s|$)").search(low):
        sp.shuffle = True
    if _rx(r"(?:^|\s)(?:in order|in track order|in album order|in release order|chronologically|oldest to newest|"
                 r"start to finish|from start to finish|front to back|beginning to end|all the way through)(?:\s|$)").search(low):
        sp.shuffle = False
    s = _clean(raw)
    s = _rx(r"^(?:me|us) ").sub("", s)
    s = _rx(r"^(?:(?:some|a little|a bit of|a little bit of|any|more|some more|a mix of|a mixture of|"
            r"a selection of|a playlist of|a list of)\s+)+").sub("", s)
    s = _rx(r"^(?:the|a)\s+(?:whole|entire|full)\s+").sub("the ", s)
    s = _rx(r",?\s+the (?:whole|entire|full) (?:album|record)$").sub(" album", s)
    if not s:
        return sp

    # ---- a saved playlist: "my gym playlist", "the playlist called road trip", "road trip playlist"
    m = (_rx(r"(?:my|our|the|that|this) (?P<n>(?!(?:new|whole|entire|full)\b)[\w' &-]{1,40}?) (?:playlist|play list|mix|mixtape)").fullmatch(s)
         or _rx(r"(?:my |the |our )?(?:playlist|play list|mix) (?:called|named|titled) (?P<n>[\w' &-]{1,40})").fullmatch(s)
         or _rx(r"(?:my |the |our )?(?:playlist|play list) (?P<n>[\w' &-]{1,40})").fullmatch(s))
    if m and m.group("n").strip() and not _rx(r"(?:new|old|whole|entire|own|current|other|last|first|"
                                                       r"favou?rite|usual|regular|default|music|song|songs)").fullmatch(
                                                       m.group("n").strip()):
        sp.saved = m.group("n").strip()
        sp.playlist_word = True
        fb = parse(sp.saved)
        sp.fallback = fb if fb.structured() or fb.names else None
        return sp
    m = _rx(r"(?P<a>an? |some )?(?P<n>[\w' &-]{1,60}?) (?:playlist|play list|mix|mixtape)").fullmatch(s)
    if m and not _rx(r"(?:my|our|the|that|this)\b").match(s):
        inner = parse(m.group("n"))
        inner.raw = raw
        inner.playlist_word = True
        if not m.group("a"):                 # "road trip playlist": his saved one if he has it, else made up
            out = MusicSpec(raw=raw, saved=m.group("n").strip(), playlist_word=True, shuffle=inner.shuffle)
            out.fallback = inner if inner.structured() or inner.names else None
            return out
        return inner

    # ---- how long: "an hour of", "for 30 minutes", "20 songs of", "the top 10"
    m = _MINUTES.search(s)
    if m:
        if m.group("h"):
            h = {"an": 1, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "a few": 3, "a couple of": 2,
                 "a couple": 2}.get(m.group("h"), None) or int(m.group("h"))
            sp.minutes = h * 60.0 + (30.0 if m.group("half") else 0.0)
        elif m.group("hh"):
            sp.minutes = 30.0
        else:
            mm = m.group("m").replace("-", " ")
            sp.minutes = float(_num(mm) or 0) or None
        s = (s[:m.start()] + " " + s[m.end():]).strip()
        s = _rx(r"\s+").sub(" ", s)
    m = _rx(r"^(?:the )?top (?P<n>" + NUM + r")\b\s*").match(s)
    if m:
        sp.count = _num(m.group("n"))
        sp.best_of = True
        s = s[m.end():]
    m = _COUNT_LEAD.match(s)
    if m and not sp.count:
        mid = m.group("mid").strip()
        rest = (mid + " " + s[m.end():]).strip()
        ok_mid = not mid or _all_structure(mid)
        if ok_mid:
            sp.count = _num(m.group("n"))
            sp.music_noun = True
            s = rest
        elif not _rx(r"cent\b|savage\b|doors\b|pilots\b|seconds\b|minutes\b").match(mid):
            sp.maybe_count = _num(m.group("n"))
    s = _rx(r"\s+").sub(" ", s).strip()

    # ---- leave out: "hip hop but no trap", "country without the old stuff", "pop but no taylor swift"
    m = _EXCL.search(s)
    if m and m.start() > 0:
        x = m.group("x").strip()
        head = s[:m.start()].strip(" ,")
        if not _rx(r"me|you|it|us|him|her|them|that|this|one|more|stop|end").fullmatch(_strip_edges(x)) and \
                (_rx(r"\bbut\b|,").search(m.group("lead")) or _all_structure(x) or m.group("w") in (
                    "without", "except", "except for", "excluding", "minus", "other than")):
            ex = parse(x)
            if ex.genres or ex.years or ex.names or ex.moods:
                sp.not_genres += ex.genres
                sp.not_years += ex.years
                sp.not_names += ([ex.names_whole] if ex.names_whole else []) + [n for n in ex.names
                                                                                if n != ex.names_whole]
                s = head

    # ---- "songs featuring logic", "songs with logic on them"
    m = _rx(r"(?:^|\s)with (?P<x>[\w' &$.-]{2,40}?) (?:on|in) (?:it|them|the track|the song|there)$").search(s) \
        or _FEAT.search(s)
    if m and m.start() >= 0:
        x = _strip_edges(m.group("x"))
        if x:
            sp.featuring = x
            s = s[:m.start()].strip()

    # ---- "artists like joyner lucas", "something similar to the weeknd"
    m = _LIKE.match(s)
    if m:
        x = _strip_edges(m.group("x"))
        if x and not _rx(r"this|that|it|this one|that one|this song|that song|the last one|what'?s playing").fullmatch(x):
            sp.like = x
            return sp

    # ---- the albums: "all his albums in order", "eminem's discography", "the album adhd by joyner lucas",
    # "his newest album", "the new morgan wallen album", "albums adhd and evolution"
    m = (_rx(r"(?:all|every one|each|everything)(?: of)?(?: the)? (?:albums|records|lps|cds) (?:by|from|of) "
                      r"(?P<y>.+)").fullmatch(s)
         or _rx(r"(?:all|every)(?: of)? (?:(?P<pr>his|her|their)|(?P<x>.+?)" + _POSS + r"?) "
                         r"(?:albums|records|discography|lps|cds)").fullmatch(s)
         or _rx(r"(?:the )?(?:whole |entire |full )?discography (?:of|by|from) (?P<y>.+)").fullmatch(s)
         or _rx(r"(?:(?P<pr2>his|her|their)|(?P<x2>.+?)" + _POSS + r"?) (?:whole |entire |full )?discography").fullmatch(s)
         or _rx(r"every (?P<x3>.+?) (?:album|record)").fullmatch(s)
         or _rx(r"all (?:the |of the )?(?P<x4>.+?) (?:albums|records)").fullmatch(s))
    if m:
        gd = m.groupdict()
        who = next((gd.get(k) for k in ("y", "x", "x2", "x3", "x4") if gd.get(k)), "")
        pr = gd.get("pr") or gd.get("pr2")
        if pr or (who and _PRONOUN.match(who.strip())):
            sp.pronoun = True
        elif who:
            sp.names = [vocab.possessive_base(_strip_edges(who))]
            sp.names_whole = sp.names[0]
        if sp.pronoun or sp.names:
            sp.discography = True
            sp.shuffle = False if sp.shuffle is None else sp.shuffle
            return sp
    m = (_rx(r"(?:(?P<pr>his|her|their)|(?P<x>.+?)(?:'s|s'|’s|s)?) (?P<pick>" + _PICKW + r") " + _ALB).fullmatch(s)
         or _rx(r"(?:the |that )?(?P<pick>" + _PICKW + r") " + _ALB + r" (?:by|from|of) (?P<x>.+)").fullmatch(s)
         or _rx(r"(?:the |that )?(?P<pick>" + _PICKW + r") (?P<x>(?!album)[\w' &$.-]{2,40}?) " + _ALB).fullmatch(s))
    if m:
        gd = m.groupdict()
        sp.album_pick = _PICK.get(gd.get("pick") or "", "newest")
        if gd.get("pr") or not _strip_edges(gd.get("x") or "") or _PRONOUN.match(_strip_edges(gd.get("x") or "")):
            sp.pronoun = True                  # ("the newest album": the artist playing now)
        else:
            sp.names = [vocab.possessive_base(_strip_edges(gd.get("x") or ""))]
            sp.names_whole = sp.names[0]
        sp.shuffle = False if sp.shuffle is None else sp.shuffle
        return sp
    m = _rx(r"(?:the |that |this )?(?:albums|records) (?P<x>.+)").fullmatch(s)
    if m and _rx(r"\s(?:and|&|plus|then)\s|,").search(m.group("x")):
        sp.albums = [a for a in (_strip_edges(x) for x in _rx(r"\s*,\s*(?:and\s+|then\s+)?|\s+(?:and|&|plus|then)"
                                                                       r"\s+(?:the\s+)?(?:album\s+)?").split(m.group("x"))) if a]
        sp.shuffle = False if sp.shuffle is None else sp.shuffle
        return sp
    m = (_rx(r"(?:the |that |this )?" + _ALB + r" (?:called |named |titled )?(?P<a>.+?)(?: (?:by|from) (?P<b>.+))?").fullmatch(s)
         or _rx(r"(?:the |that |this )?(?P<a>.+?) (?:album|record|lp)(?: (?:by|from) (?P<b>.+))?").fullmatch(s)
         or _rx(r"(?P<a>.+?) (?:by|from) (?P<b>.+?),? (?:the )?(?:whole |entire |full )?(?:album|record)").fullmatch(s)
         or _rx(r"(?:the )?(?P<a>.+?) (?:the )?(?:album|record)").fullmatch(s))
    if m and _strip_edges(m.group("a")):
        a = _strip_edges(m.group("a"))
        if not _rx(r"(?:this|that|the|whole|entire|current|same|his|her|their|new|newest|latest|first|"
                            r"last|next|previous|other)").fullmatch(a):
            sp.album = a
            sp.album_artist = _strip_edges(m.groupdict().get("b") or "")
            if not sp.album_artist and _rx(r"\s(?:by|from)\s").search(a):   # "abbey road by the beatles album"
                aa, _sep, bb = _rx(r"\s(by|from)\s").split(a, maxsplit=1)
                sp.album, sp.album_artist = _strip_edges(aa), _strip_edges(bb)
            if _rx(r"(?:his|her|their)$").match(sp.album_artist):
                sp.pronoun, sp.album_artist = True, ""
            sp.shuffle = False if sp.shuffle is None else sp.shuffle
            return sp

    # ---- "best of eminem", "eminem's greatest hits", "the hits by queen", "80s hits"
    m = (_rx(r"(?:the )?(?:very )?(?:best of|greatest hits(?: of| by| from)?|biggest hits(?: of| by| from)|"
                      r"(?:the )?hits (?:of|by|from)|(?:the )?best (?:songs |stuff |tracks |hits )?(?:of|by|from)|"
                      r"(?:most )?popular (?:songs |stuff |hits |tracks )?(?:of|by|from)|"
                      r"(?:the )?(?:big|biggest|greatest|famous|well known|best known) (?:songs|hits|tracks) (?:of|by|from)|"
                      r"(?:the )?classics (?:of|by|from)|essential) (?P<x>.+)").fullmatch(s)
         or _rx(r"(?P<x>.+?)" + _POSS + r"? (?:(?:greatest|best|biggest|top|famous|most popular|popular|"
                         r"well known|best known|big) )?(?:hits|songs|stuff|tracks|classics|bangers|best)").fullmatch(s))
    if m:
        x = m.group("x").strip()
        whole = _rx(r"(?:greatest|best|biggest|top|famous|most popular|popular|well known|best known|big|"
                         r"the best of|best of)\b").match(s) or _rx(r"\b(?:hits|classics|best|bangers)$").search(s) or \
            _rx(r"(?:greatest|best|biggest|top|famous|popular|well known|best known|big) (?:hits|songs|stuff|"
                      r"tracks|classics)$").search(s)
        if whole:
            sp.best_of = True
            s = x

    # ---- "joyner lucas's newest songs", "the newest songs by logic", "the new taylor swift song"
    m = (_rx(r"(?:the )?(?:newest|latest|new|newer|most recent|recent) (?:songs?|stuff|music|tracks?|singles?|"
                      r"releases?|drops?|joints) (?:by|from|of) (?P<x>.+)").fullmatch(s)
         or _rx(r"(?P<x>.+?)" + _POSS + r"? (?:newest|latest|new|newer|most recent|recent) (?:songs?|stuff|music|"
                         r"tracks?|singles?|releases?|drops?|joints)").fullmatch(s)
         or _rx(r"(?:the )?(?:newest|latest|new|most recent) (?P<x>.+?) (?:songs?|singles?|tracks?|stuff|music|"
                         r"joints)").fullmatch(s))
    if m:
        x = _strip_edges(m.group("x"))
        if x and not _all_structure(x):
            sp.newest = True
            sp.music_noun = True
            if _PRONOUN.match(x):
                sp.pronoun = True
            else:
                sp.names = [vocab.possessive_base(x)]
                sp.names_whole = sp.names[0]
            if _rx(r"\b(?:song|single|track)$").search(s) and not sp.count:
                sp.count = 1 if _rx(r"(?:the )?(?:newest|latest|new|most recent) ").match(s) else sp.count
            return sp

    # ---- "songs by eminem" / "sad songs by eminem" (structure before the by) vs "billie jean by michael jackson"
    m = _rx(r"(?P<x>.+?) (?:by|from) (?P<y>(?!(?:the )?(?:" + vocab.DECADE_WORD + r"|19\d\d|20\d\d)\b).+)").fullmatch(s)
    if m:
        x, y = m.group("x").strip(), _strip_edges(m.group("y"))
        if _all_structure(x) or _rx(r"(?:everything|anything|all|stuff|some stuff|whatever)").fullmatch(x):
            if y and not _all_structure(y):
                sp.names = [vocab.possessive_base(n) for n in _split_names(y)]
                sp.names_whole = y
                sp.music_noun = True
                s = x
        elif not sp.best_of and y:
            sp.song_by = (x, y)
            return sp

    # ---- eras, moods, genres: what's left
    ranges, spans = vocab.era_ranges(s)
    sp.years += ranges
    s = _cut(s, spans)
    # what it's FOR: "music for studying", "songs to fall asleep to", "something for the gym"
    m = _rx(r"(?:^|\s)(?:for|to|while(?: i'?m| we'?re| i| we)?)\s+(?:(?:the|a|an|my|our|some)\s+)?"
                  r"(?P<a>(?:[\w']+\s+){0,4}?[\w']+?)(?:\s+to)?$").search(s)
    if m:
        ms = vocab.find_moods(m.group("a"))
        if ms:
            for _a, _b, k in ms:
                if k not in sp.moods:
                    sp.moods.append(k)
            s = s[:m.start()].strip()
    mspans = []
    for a, b, k in vocab.find_moods(s):
        if k not in sp.moods:
            sp.moods.append(k)
        mspans.append((a, b))
    s = _cut(s, mspans)
    gspans = []
    for a, b, k in vocab.find_spoken_genres(s):
        if k not in sp.genres:
            sp.genres.append(k)
        gspans.append((a, b))
    s = _cut(s, gspans)
    if _rx(r"(?:^|\s)(?:" + "|".join(sorted((re.escape(n) for n in _NOUNS), key=len, reverse=True)) + r")(?:\s|$)").search(
                 s):
        sp.music_noun = True
    if _rx(r"(?:^|\s)(?:classics|hits|bangers|anthems|greatest)(?:\s|$)").search(s) and not sp.names:
        sp.best_of = True
    s = _rx(r"(?:^|\s)(?:classics|hits|anthems|greatest)(?=\s|$)").sub(" ", s)
    words = [w for w in s.split() if w not in _NOUNS]
    s = _strip_edges(" ".join(words))
    if s and not sp.names:
        if _PRONOUN.match(s):
            sp.pronoun = True
        elif not _rx(r"(?:it|that|this|them|those|these|one|something|anything|music|me|you)").fullmatch(s):
            sp.names = [vocab.possessive_base(n) for n in _split_names(s)]
            sp.names_whole = s
            sp.leftover = s
    return sp


def _cut(s: str, spans) -> str:
    if not spans:
        return s
    out, last = [], 0
    for a, b in sorted(spans):
        if a < last:
            continue
        out.append(s[last:a])
        last = b
    out.append(s[last:])
    return _rx(r"\s+").sub(" ", " ".join(out)).strip(" ,")


def _split_names(s: str) -> list[str]:
    parts = [p for p in (_strip_edges(x) for x in _rx(r"\s*,\s*(?:and\s+|&\s+|plus\s+)?|\s+(?:and|&|plus|or|"
                                                             r"with|mixed with|along with|n)\s+").split(s)) if p]
    return parts or [s]


def _all_structure(s: str) -> bool:
    """Only genre / mood / era / music-noun / filler words ('some sad 90s', 'songs', 'old school rap')."""
    t = (s or "").lower()
    _r, spans = vocab.era_ranges(t)
    t = _cut(t, spans)
    t = _cut(t, [(a, b) for a, b, _k in vocab.find_moods(t)])
    t = _cut(t, [(a, b) for a, b, _k in vocab.find_spoken_genres(t)])
    rest = [w for w in t.split() if w not in _NOUNS and w not in _EDGE and w not in ("hits", "classics", "bangers")]
    return not rest
