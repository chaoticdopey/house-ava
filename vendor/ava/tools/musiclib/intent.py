"""The music commands, as said -> {"op": ..., ...}. Pure text work (a saved playlist's NAME may be looked up in her
Playlists folder; nothing else is read). The router turns each op into a step.

    play        "play some 80s rock", "shuffle my gym playlist", "make me a sad playlist", "gimme 90s hip hop"
    create      "make a playlist called road trip with joyner lucas, eminem and logic"
    save        "save this as my gym playlist", "call this playlist summer"
    add         "add this song to road trip", "put this album on my gym playlist"
    remove      "remove this song from road trip"
    show        "what's in my gym playlist"
    list        "list my playlists"
    rename      "rename road trip to summer drive"
    delete      "delete my road trip playlist" (asks for a spoken yes first; it only removes the list)
    skip_artist "skip this artist"            more_like  "play more like this"
    love        "i love this song"             ban        "never play this song again"
    keep_paused "keep it paused"
    albums_by   "what albums do i have by logic"   count_by  "how many songs do i have by eminem"
    status      "how's the music index coming"     reindex   "rescan my music"
A plain "play <name>" is NOT taken here (the library search in the router already plays an artist, a song, an
album or a movie by that name); only a request with some shape to it is.
"""

from __future__ import annotations

import re

from . import playlists, spec


_RXC: dict = {}


def _rx(p: str):
    """A compiled pattern from this module's own cache (never Python's shared 512-entry re cache, which the
    router's phrasebook already fills)."""
    r = _RXC.get(p)
    if r is None:
        r = _RXC[p] = re.compile(p)
    return r


_PL = r"(?:playlist|play list|playlists)"
# a second command riding along ("...and set a timer", "...then lock the pc"): never part of what to play
_SECOND = re.compile(r"(?:,|\s)\s*(?:and|then|and then|also|plus|but)\s+(?:then\s+)?(?:set|turn|remind|open|close|lock|"
                     r"mute|unmute|pause|stop|start|launch|search|google|look|tell|what|what's|how|show|take|dim|"
                     r"brighten|text|send|call|remember|check|read|type|go|switch|change|raise|lower|crank|volume|"
                     r"make it|put it|shuffle it|repeat|skip|wake|cancel|minimi[sz]e|maximi[sz]e|snap|copy|is|are|"
                     r"weather|lights?)\b")
# another domain's things: a play verb with one of these is the lights, the volume, a timer, the TV — never music
_NOT_PLAYABLE = re.compile(r"\b(?:lights?|lamps?|bulbs?|lites?|volume|timers?|alarms?|reminders?|tv|television|telly|"
                           r"jellyfin|plex|netflix|hulu|episodes?|movies?|films?|shows?|series|seasons?|brightness|"
                           r"heat|heater|ac|air|fan|thermostat|screen|monitor|window|tab|wifi|bluetooth|camera|"
                           r"cameras|kettle|oven|stove|game|games|vm|server|computer|pc)\b(?! (?:music|"
                           r"songs?|playlists?|soundtracks?|themes?|tunes)\b)")
_PLAYV = (r"(?:play|put on|throw on|spin|queue up|queue|cue up|cue|blast|bump|start playing|shuffle|listen to|"
          r"let me hear|lemme hear|let's hear|let us hear|i want to hear|i wanna hear|play me|play us|put me on|"
          r"pull up|bring up|crank up|crank|turn on|fire up)")
# shuffle as the player's MODE, not "shuffle <something>"
_MODE_OBJ = re.compile(r"(?:(?:the|my|this|that|these|those|all|of|everything|it|them|em|'em) ){0,4}"
                       r"(?:it|them|em|everything|songs?|music|tracks?|tunes|playlist|play list|list|queue|album|"
                       r"library|stuff|things|order|mode|on|off|back on|again|up|around|please|for me|now|"
                       r"the playlist|my playlist|the songs|my songs|my music|the music)"
                       r"(?: (?:on|off|up|around|again|back on|mode|please|for me|now|on spotify|in spotify|on winamp)){0,3}")
_SONGISH = (r"(?:this(?: (?:song|track|tune|one|jam|banger|record))?|that(?: (?:song|track|tune|one|jam))?|"
            r"the (?:current )?(?:song|track|tune)(?: that'?s (?:on|playing))?|it|what'?s playing|what'?s on|"
            r"the song (?:that'?s )?playing|the one (?:that'?s )?playing|this song right here)")
_ALBUMISH = r"(?:this album|this whole album|the album|the whole album|this record|that album)"
_NOT_A_LIST = re.compile(r"(?:repeat|shuffle|loop|hold|pause|mute|the tv|tv|speaker|speakers|my phone|phone|"
                         r"(?:the )?clipboard|(?:my )?cart|(?:the )?cart|(?:my |the )?(?:shopping|grocery|to do|todo|"
                         r"wish|reading|bucket) list|(?:my )?notes?|(?:my )?calendar|(?:my )?reminders?|(?:the |my )?"
                         r"(?:desktop|downloads|documents|computer|pc|drive|hard drive|flash drive|usb|folder|disk|"
                         r"laptop|library|music folder|nas|server|[a-z] drive)|the queue|queue|favorites bar|bookmarks)$")
_NAME = r"(?P<n>[\w'&$ -]{1,40}?)"


def _name_ok(n: str) -> bool:
    n = (n or "").strip()
    return bool(n) and not _NOT_A_LIST.fullmatch(n) and not _rx(r"(?:it|that|this|them|one|the|my|a)").fullmatch(n)


def _is_list(n: str, said_playlist: bool) -> bool:
    """A target is a playlist when he said 'playlist', or he has a saved one by that name."""
    if not _name_ok(n):
        return False
    return said_playlist or playlists.exists(n)


def parse(t: str, session_live: bool = False) -> dict | None:
    """t = the normalized command. session_live = she started a playlist a moment ago (so "save this as gym" means it)."""
    t = (t or "").strip().lower().rstrip(" .!?")
    if not t or len(t) > 220:
        return None
    t = _rx(r"^(?:ok(?:ay)?|hey|yo|so|um+|uh+|ava|please|now)[, ]+").sub("", t)
    t = _rx(r"[, ]+(?:please|for me|thanks|thank you|ava|now)$").sub("", t)

    # ---- keep it paused (after she paused your music to listen)
    if _rx(r"(?:(?:just|please) )?(?:keep|leave) (?:it|the music|that|the song|everything|spotify|winamp|the video|"
                    r"the player|my music)(?: (?:off|stopped|on pause|paused|paused for now|off for now|stopped for now|"
                    r"that way))?(?: (?:for now|for a bit|for a while))?|(?:keep|leave) (?:it )?paused(?: for now)?|"
                    r"stay paused|(?:don'?t|do not|no need to) (?:resume|unpause|restart|turn back on|start) (?:it|the music|"
                    r"the song|playing|the player|my music)(?: (?:again|back up|yet))?|(?:don'?t|do not) (?:start|turn) "
                    r"(?:it|the music) back (?:on|up)|(?:no|nah),? (?:keep|leave) it (?:off|paused)").fullmatch(t):
        if _rx(r"\b(?:paused|pause|resume|unpause|off|stopped|back on|back up|restart|start)\b").search(t):
            return {"op": "keep_paused"}

    # ---- the playlists themselves
    if _rx(r"(?:list|show|read|tell|name|give)(?: me| off| out)?(?: all)?(?: of)? (?:my|the|our) (?:saved )?"
                    + _PL + r"|(?:what|which) " + _PL + r" (?:do i have|have i (?:got|made|saved)|are there|did i "
                    r"(?:make|save)|do we have|have you (?:saved|made))|(?:do i have any|how many) (?:saved )?" + _PL +
                    r"(?: do i have)?|what are (?:all )?(?:of )?my " + _PL + r"|my " + _PL + r"|(?:list|show me) " + _PL).fullmatch(t):
        return {"op": "list"}
    m = _rx(r"(?:delete|remove|get rid of|trash|scrap|ditch|drop|erase|throw away|throw out|lose|kill|toss|"
                     r"axe|nuke) (?:my |the |that |this |our )?" + _NAME + r" " + _PL + r"(?: for good| completely)?|"
                     r"(?:delete|remove|get rid of|trash|scrap|ditch|erase|toss) (?:my |the |that )?" + _PL +
                     r" (?:called |named |titled )?(?P<n2>[\w'&$ -]{1,40})").fullmatch(t)
    if m:
        n = (m.group("n") or m.group("n2") or "").strip()
        if _name_ok(n) and not _rx(r"(?:song|track|songs|tracks|this song|it)\b").match(n):
            return {"op": "delete", "name": n}
    m = (_rx(r"(?:rename|re-?name|re name) (?:my |the |our )?(?P<a>[\w'&$ -]{1,40}?)(?P<pl> " + _PL +
                      r")? (?:to|as|into) (?:a |the )?(?P<b>[\w'&$ -]{1,40}?)(?: " + _PL + r")?").fullmatch(t)
         or _rx(r"change (?:the name of )?(?:my |the )?(?P<a>[\w'&$ -]{1,40}?)(?P<pl> " + _PL +
                         r")(?:'?s name)? to (?P<b>[\w'&$ -]{1,40}?)(?: " + _PL + r")?").fullmatch(t)
         or _rx(r"(?:call|name) (?:my |the )?(?P<a>[\w'&$ -]{1,40}?)(?P<pl> " + _PL + r") (?P<b>[\w'&$ -]{1,40}?)"
                         r"(?: instead| from now on)?").fullmatch(t))
    if m and _is_list(m.group("a"), bool(m.group("pl"))) and _name_ok(m.group("b")):
        return {"op": "rename", "old": m.group("a").strip(), "new": m.group("b").strip()}
    m = (_rx(r"(?:what'?s|what is|what are|what all is|which songs are|what songs are|what tracks are|"
                      r"tell me what'?s|read me what'?s|show me what'?s|list what'?s|what do i have|what did i put|"
                      r"what have i got|what'?s on|whats on) (?:in|on) (?:my |the |our )?" + _NAME +
                      r"(?P<pl> " + _PL + r")?").fullmatch(t)
         or _rx(r"(?:read|list|show|tell|go through|run through|read off)(?: me)?(?: (?:the|all the))? ?"
                         r"(?:songs|tracks)? ?(?:in|on|from|of)? ?(?:my |the |our )?" + _NAME + r"(?P<pl> " + _PL + r")").fullmatch(t))
    if m and _is_list(m.group("n"), bool(m.group("pl"))):
        return {"op": "show", "name": m.group("n").strip()}

    # ---- save what's playing as a playlist
    m = (_rx(r"(?:save|keep|store|remember|bookmark|make) (?:this|these|that|it|the current|this current|"
                      r"what'?s playing|what i'?m listening to|the songs|these songs|this one)(?P<pl1> (?:playlist|play list|"
                      r"list|mix|queue|songs|tracks|lineup|set|selection))? (?:as|to|into|under|in) (?:a |my |the |our )?"
                      r"(?:new )?(?P<pl2>playlist |play list )?(?:called |named |titled )?" + _NAME +
                      r"(?P<pl3> " + _PL + r")?").fullmatch(t)
         or _rx(r"(?:call|name|title) (?:this|that|it|the current|these songs)(?P<pl1> (?:playlist|play list|list|"
                         r"mix|queue))? " + _NAME + r"(?P<pl3> " + _PL + r")?").fullmatch(t)
         or _rx(r"make (?:this|that|it|these songs|these) (?:into )?(?:my|a|the) " + _NAME + r"(?P<pl3> " + _PL + r")").fullmatch(
                         t)
         or _rx(r"(?:save|turn) (?:this|it|that)(?P<pl1> (?:playlist|list|mix))? into (?:a|my) " + _NAME +
                         r"(?P<pl3> " + _PL + r")").fullmatch(t))
    if m:
        gd = m.groupdict()
        said_pl = bool(gd.get("pl1") or gd.get("pl2") or gd.get("pl3"))
        n = m.group("n").strip()
        if _name_ok(n) and (said_pl or session_live) and not _rx(r"(?:a )?(?:file|document|pdf|layout|note|"
                                                                          r"text file|draft|screenshot|picture|photo)").fullmatch(n):
            return {"op": "save", "name": n}
    if _rx(r"(?:save|keep|store) (?:this|these|that|the current|this current)(?: (?:playlist|play list|mix|list|"
                    r"queue|songs|tracks))(?: (?:for (?:me|later)|as a playlist))?|save (?:this|it) as a playlist").fullmatch(t):
        return {"op": "save", "name": ""}

    # ---- add / remove the song (or the album) that's playing
    m = _rx(r"(?:add|put|stick|throw|save|toss|chuck|drop|pop|slap|include) (?P<w>" + _SONGISH + r"|" + _ALBUMISH +
                     r") (?:to|on|onto|in|into|on to) (?:my |the |our )?" + _NAME + r"(?P<pl> " + _PL + r")?").fullmatch(t)
    if m:
        n = m.group("n").strip()
        songish = bool(_rx(r"\b(?:song|track|tune|album|record|playing|jam|banger)\b").search(m.group("w")))
        if _name_ok(n) and (songish or session_live or _is_list(n, bool(m.group("pl")))) and \
                not _rx(r"(?:repeat|shuffle|loop)").fullmatch(n):
            what = "album" if _rx(r"\b(?:album|record)\b").search(m.group("w")) else "song"
            return {"op": "add", "name": n, "what": what}
    m = _rx(r"(?:add|put|stick|throw|toss|chuck) (?P<x>.{2,80}?) (?:to|on|onto|in|into) (?:my |the |our )?" + _NAME +
                     r" " + _PL).fullmatch(t)
    if m and _name_ok(m.group("n")) and not _rx(r"(?:a|an|some|the)? ?(?:reminder|note|timer|alarm)\b").match(m.group("x")):
        return {"op": "add", "name": m.group("n").strip(), "what": "spec", "text": m.group("x").strip()}
    m = _rx(r"(?:remove|take|delete|drop|pull|get rid of|kick|cut|yank|scratch) (?P<w>" + _SONGISH + r") "
                     r"(?:out of|off(?: of)?|from) (?:my |the |our )?" + _NAME + r"(?P<pl> " + _PL + r")?").fullmatch(t)
    if m:
        n = m.group("n").strip()
        songish = bool(_rx(r"\b(?:song|track|tune|playing|jam|banger)\b").search(m.group("w")))
        if _name_ok(n) and (songish or session_live or _is_list(n, bool(m.group("pl")))):
            return {"op": "remove", "name": n, "what": "song"}
    m = _rx(r"(?:remove|take|delete|drop|cut) (?P<x>.{2,80}?) (?:out of|off(?: of)?|from) (?:my |the |our )?" + _NAME +
                     r" " + _PL).fullmatch(t)
    if m and _name_ok(m.group("n")):
        return {"op": "remove", "name": m.group("n").strip(), "what": "spec", "text": m.group("x").strip()}

    # ---- while it plays: skip this artist, more like this, i love this, never again
    if _rx(r"(?:skip|no more|enough(?: of)?|stop playing|i'?m (?:sick|tired) of|i am (?:sick|tired) of|cut|lose|"
                    r"ditch|drop|block|get rid of|kill|nix|pass on|move on from|i don'?t want|i do not want|no)(?: of)? "
                    r"(?:(?:all|everything|anything|songs|stuff|music|tracks)(?: (?:by|from))? )?(?:this|that|the current|"
                    r"the) (?:artist|band|singer|rapper|group|guy|dude|girl|lady|person|musician|dj)(?:'?s (?:songs|stuff|"
                    r"music))?(?: (?:for (?:now|a while|today|tonight)|right now|anymore|please))?|"
                    r"(?:skip|no more|enough) (?:of )?(?:him|her|them)(?: for (?:now|a while|today))?|"
                    r"no more (?:songs|stuff|music) (?:by|from) (?:him|her|them|this (?:artist|guy|band))").fullmatch(t):
        return {"op": "skip_artist"}
    if _rx(r"(?:(?:play|give me|gimme|i want|i'?d like|let'?s hear|queue up|find me|throw on|put on|keep "
                    r"(?:playing|going with)|do|start) )?(?:more|some more|other|similar|more songs?|more music|more stuff|"
                    r"more tracks|songs|music|stuff|something|anything|tracks|things|more things)(?: (?:just|kind of|sort of|"
                    r"a bit))? (?:like|similar to|along the lines of) (?:this|that|this one|that one|this song|that song|"
                    r"this track|what'?s playing|the current song|this artist|this guy)(?: one)?(?: please)?|"
                    r"(?:play |give me |gimme |more of )?(?:more|some more) of (?:this|that)(?: (?:kind of|sort of|type of) "
                    r"(?:music|stuff|song|thing|vibe))?|(?:play |queue up )?similar (?:songs|music|stuff|tracks|artists)|"
                    r"(?:keep|keep it) (?:going|coming)(?: with)? (?:stuff|songs|music|things|tracks) like this|"
                    r"(?:play |start )?(?:a )?radio (?:station )?(?:from|based on|like) (?:this|this song|this artist)|"
                    r"more like this").fullmatch(t):
        return {"op": "more_like"}
    if _rx(r"(?:never|don'?t|do not|dont) (?:ever )?play (?:this|that)(?: (?:song|track|one|tune))? (?:again|"
                    r"anymore|ever again|no more)|(?:ban|block|blacklist|bury) (?:this|that)(?: (?:song|track|one))?|"
                    r"i (?:never want to|don'?t (?:ever )?want to|do not want to|never wanna|don'?t wanna) hear (?:this|that)"
                    r"(?: (?:song|track|one))? (?:again|anymore|ever again)|thumbs down(?: (?:on|to) this(?: song)?)?").fullmatch(t):
        return {"op": "ban"}
    love_song = _rx(
        r"(?:i )?(?:really |absolutely |just |totally |freaking |so )?(?:love|like|dig|adore|am loving|'?m loving|"
        r"am digging|'?m digging|am feeling|'?m feeling|'?m vibing with|am vibing with|'?m into|am into) "
        r"(?:this|that) (?:song|track|tune|jam|beat|banger|record)(?: (?:so much|a lot|right here|ava))?|"
        r"(?:this|that) (?:song|track|tune|one|jam) (?:is|was|'s) (?:so )?(?:fire|awesome|great|amazing|a banger|a bop|my jam|"
        r"so good|dope|sick|lit|a vibe|beautiful|perfect|excellent|the best|incredible|amazing|a classic|a masterpiece)|"
        r"(?:this|that) (?:song|track|tune) (?:slaps|bangs|goes hard|rocks|hits|hits different|is a bop)|"
        r"(?:favorite|favourite|heart|like|thumbs up|star|upvote) (?:this|that)(?: (?:song|track|one))?|"
        r"thumbs up(?: (?:on|to) this(?: song)?)?|(?:remember|note|mark|know) (?:that )?i (?:love|like) this(?: song| track)?|"
        r"(?:add|mark) (?:this|that)(?: song)? (?:as|to) (?:a |my )?(?:favou?rite|favou?rites|liked songs)").fullmatch(t)
    if love_song or (session_live and _rx(r"(?:i )?(?:really |absolutely |just |totally |so )?(?:love|like|dig|"
                                                  r"adore|'?m loving|am loving|'?m feeling|am feeling) (?:this|this one|it)"
                                                  r"(?: one)?(?: (?:so much|a lot))?|this (?:one )?(?:slaps|bangs|hits|"
                                                  r"goes hard|rocks|is fire)|banger").fullmatch(t)):
        return {"op": "love"}

    # ---- what's in the library
    m = (_rx(r"(?:what|which) (?:albums|records|cds)(?: (?:by|from) (?P<a>.+?))? (?:do i have|have i got|do we have|"
                      r"have we got|are (?:in|on) (?:my|the) (?:library|collection|music|computer|pc)|did i (?:get|download)"
                      r"|(?:do|does) my (?:library|collection) have)(?: (?:by|from) (?P<b>.+?))?").fullmatch(t)
         or _rx(r"(?:what|which) (?P<c>.+?)(?:'s|s')? (?:albums|records|cds) (?:do i have|have i got|do we have|"
                         r"have we got|are (?:in|on) (?:my|the) (?:library|collection|music))").fullmatch(t)
         or _rx(r"(?:list|name|tell me|read me|show me|read off|give me)(?: all)?(?: of)?(?: the)? (?:albums|records) "
                         r"(?:by|from) (?P<d>.+?)(?: (?:that )?i have| in my (?:library|collection))?").fullmatch(t)
         or _rx(r"(?:list|name|tell me|read me|show me|read off|give me)(?: all)?(?: of)? (?P<e>.+?)(?:'s|s')? "
                         r"(?:albums|records)(?: (?:that )?i have)?").fullmatch(t)
         or _rx(r"do i have (?:any |all )?(?:of )?(?:the )?(?:albums|records|cds) (?:by|from) (?P<f>.+)").fullmatch(t)
         or _rx(r"how many (?:albums|records|cds) (?:do i have|have i got|are there) (?:by|from) (?P<g>.+)").fullmatch(t)
         or _rx(r"how many (?P<h>.+?) (?:albums|records|cds) (?:do i have|have i got|are there)").fullmatch(t))
    if m:
        who = next((v for v in m.groupdict().values() if v), "")
        if who and not _rx(r"(?:new|old|good|the|my|any|some|other)").fullmatch(who.strip()):
            return {"op": "albums_by", "artist": who.strip()}
    m = (_rx(r"how many (?:songs|tracks|tunes) (?:do i have|have i got|are there|is there|do we have) (?:by|from) "
                      r"(?P<a>.+)").fullmatch(t)
         or _rx(r"how many (?P<b>.+?) (?:songs|tracks|tunes) (?:do i have|have i got|are there|do we have)").fullmatch(t)
         or _rx(r"do i have (?:any )?(?:songs|music|tracks|stuff|anything) (?:by|from) (?P<c>.+)").fullmatch(t)
         or _rx(r"(?:is|are) (?:there )?(?:any )?(?P<d>.+?) (?:songs|tracks|albums) (?:on|in) (?:my|the) "
                         r"(?:library|collection|music|computer|pc|drive)").fullmatch(t))
    if m:
        who = next((v for v in m.groupdict().values() if v), "")
        if who and not _rx(r"(?:new|old|good|the|my|any|some|other|music)").fullmatch(who.strip()):
            return {"op": "count_by", "artist": who.strip()}
    if _rx(r"how (?:many|much) (?:songs|music|tracks|tunes) (?:do i have|is there|are there|have i got)"
                    r"(?: (?:in|on) (?:my|the) (?:music )?(?:library|collection|computer|drive))?|how big is my music"
                    r"(?: library| collection)?|(?:is|has) (?:my |the )?music (?:library |index |collection )?(?:done|"
                    r"finished|ready|indexed|built|scanned)(?: (?:indexing|building|scanning|yet))?|how(?:'s| is) "
                    r"(?:the |my )?music (?:index|indexing|scan|library scan|library)(?: (?:going|coming(?: along)?|"
                    r"doing|coming))?|music (?:index|library) status|(?:are you|have you) (?:done|finished) "
                    r"(?:indexing|scanning|reading) my music").fullmatch(t):
        return {"op": "status"}
    if _rx(r"(?:re-?index|re-?scan|rescan|reindex|re index|re scan|refresh|update|index|scan)(?: (?:my|the))? "
                    r"music(?: library| collection| folder| index)?(?: (?:again|now))?").fullmatch(t):
        return {"op": "reindex"}

    # ---- make a playlist
    m = _rx(r"(?:make|create|build|put together|start|generate|whip up|throw together|set up|do|get|cook up|"
                     r"come up with|assemble)(?: me| us)? (?:a |an |some |one )?(?:new |quick |little |good |nice |fresh )?"
                     + _PL + r"(?: (?:called|named|titled) (?P<name>[\w'&$ -]{1,40}?))?"
                     r"(?: (?:with|of|from|for|full of|that has|containing|using|out of|made of|made from|based on)"
                     r" (?P<x>.+))?").fullmatch(t)
    if m:
        x = (m.group("x") or "").strip()
        if x and _SECOND.search(" " + x):
            return None
        name = (m.group("name") or "").strip()
        if name:
            return {"op": "create", "name": name, "text": x}
        if x:
            return {"op": "play", "text": x, "playlist": True}
        return {"op": "play", "text": "", "playlist": True}
    m = _rx(r"(?:make|create|build|put together|start|generate|whip up|throw together|get|cook up|do|"
                     r"give)(?: me| us)? (?:a |an |some |one )?(?P<x>.{2,80}?) " + _PL +
                     r"(?: (?:called|named|titled) (?P<name>[\w'&$ -]{1,40}))?").fullmatch(t)
    if m and not _rx(r"(?:new|my|the|this|that|good|nice|quick|little|fresh)").fullmatch(m.group("x")) and \
            not _SECOND.search(" " + m.group("x")):
        if m.group("name"):
            return {"op": "create", "name": m.group("name").strip(), "text": m.group("x").strip()}
        return {"op": "play", "text": m.group("x").strip(), "playlist": True}

    # ---- play something with some shape to it
    m = _rx(_PLAYV + r" (?P<x>.{1,160})").fullmatch(t)
    if m:
        verb = t[:m.start("x")].strip()
        x = m.group("x").strip()
        if _SECOND.search(" " + x) or _NOT_PLAYABLE.search(x):
            return None                              # ("bump the bedroom light up", "throw on a timer", "the newest
                                                     # bluey episode on the tv": not music)
        if _rx(r"\s(?:on|in|from|using|with) (?:youtube|you tube|spotify|netflix|hulu|pandora|soundcloud|the tv|tv|"
                     r"jellyfin|plex|vlc|chrome|the browser|apple music|amazon music|tidal)$").search(x):
            return None
        if verb == "shuffle" and _MODE_OBJ.fullmatch(x):
            return None                              # "shuffle the playlist" / "shuffle on": the player's mode
        if verb in ("turn on", "fire up", "pull up", "bring up", "crank", "crank up") and \
                not _rx(r"\b(?:playlist|music|songs|album|hits|tunes|jams)\b").search(x):
            return None                              # ("turn on the 80s lights"?) only with music named
        sp = spec.parse(x)
        if sp.song_by:
            return None                              # "billie jean by michael jackson": the song search
        if music_shaped(sp, x) or (verb == "shuffle" and _shuffle_names(sp, x)):
            return {"op": "play", "text": x, "shuffle": True if verb == "shuffle" else sp.shuffle}
    return None


# things people shuffle that aren't music
_NOT_SHUFFLED = re.compile(r"\b(?:cards?|deck|decks|papers?|files?|feet|chairs?|tiles?|dominoes|pieces|board|lineup|"
                           r"teams?|players|seats|desks|windows?|tabs|icons|photos?|pictures|slides|questions|"
                           r"flashcards|names|order|things|stuff|everything|it|them)\b")


def _shuffle_names(sp, x: str) -> bool:
    """'shuffle eminem' / 'shuffle road trip' (his playlist) — never 'shuffle the cards'."""
    if sp.saved or playlists.exists(x):
        return True
    return bool(sp.names) and not _rx(r"(?:the|a|an|my|your|his|her|our|their|these|those|this|that|some|all)\b"
                                      ).match(x) and not _NOT_SHUFFLED.search(x)


_ERA_WORDS = re.compile(r"(?:^|\s)(?:the )?(?:(?:early|mid|late) )?(?:'?\d0'?s|(?:19|20)\d0'?s|twenties|thirties|forties|"
                        r"fifties|sixties|seventies|eighties|nineties|two thousands|aughts|oldies|throwbacks?|newer|"
                        r"newest|new stuff|recent|latest|old school|this year|last year)(?:\s|$)")


# a mood said on its own that can only mean music ("play something chill" needs no list; "play chill" too)
_BARE_MOOD = re.compile(r"(?:(?:really|very|super|kinda|more|some) )?(?:chill|chilled|chilled out|chill out|relaxing|"
                        r"mellow|calming|soothing|upbeat|energetic|high energy|hype|pump up|pumped up|sad|sadder|"
                        r"depressing|melancholy|angry|aggressive|romantic|sexy|feel good|feel-good|chill vibes|"
                        r"good vibes|sad vibes|workout|gym|study|studying|sleep|bedtime|party|road trip|dinner)")


def music_shaped(sp, x: str) -> bool:
    """A request only the music library answers ('some 80s rock', 'songs by eminem', 'the album adhd'). Plain names
    ('eminem and logic', 'halloween 1978', 'tom and jerry') stay with the library-wide search, which plays an artist,
    several artists, a song, an album or a movie — whichever those words name."""
    if sp.moods and not (sp.genres or sp.years or sp.music_noun or sp.count or sp.minutes or sp.names or sp.featuring
                         or sp.like or sp.saved or sp.playlist_word or sp.not_genres) \
            and not _rx(r"\b(?:some|something|anything|stuff|vibes?|music|songs?|tunes|jams|playlist|for|to|while)\b"
                        ).search(x) and not _BARE_MOOD.fullmatch(spec._clean(x)):
        return False                                 # "play work" / "play happy": a song title, not a mood
    if (sp.genres or sp.moods or sp.featuring or sp.like or sp.best_of or sp.newest or sp.album or sp.album_pick
            or sp.albums or sp.discography or sp.saved or sp.playlist_word or sp.not_genres or sp.minutes
            or sp.pronoun or sp.not_names or sp.not_years):
        return True
    if sp.music_noun and (sp.names or sp.years or sp.count):
        return True
    if sp.years and (not sp.names) and (_ERA_WORDS.search(" " + x + " ") or sp.music_noun):
        return True
    if sp.years and sp.names and _ERA_WORDS.search(" " + x + " "):
        return True                                  # "90s eminem" (a bare year + a name may be a movie)
    return False


def claims(t: str, session_live: bool = False) -> bool:
    return parse(t, session_live) is not None
