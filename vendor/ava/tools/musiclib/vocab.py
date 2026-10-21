"""Music vocabulary: genres (as tagged and as said), moods, eras, and keys for fuzzy name matching.

Genres: every tag or spoken genre maps to one CANONICAL key ("Hip-Hop/Rap" -> "hip hop", "trap" -> "trap"); a sub-genre
also counts as its families ("trap" is "hip hop" too), so "hip hop" finds trap songs and "hip hop but no trap" leaves
them out. Moods aren't in tags: a mood is hints (genres it lives in, a tempo range, title words) that narrow and rank
the pool; labels the brain gives a song are cached and learned from.
"""

from __future__ import annotations

import datetime
import re
import unicodedata


_RXC: dict = {}


def _rx(p: str):
    """A compiled pattern from this module's own cache (never Python's shared 512-entry re cache, which the
    router's phrasebook already fills)."""
    r = _RXC.get(p)
    if r is None:
        r = _RXC[p] = re.compile(p)
    return r


# ---------------------------------------------------------------- genres
# canonical key -> (families it also counts as, aliases as tagged or said)
_GENRES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "hip hop": ((), ("hip hop", "hip-hop", "hiphop", "hip hop rap", "hip-hop rap", "rap hip hop", "rap", "rap music",
                     "hip hop music", "gangsta rap", "gangster rap", "east coast rap", "west coast rap",
                     "east coast hip hop", "west coast hip hop", "southern hip hop", "southern rap", "dirty south",
                     "conscious hip hop", "conscious rap", "underground hip hop", "underground rap",
                     "alternative hip hop", "hardcore hip hop", "hardcore rap", "boom bap", "horrorcore", "g funk",
                     "g-funk", "crunk", "hyphy", "emo rap", "cloud rap", "pop rap", "christian rap", "christian hip hop",
                     "uk hip hop", "grime", "jazz rap", "abstract hip hop", "political hip hop", "comedy rap",
                     "midwest rap", "rap & hip-hop", "rap and hip hop", "hip hop and rap", "rap/hip hop", "urban rap",
                     "mumble rap")),
    "trap": (("hip hop",), ("trap", "trap music", "trap rap", "rage")),
    "drill": (("hip hop",), ("drill", "uk drill", "chicago drill", "brooklyn drill", "ny drill")),
    "old school hip hop": (("hip hop",), ("old school hip hop", "old school rap", "golden age hip hop",
                                          "oldschool hip hop", "old skool hip hop", "classic hip hop", "classic rap",
                                          "90s hip hop", "90s rap")),
    "phonk": (("hip hop",), ("phonk", "drift phonk")),
    "r&b": ((), ("r&b", "rnb", "r and b", "r n b", "r'n'b", "r & b", "rhythm and blues", "rhythm & blues",
                 "contemporary r&b", "contemporary r and b", "urban contemporary", "new jack swing", "alternative r&b",
                 "alternative r and b", "r&b soul", "r and b soul", "slow jams", "quiet storm")),
    "soul": ((), ("soul", "soul music", "northern soul", "southern soul", "classic soul", "blue eyed soul",
                  "philly soul", "psychedelic soul")),
    "neo soul": (("soul", "r&b"), ("neo soul", "neo-soul")),
    "motown": (("soul", "oldies"), ("motown",)),
    "funk": ((), ("funk", "p funk", "p-funk", "funk rock")),
    "disco": ((), ("disco", "nu disco", "nu-disco", "post disco", "post-disco")),
    "pop": ((), ("pop", "pop music", "dance pop", "dance-pop", "teen pop", "electropop", "power pop", "bubblegum",
                 "bubblegum pop", "adult contemporary", "top 40", "top forty", "chart", "chart pop", "europop",
                 "art pop", "baroque pop", "sophisti pop", "pop ballad", "pop ballads", "ballad", "ballads",
                 "vocal pop", "traditional pop")),
    "k-pop": (("pop",), ("k-pop", "kpop", "k pop", "korean pop")),
    "j-pop": (("pop",), ("j-pop", "jpop", "j pop", "japanese pop", "anime")),
    "latin pop": (("latin", "pop"), ("latin pop",)),
    "rock": ((), ("rock", "rock music", "rock and roll", "rock n roll", "rock 'n' roll", "rock'n'roll", "rock & roll",
                  "rocknroll", "blues rock", "garage rock", "rockabilly", "glam rock", "stoner rock",
                  "progressive rock", "prog rock", "prog", "psychedelic rock", "psychedelic", "art rock",
                  "arena rock", "aor", "heartland rock", "surf rock", "surf", "christian rock", "instrumental rock",
                  "symphonic rock", "space rock", "jam band", "math rock", "post rock", "post-rock", "krautrock",
                  "british invasion", "rock en espanol")),
    "classic rock": (("rock",), ("classic rock", "classic rock music")),
    "hard rock": (("rock",), ("hard rock",)),
    "soft rock": (("rock",), ("soft rock", "yacht rock", "adult rock", "soft rock ballads")),
    "southern rock": (("rock",), ("southern rock",)),
    "pop rock": (("pop", "rock"), ("pop rock", "pop/rock", "pop-rock")),
    "folk rock": (("folk", "rock"), ("folk rock", "folk-rock")),
    "country rock": (("country", "rock"), ("country rock",)),
    "rap rock": (("rock", "hip hop"), ("rap rock", "rap metal", "rapcore")),
    "alternative": (("rock",), ("alternative", "alternative rock", "alt rock", "alt-rock", "alt", "modern rock",
                                "post grunge", "post-grunge", "britpop", "brit pop", "college rock",
                                "alternative pop", "alt pop", "dream pop", "shoegaze")),
    "indie": ((), ("indie", "indie music", "lo-fi indie", "bedroom pop")),
    "indie rock": (("indie", "rock", "alternative"), ("indie rock", "indie-rock", "garage rock revival")),
    "indie pop": (("indie", "pop"), ("indie pop", "indie-pop", "indietronica")),
    "indie folk": (("indie", "folk"), ("indie folk", "indie-folk")),
    "grunge": (("rock", "alternative"), ("grunge", "seattle sound")),
    "punk": (("rock",), ("punk", "punk rock", "hardcore punk", "skate punk", "post punk", "post-punk", "pop punk",
                         "pop-punk", "ska punk", "street punk", "celtic punk", "oi")),
    "emo": (("rock",), ("emo", "emocore", "screamo", "midwest emo")),
    "metal": ((), ("metal", "heavy metal", "thrash", "thrash metal", "death metal", "black metal", "doom metal",
                   "nu metal", "nu-metal", "metalcore", "deathcore", "power metal", "speed metal",
                   "progressive metal", "symphonic metal", "industrial metal", "groove metal", "sludge metal",
                   "melodic death metal", "djent", "post metal", "folk metal", "gothic metal", "hardcore",
                   "metal music")),
    "hair metal": (("metal", "hard rock", "rock"), ("hair metal", "glam metal", "glam rock metal", "pop metal")),
    "alternative metal": (("metal", "alternative"), ("alternative metal", "alt metal")),
    "industrial": ((), ("industrial", "industrial rock", "ebm")),
    "country": ((), ("country", "country music", "contemporary country", "honky tonk", "honky-tonk", "red dirt",
                     "bro country", "texas country", "country & western", "country and western", "western",
                     "alt country", "alt-country", "nashville", "country ballad", "new country", "neo traditional",
                     "neotraditional country", "western swing", "cowboy")),
    "outlaw country": (("country",), ("outlaw country", "outlaw")),
    "classic country": (("country",), ("classic country", "traditional country", "old country")),
    "country pop": (("country", "pop"), ("country pop", "country-pop")),
    "americana": (("country", "folk"), ("americana", "roots", "roots rock")),
    "bluegrass": (("country", "folk"), ("bluegrass", "newgrass")),
    "folk": ((), ("folk", "folk music", "contemporary folk", "singer songwriter", "singer-songwriter",
                  "singer/songwriter", "traditional folk", "acoustic folk", "celtic", "irish folk", "sea shanties",
                  "shanties", "anti-folk")),
    "blues": ((), ("blues", "delta blues", "chicago blues", "electric blues", "texas blues", "modern blues",
                   "acoustic blues", "soul blues")),
    "jazz": ((), ("jazz", "jazz music", "cool jazz", "jazz fusion", "fusion", "vocal jazz", "acid jazz",
                  "latin jazz", "dixieland", "free jazz", "hard bop", "modal jazz", "contemporary jazz", "jazz funk",
                  "gypsy jazz", "jazz vocal", "crossover jazz", "nu jazz")),
    "smooth jazz": (("jazz",), ("smooth jazz",)),
    "big band": (("jazz",), ("big band", "swing", "big band swing", "swing music")),
    "bebop": (("jazz",), ("bebop", "bop")),
    "classical": ((), ("classical", "classical music", "orchestral", "orchestra", "symphony", "symphonic", "baroque",
                       "chamber music", "chamber", "romantic era", "string quartet", "concerto", "choral",
                       "modern classical", "neoclassical", "neo-classical", "contemporary classical", "early music",
                       "renaissance", "minimalism", "piano solo", "solo piano", "classical piano", "classical crossover")),
    "opera": (("classical",), ("opera", "operetta", "aria")),
    "electronic": ((), ("electronic", "electronica", "electronic music", "edm", "electro", "big room", "future bass",
                        "hardstyle", "idm", "breakbeat", "breaks", "eurodance", "club", "club music", "electro pop",
                        "electronic dance music", "bass music", "big beat", "glitch", "electroclash", "uk garage",
                        "garage", "jungle", "footwork", "moombahton", "hardcore techno", "gabber", "edm music")),
    "dance": (("electronic",), ("dance", "dance music", "dance/electronic", "dance & electronic",
                                "dance and electronic", "electronic/dance")),
    "house": (("electronic",), ("house", "house music", "deep house", "tech house", "progressive house",
                                "electro house", "tropical house", "future house", "chicago house", "acid house",
                                "afro house")),
    "techno": (("electronic",), ("techno", "minimal techno", "detroit techno", "acid techno")),
    "trance": (("electronic",), ("trance", "psytrance", "psy trance", "progressive trance", "uplifting trance",
                                 "vocal trance", "goa")),
    "dubstep": (("electronic",), ("dubstep", "brostep", "riddim")),
    "drum and bass": (("electronic",), ("drum and bass", "drum & bass", "drum n bass", "drum'n'bass", "dnb", "d&b",
                                        "liquid funk", "neurofunk")),
    "synthwave": (("electronic",), ("synthwave", "retrowave", "outrun", "vaporwave", "darksynth")),
    "downtempo": (("electronic",), ("downtempo", "chillout", "chill out", "chill-out", "trip hop", "trip-hop",
                                    "chillwave", "lounge", "nu lounge")),
    "ambient": ((), ("ambient", "ambient music", "new age", "meditation", "drone", "relaxation", "nature sounds",
                     "spa", "healing", "sleep music", "white noise", "rain sounds", "binaural")),
    "lofi": ((), ("lofi", "lo-fi", "lo fi", "lofi hip hop", "lo-fi hip hop", "lo fi hip hop", "chillhop",
                  "chill hop", "jazzhop", "jazz hop", "lofi beats", "lo-fi beats", "study beats")),
    "reggae": ((), ("reggae", "roots reggae", "dub", "rocksteady", "reggae fusion", "lovers rock", "ska")),
    "dancehall": (("reggae",), ("dancehall",)),
    "reggaeton": (("latin",), ("reggaeton", "reguetón", "reggaetón", "regueton", "dembow", "latin urbano",
                               "urbano latino", "urbano", "latin trap")),
    "latin": ((), ("latin", "latin music", "latino", "salsa", "bachata", "merengue", "cumbia", "regional mexican",
                   "mariachi", "ranchera", "banda", "norteño", "norteno", "corridos", "corridos tumbados", "tejano",
                   "bossa nova", "samba", "tango", "flamenco", "latin rock", "musica mexicana", "música mexicana",
                   "musica latina", "música latina", "vallenato", "son cubano", "bolero", "latin jazz",
                   "latin alternative", "tropical", "mpb")),
    "afrobeats": ((), ("afrobeats", "afrobeat", "afropop", "afro pop", "highlife", "amapiano", "afro")),
    "gospel": ((), ("gospel", "gospel music", "christian", "christian music", "contemporary christian", "ccm",
                    "worship", "praise and worship", "praise & worship", "praise", "hymns", "hymn", "spiritual",
                    "spirituals", "religious", "inspirational", "southern gospel", "christian contemporary",
                    "christian & gospel", "christian and gospel")),
    "soundtrack": ((), ("soundtrack", "soundtracks", "ost", "original soundtrack", "score", "film score",
                        "movie soundtrack", "movie soundtracks", "film soundtrack", "original score", "musical",
                        "musicals", "show tunes", "showtunes", "broadway", "cast recording", "video game",
                        "video game music", "game music", "game soundtrack", "tv soundtrack", "film scores",
                        "soundtracks & scores", "stage & screen")),
    "oldies": ((), ("oldies", "oldie", "doo wop", "doo-wop", "standards", "vocal standards", "crooner", "crooners",
                    "nostalgia", "50s", "60s", "early rock and roll", "rock n roll oldies", "golden oldies",
                    "traditional vocal")),
    "easy listening": ((), ("easy listening", "beautiful music", "mood music", "exotica", "light music",
                            "elevator music", "background music")),
    "new wave": (("rock", "pop"), ("new wave", "new romantic", "post punk revival")),
    "synthpop": (("new wave", "pop", "electronic"), ("synthpop", "synth-pop", "synth pop", "electropop 80s")),
    "children": ((), ("children's", "childrens", "children's music", "childrens music", "children", "kids",
                      "kids music", "kid's music", "nursery rhymes", "lullaby", "lullabies", "kids songs",
                      "children's songs", "family")),
    "holiday": ((), ("christmas", "christmas music", "xmas", "holiday", "holiday music", "holidays",
                     "christmas songs", "christmas carols", "carols", "halloween")),
    "comedy": ((), ("comedy", "spoken word", "stand-up", "stand up", "standup", "stand-up comedy", "audiobook",
                    "audiobooks", "audio book", "podcast", "podcasts", "speech", "spoken", "talk", "radio show",
                    "skit", "skits", "parody", "novelty")),
    "world": ((), ("world", "world music", "international", "bollywood", "indian", "african", "celtic music",
                   "polka", "zydeco", "cajun", "hawaiian", "klezmer", "arabic", "middle eastern", "flamenco pop",
                   "fado", "chanson", "french pop", "c-pop", "cantopop", "mandopop")),
    "instrumental": ((), ("instrumental", "instrumentals", "instrumental music", "beats", "type beat")),
}

# genres kept out of mood / decade / "artists like" playlists unless asked for by name
NOT_MUSIC = frozenset({"comedy", "children", "holiday"})

PARENTS: dict[str, tuple[str, ...]] = {k: v[0] for k, v in _GENRES.items()}
_ALIAS: dict[str, str] = {}
for _k, (_p, _al) in _GENRES.items():
    _ALIAS.setdefault(_k, _k)
    for _a in _al:
        _ALIAS.setdefault(_a.lower(), _k)


def _families(key: str) -> list[str]:
    out, todo = [], [key]
    while todo:
        k = todo.pop(0)
        if k in out:
            continue
        out.append(k)
        todo.extend(PARENTS.get(k, ()))
    return out


def _gclean(s: str) -> str:
    s = strip_accents((s or "").lower())
    s = s.replace("’", "'").replace("_", " ")
    s = _rx(r"\s+").sub(" ", s).strip(" .-'\"")
    return s


# aliases by length, longest first, for finding genre words inside a longer tag ("alternative hip hop")
_ALIAS_RX = re.compile(r"(?<![\w&'])(" + "|".join(re.escape(a) for a in sorted(_ALIAS, key=len, reverse=True))
                       + r")(?![\w&'])")


def tag_genres(tag: str) -> list[str]:
    """A genre TAG ("Hip-Hop/Rap", "Alternative Rock; Indie", "R&B/Soul") -> canonical keys incl. families."""
    keys: list[str] = []
    for piece in _rx(r"\s*[;|,/\\\x00]\s*").split(tag or ""):
        p = _gclean(piece)
        if not p or p.isdigit():
            continue
        k = _ALIAS.get(p)
        if k is None:
            # "Rap & Hip-Hop" / "Pop/Rock": try the whole thing with & -> and, then the genre words inside it
            k = _ALIAS.get(p.replace(" & ", " and "))
        if k is not None:
            found = [k]
        else:
            found = []
            for m in _ALIAS_RX.finditer(p):
                found.append(_ALIAS[m.group(1)])
        for f in found:
            for fam in _families(f):
                if fam not in keys:
                    keys.append(fam)
    return keys


def genre_field(tag: str) -> str:
    """How the index stores a tag's genres: '|hip hop|trap|' (empty when none) — searchable with LIKE '%|trap|%'."""
    ks = tag_genres(tag)
    return ("|" + "|".join(ks) + "|") if ks else ""


def genre_name(key: str) -> str:
    return {"r&b": "R and B", "k-pop": "K-pop", "j-pop": "J-pop", "edm": "EDM", "lofi": "lo-fi",
            "hip hop": "hip hop"}.get(key, key)


# the genre words she listens for in a REQUEST — the tag aliases minus ones that are everyday words or that name a
# mood/era instead ("chill", "beats", "kids", "christian" stays: "christian music")
_SPOKEN_SKIP = {"alt", "prog", "surf", "chart", "ballad", "ballads", "western", "cowboy", "roots", "family", "kids",
                "children", "talk", "speech", "spoken", "chamber", "praise", "club", "garage", "breaks", "dub",
                "bop", "outlaw", "urbano", "afro", "tropical", "rage", "score", "beats", "type beat", "fusion",
                "spa", "healing", "drone", "relaxation", "meditation", "lounge", "skit", "skits", "nostalgia", "50s",
                "60s", "90s hip hop", "90s rap", "anime", "international", "arabic", "indian", "african", "celtic",
                "goa", "jungle", "oi", "hardcore", "symphonic", "psychedelic", "musical", "spiritual", "religious",
                "inspirational", "parody", "novelty", "halloween", "holiday", "holidays", "family", "standards",
                "crooner", "crooners", "background music", "mood music", "light music", "sleep music", "chillout",
                "chill out", "chill-out", "classic rap", "classic hip hop"}
SPOKEN_GENRES: dict[str, str] = {a: k for a, k in _ALIAS.items() if a not in _SPOKEN_SKIP and not a.isdigit()}
SPOKEN_GENRES.update({
    "hip hop": "hip hop", "hip-hop": "hip hop", "hiphop": "hip hop", "hip hop music": "hip hop", "rap": "hip hop",
    "rap songs": "hip hop", "r and b": "r&b", "r n b": "r&b", "rnb": "r&b", "r&b": "r&b", "r & b": "r&b",
    "rhythm and blues": "r&b", "edm": "electronic", "electronic": "electronic", "techno": "techno",
    "lo fi": "lofi", "lo-fi": "lofi", "lofi": "lofi", "low fi": "lofi", "low-fi": "lofi", "lo fi hip hop": "lofi",
    "christmas": "holiday", "christmas music": "holiday", "christmas songs": "holiday", "xmas music": "holiday",
    "holiday music": "holiday", "kids music": "children", "kids songs": "children", "children's music": "children",
    "nursery rhymes": "children", "lullabies": "children", "classic rock": "classic rock", "classical": "classical",
    "classical music": "classical", "country": "country", "country music": "country", "christian music": "gospel",
    "christian": "gospel", "worship music": "gospel", "worship": "gospel", "praise and worship": "gospel",
    "gospel": "gospel", "hymns": "gospel", "old school hip hop": "old school hip hop",
    "old school rap": "old school hip hop", "oldschool rap": "old school hip hop", "boom bap": "old school hip hop",
    "big band": "big band", "swing": "big band", "swing music": "big band", "motown": "motown", "doo wop": "oldies",
    "show tunes": "soundtrack", "broadway": "soundtrack", "movie soundtracks": "soundtrack",
    "soundtracks": "soundtrack", "film scores": "soundtrack", "video game music": "soundtrack",
    "sea shanties": "folk", "shanties": "folk", "slow jams": "r&b", "power ballads": "hair metal",
    "hair metal": "hair metal", "yacht rock": "soft rock", "elevator music": "easy listening",
    "easy listening": "easy listening", "trip hop": "downtempo", "drum and bass": "drum and bass",
    "dancehall": "dancehall", "reggaeton": "reggaeton", "afrobeats": "afrobeats", "afrobeat": "afrobeats",
    "k-pop": "k-pop", "kpop": "k-pop", "k pop": "k-pop", "j-pop": "j-pop", "jpop": "j-pop", "j pop": "j-pop",
    "new wave": "new wave", "synth pop": "synthpop", "synthpop": "synthpop", "synthwave": "synthwave",
    "dance music": "dance", "dance": "dance", "house music": "house", "house": "house", "dubstep": "dubstep",
    "trance": "trance", "outlaw country": "outlaw country", "classic country": "classic country",
    "old country": "classic country", "bluegrass": "bluegrass", "americana": "americana",
    "smooth jazz": "smooth jazz", "neo soul": "neo soul", "soul": "soul", "funk": "funk", "disco": "disco",
    "punk": "punk", "pop punk": "punk", "emo": "emo", "grunge": "grunge", "metal": "metal", "heavy metal": "metal",
    "nu metal": "metal", "death metal": "metal", "thrash": "metal", "thrash metal": "metal", "metalcore": "metal",
    "hard rock": "hard rock", "soft rock": "soft rock", "southern rock": "southern rock", "alternative": "alternative",
    "alternative rock": "alternative", "alt rock": "alternative", "indie": "indie", "indie rock": "indie rock",
    "indie pop": "indie pop", "indie folk": "indie folk", "folk": "folk", "blues": "blues", "jazz": "jazz",
    "opera": "opera", "latin": "latin", "latin music": "latin", "salsa": "latin", "bachata": "latin",
    "mariachi": "latin", "cumbia": "latin", "reggae": "reggae", "ska": "reggae", "trap": "trap", "drill": "drill",
    "phonk": "phonk", "pop": "pop", "pop music": "pop", "rock": "rock", "rock and roll": "rock",
    "rock n roll": "rock", "rock 'n' roll": "rock", "ambient": "ambient", "new age": "ambient", "oldies": "oldies",
    "instrumental": "instrumental", "instrumentals": "instrumental", "world music": "world", "polka": "world",
    "zydeco": "world", "bollywood": "world", "comedy": "comedy", "stand up": "comedy", "stand-up": "comedy",
    "chillhop": "lofi", "lofi beats": "lofi", "lo fi beats": "lofi", "study beats": "lofi", "britpop": "alternative",
    "shoegaze": "alternative", "gangsta rap": "hip hop", "conscious rap": "hip hop", "underground rap": "hip hop",
    "emo rap": "hip hop", "uk drill": "drill", "grime": "hip hop", "crunk": "hip hop", "g funk": "hip hop",
    "progressive rock": "rock", "prog rock": "rock", "psychedelic rock": "rock", "rockabilly": "rock",
    "garage rock": "rock", "glam rock": "rock", "surf rock": "rock", "blues rock": "rock", "folk rock": "folk rock",
    "country rock": "country rock", "pop rock": "pop rock", "rap rock": "rap rock", "rap metal": "rap rock",
    "deep house": "house", "tech house": "house", "electro": "electronic", "electronica": "electronic",
    "downtempo": "downtempo", "chillwave": "downtempo", "vaporwave": "synthwave", "retrowave": "synthwave",
})
_SPOKEN_RX = re.compile(r"(?<![\w'&])(" + "|".join(re.escape(a) for a in sorted(SPOKEN_GENRES, key=len, reverse=True))
                        + r")(?![\w'&])")


def find_spoken_genres(text: str) -> list[tuple[int, int, str]]:
    """[(start, end, canonical key)] of genre words in a request, longest first, not overlapping."""
    out = []
    for m in _SPOKEN_RX.finditer(text or ""):
        out.append((m.start(1), m.end(1), SPOKEN_GENRES[m.group(1)]))
    return out


# ---------------------------------------------------------------- moods
# key -> how it's said, the genres it lives in, genres it never is, a tempo range, title words that hint at it
MOODS: dict[str, dict] = {
    "sad": {"say": ("sad", "sadder", "saddest", "depressing", "depressed", "heartbreak", "heartbroken", "breakup",
                    "break up", "crying", "cry", "melancholy", "melancholic", "emotional", "in my feelings", "gloomy",
                    "lonely", "somber", "sombre", "down", "blue", "bummed", "moody", "rainy day", "sad boy",
                    "sad girl", "heart broken", "tearjerker", "tear jerker"),
            "genres": ("r&b", "soul", "folk", "indie", "alternative", "country", "blues", "soft rock", "emo",
                       "singer songwriter", "hip hop", "pop"),
            "avoid": ("electronic", "dance", "house", "techno", "trance", "disco", "funk", "children", "holiday",
                      "comedy", "metal", "reggaeton", "latin"),
            "bpm": (50, 100),
            "words": ("cry", "crying", "tears", "alone", "lonely", "goodbye", "sorry", "hurt", "miss", "missing",
                      "broken", "heart", "rain", "gone", "lost", "pain", "sad", "blue", "without you", "last",
                      "remember", "letter", "hate me", "leave", "left", "empty", "cold", "ghost", "funeral", "die",
                      "dead", "suicide", "demons", "scars", "wish")},
    "happy": {"say": ("happy", "happier", "feel good", "feel-good", "feelgood", "good mood", "cheerful", "cheery",
                      "uplifting", "good vibes", "positive", "sunny", "joyful", "fun", "carefree", "sunshine",
                      "summer", "summery", "bright", "upbeat", "up beat", "cleaning", "cleaning the house",
                      "clean the house", "chores", "doing chores", "housework", "house work", "the shower", "shower",
                      "the beach", "beach", "the pool", "pool", "getting ready", "morning"),
              "genres": ("pop", "funk", "disco", "soul", "reggae", "motown", "dance", "latin", "afrobeats",
                         "country pop", "pop rock", "new wave"),
              "avoid": ("metal", "emo", "drill", "comedy", "ambient", "blues"),
              "bpm": (98, 140),
              "words": ("happy", "sun", "sunshine", "good", "smile", "dance", "fun", "summer", "shine", "love",
                        "celebrate", "party", "joy", "beautiful", "best day", "feeling", "walking on")},
    "workout": {"say": ("workout", "work out", "working out", "gym", "lifting", "lift", "pump up", "pump-up",
                        "pumped up", "pumped", "hype", "hyped", "hype up", "energetic", "high energy", "energy",
                        "intense", "beast mode", "motivation", "motivational", "running", "run", "cardio",
                        "leg day", "training", "fast", "aggressive workout", "get hyped", "banger", "bangers"),
                "genres": ("hip hop", "trap", "electronic", "dance", "house", "dubstep", "drum and bass", "rock",
                           "metal", "hard rock", "punk", "pop", "rap rock", "phonk", "drill"),
                "avoid": ("ambient", "classical", "lofi", "smooth jazz", "easy listening", "children", "holiday",
                          "comedy", "folk", "blues", "soft rock", "oldies"),
                "bpm": (115, 185),
                "words": ("power", "run", "fight", "fire", "go", "up", "strong", "stronger", "beast", "lose yourself",
                          "jump", "hard", "rise", "till i collapse", "eye of the tiger", "wild", "energy", "lit",
                          "turn up", "war", "unstoppable", "champion", "remember the name", "monster")},
    "chill": {"say": ("chill", "chilled", "chilled out", "chill out", "chillout", "relax", "relaxing", "relaxed",
                      "mellow", "calm", "calming", "laid back", "laid-back", "laidback", "easy going", "easygoing",
                      "soothing", "smooth", "peaceful", "unwind", "unwinding", "low key", "lowkey", "cozy", "cosy",
                      "background", "soft", "gentle", "quiet", "lazy", "sunday morning", "vibes", "vibey",
                      "chill vibes", "bath", "reading", "rainy"),
              "genres": ("lofi", "ambient", "jazz", "smooth jazz", "soul", "neo soul", "r&b", "indie", "indie folk",
                         "folk", "downtempo", "soft rock", "acoustic", "reggae", "easy listening", "bossa nova"),
              "avoid": ("metal", "punk", "hardcore", "drill", "trap", "dubstep", "hard rock", "comedy", "children",
                        "phonk", "drum and bass"),
              "bpm": (60, 108),
              "words": ("slow", "easy", "sunday", "dream", "float", "breathe", "calm", "rain", "moon", "night",
                        "coffee", "sleep", "chill", "smooth", "lazy", "ocean", "waves", "breeze")},
    "focus": {"say": ("study", "studying", "focus", "focused", "focusing", "concentrate", "concentration",
                      "concentrating", "homework", "work", "working", "productive", "productivity", "deep work",
                      "coding", "programming", "thinking", "deep focus", "brain"),
              "genres": ("lofi", "ambient", "classical", "soundtrack", "downtempo", "jazz", "instrumental",
                         "electronic", "synthwave", "post rock"),
              "avoid": ("metal", "punk", "hip hop", "trap", "drill", "comedy", "children", "holiday", "country",
                        "reggaeton", "dance"),
              "bpm": (60, 115),
              "words": ("instrumental", "piano", "theme", "study", "focus", "interlude", "prelude", "nocturne",
                        "suite", "sonata", "etude", "concerto", "symphony", "lofi", "beat")},
    "sleep": {"say": ("sleep", "sleepy", "sleeping", "bedtime", "bed time", "fall asleep", "go to sleep", "nap",
                      "napping", "lullaby", "night time", "nighttime", "dreamy", "insomnia", "wind down"),
              "genres": ("ambient", "classical", "lofi", "downtempo", "easy listening", "folk", "smooth jazz"),
              "avoid": ("metal", "punk", "hip hop", "trap", "drill", "rock", "dance", "electronic", "house",
                        "comedy", "country", "reggaeton", "funk", "disco"),
              "bpm": (40, 90),
              "words": ("sleep", "dream", "night", "moon", "lullaby", "slow", "rain", "quiet", "stars", "nocturne",
                        "gentle", "soft", "calm", "ocean")},
    "angry": {"say": ("angry", "anger", "mad", "pissed", "pissed off", "rage", "raging", "furious", "aggressive",
                      "aggro", "hardcore", "heavy", "hard hitting", "hard-hitting", "violent", "intense", "dark",
                      "evil", "grimy", "gritty", "diss", "diss tracks", "beef"),
              "genres": ("metal", "hard rock", "punk", "hip hop", "trap", "drill", "rap rock", "industrial",
                         "alternative metal", "phonk", "grunge", "dubstep"),
              "avoid": ("easy listening", "soft rock", "ambient", "lofi", "smooth jazz", "children", "holiday",
                        "comedy", "classical", "folk", "country pop", "disco"),
              "bpm": (85, 200),
              "words": ("hate", "kill", "rage", "war", "fight", "mad", "angry", "enemy", "enemies", "revenge",
                        "blood", "die", "dead", "hell", "devil", "burn", "break", "smash", "fuck", "shit", "diss",
                        "beef", "violence", "demon", "scream", "bodies", "riot", "chaos", "venom", "killshot",
                        "takeover", "hit em up", "ether", "control", "the warning", "anger", "not afraid")},
    "romantic": {"say": ("romantic", "romance", "love songs", "love song", "date night", "date", "sexy", "sensual",
                         "intimate", "baby making", "make out", "makeout", "slow dance", "slow dancing", "candlelight",
                         "valentine", "valentines", "valentine's", "anniversary", "wedding", "in love", "lovey",
                         "sweet", "bedroom"),
                 "genres": ("r&b", "soul", "neo soul", "jazz", "smooth jazz", "oldies", "soft rock", "pop",
                            "country", "latin", "motown", "big band", "easy listening"),
                 "avoid": ("metal", "punk", "drill", "trap", "comedy", "children", "dubstep", "industrial", "phonk"),
                 "bpm": (55, 105),
                 "words": ("love", "baby", "heart", "kiss", "tonight", "forever", "you", "my girl", "beautiful",
                           "together", "honey", "darling", "hold", "touch", "lady", "angel", "wonderful", "lover",
                           "romance", "only you", "all of me", "perfect", "marry", "sweet")},
    "party": {"say": ("party", "partying", "dance party", "dancing", "dance", "club", "clubbing", "turn up",
                      "turnt", "lit", "pregame", "pre game", "house party", "get the party started", "bbq",
                      "barbecue", "cookout", "pool party", "celebration", "celebrate"),
              "genres": ("pop", "dance", "electronic", "house", "hip hop", "trap", "latin", "reggaeton", "disco",
                         "funk", "afrobeats", "dancehall", "r&b"),
              "avoid": ("ambient", "classical", "folk", "blues", "soft rock", "comedy", "children", "lofi",
                        "smooth jazz", "easy listening", "opera"),
              "bpm": (100, 140),
              "words": ("party", "dance", "club", "night", "shake", "move", "bounce", "turn up", "drink", "shots",
                        "celebrate", "tonight", "floor", "jump", "get low", "hands up", "boogie", "groove")},
    "road trip": {"say": ("road trip", "roadtrip", "driving", "drive", "car ride", "cruising", "cruise", "highway",
                          "long drive", "commute", "commuting", "travel", "traveling", "travelling"),
                  "genres": ("rock", "classic rock", "pop", "country", "hip hop", "alternative", "pop rock",
                             "southern rock", "indie", "soft rock", "r&b"),
                  "avoid": ("ambient", "classical", "comedy", "children", "opera", "lofi"),
                  "bpm": (90, 140),
                  "words": ("road", "drive", "highway", "ride", "car", "free", "fast", "wheels", "go", "home",
                            "running", "wind", "sun", "summer", "miles", "journey", "route", "life is a highway")},
    "dinner": {"say": ("dinner", "dinner party", "cooking", "cook", "brunch", "breakfast", "coffee shop",
                       "coffeehouse", "restaurant", "cocktail", "cocktails", "wine", "sunday dinner", "lunch"),
               "genres": ("jazz", "smooth jazz", "soul", "oldies", "big band", "easy listening", "latin",
                          "bossa nova", "r&b", "folk", "indie folk", "classical", "motown"),
               "avoid": ("metal", "punk", "drill", "trap", "comedy", "children", "dubstep", "hard rock", "phonk"),
               "bpm": (60, 120),
               "words": ("moon", "night", "wonderful", "love", "fly", "summer", "dream", "cheek", "coffee",
                         "wine", "dinner", "table", "kitchen")},
    "nostalgic": {"say": ("nostalgic", "nostalgia", "memories", "memory lane", "old times", "back in the day",
                          "childhood"),
                  "genres": (), "avoid": ("comedy",), "bpm": None,
                  "words": ("remember", "memories", "back", "yesterday", "old", "days", "young", "time")},
}
# mood key -> it's really a tempo / era / genre ask: none (all handled above)


def _mood_rx():
    pairs = []
    for k, d in MOODS.items():
        for w in d["say"]:
            pairs.append((w, k))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return re.compile(r"(?<![\w'])(" + "|".join(re.escape(p[0]) for p in pairs) + r")(?![\w'])"), dict(pairs)


_MOOD_RX, _MOOD_OF = _mood_rx()


def find_moods(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(1), m.end(1), _MOOD_OF[m.group(1)]) for m in _MOOD_RX.finditer(text or "")]


# ---------------------------------------------------------------- eras
def this_year() -> int:
    return datetime.date.today().year


_DEC_WORD = {"twenties": 2, "thirties": 3, "forties": 4, "fifties": 5, "sixties": 6, "seventies": 7,
             "eighties": 8, "nineties": 9}
_EDGE = {"early": (0, 3), "mid": (3, 6), "middle": (3, 6), "late": (6, 9)}


def decade_start(word: str) -> int | None:
    """'80s' / "'80s" / '1980s' / 'eighties' / 'nineteen eighties' / '2000s' / 'two thousands' / '2010s' -> 1980 ..."""
    w = (word or "").lower().strip().replace("’", "'")
    w = _rx(r"^the ").sub("", w)
    w = _rx(r"^'").sub("", w)
    m = _rx(r"(19|20)(\d)0'?s").fullmatch(w)
    if m:
        return int(m.group(1) + m.group(2) + "0")
    m = _rx(r"(\d)0'?s").fullmatch(w)
    if m:
        d = int(m.group(1))
        return 2000 + d * 10 if d <= 2 else 1900 + d * 10     # "20s" = the 2020s, "10s" = 2010s, "00s" = 2000s
    m = _rx(r"(?:nineteen )?(twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties)").fullmatch(w)
    if m:
        d = _DEC_WORD[m.group(1)]
        if d == 2 and not w.startswith("nineteen"):
            return 2020
        return 1900 + d * 10
    if _rx(r"(?:two thousands|the two thousands|aughts|naughties|noughties|two thousand(?:'?s)?|00s|2000'?s)").fullmatch(w):
        return 2000
    if _rx(r"(?:twenty tens|twenty-tens|tens|teens|2010'?s|twenty tens)").fullmatch(w):
        return 2010
    if _rx(r"(?:twenty twenties|2020'?s)").fullmatch(w):
        return 2020
    return None


DECADE_WORD = (r"(?:(?:19|20)\d0'?s|'?\d0'?s|(?:nineteen )?(?:twenties|thirties|forties|fifties|sixties|seventies|"
               r"eighties|nineties)|two thousands|aughts|naughties|noughties|twenty tens|twenty twenties)")
_ERA_RX = re.compile(
    r"(?<![\w'])(?:(?P<edge>early|mid|middle|late)[\s-]+)?(?:the\s+)?(?P<dec>" + DECADE_WORD + r")(?![\w'])")
_SPAN_RX = re.compile(r"(?<!\w)(?:from|between)\s+(?P<a>(?:19|20)\d\d)\s+(?:to|and|through|thru|till|until|-)\s+"
                      r"(?P<b>(?:19|20)\d\d)(?!\w)|(?<!\w)(?P<c>(?:19|20)\d\d)\s*(?:-|to|through|thru)\s*"
                      r"(?P<d>(?:19|20)\d\d)(?!\w)")
_YEAR_RX = re.compile(r"(?<![\w$])(?P<y>(?:19[2-9]\d|20[0-4]\d))(?![\w'])")


def era_ranges(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """-> ([(lo, hi) year ranges said], [(start, end) spans of the words that said them])."""
    t = text or ""
    ranges: list[tuple[int, int]] = []
    spans: list[tuple[int, int]] = []
    for m in _SPAN_RX.finditer(t):
        a, b = (m.group("a"), m.group("b")) if m.group("a") else (m.group("c"), m.group("d"))
        lo, hi = sorted((int(a), int(b)))
        ranges.append((lo, hi))
        spans.append(m.span())
    for m in _ERA_RX.finditer(t):
        if any(s <= m.start() < e for s, e in spans):
            continue
        d = decade_start(m.group("dec"))
        if d is None:
            continue
        lo, hi = d, d + 9
        if m.group("edge"):
            a, b = _EDGE[m.group("edge")]
            lo, hi = d + a, d + b
        ranges.append((lo, hi))
        spans.append(m.span())
    for m in _YEAR_RX.finditer(t):
        if any(s <= m.start() < e for s, e in spans):
            continue
        y = int(m.group("y"))
        ranges.append((y, y))
        spans.append(m.span())
    now = this_year()
    for m in _rx(r"(?<![\w'])(?:(?P<old>oldies|golden oldies|old school|oldschool|old stuff|old songs|"
                         r"the old stuff|older (?:stuff|songs|music)|old music)|(?P<tb>throwbacks?|throw backs?|"
                         r"throwback (?:jams|songs|hits|music))|(?P<new>newer|newest|recent|latest|new stuff|new songs|"
                         r"new music|the new stuff|newer stuff|modern|current|brand new|fresh|today'?s|todays)|(?P<ty>this year'?s?|"
                         r"from this year|came out this year)|(?P<ly>last year'?s?|from last year))(?![\w'])").finditer(t):
        if any(s <= m.start() < e for s, e in spans):
            continue
        if m.group("old"):
            ranges.append((1900, 1979) if m.group("old") in ("oldies", "golden oldies") else (1900, now - 20))
        elif m.group("tb"):
            ranges.append((now - 35, now - 12))
        elif m.group("new"):
            ranges.append((now - 5, now))
        elif m.group("ty"):
            ranges.append((now, now))
        elif m.group("ly"):
            ranges.append((now - 1, now - 1))
        spans.append(m.span())
    return ranges, spans


def year_from_text(s: str) -> int | None:
    """A 4-digit year in a tag ('1997-05-13', '1997') or a folder name ('ADHD (2020)', '[1997] Album', '1997 - Album')."""
    m = _rx(r"(?<!\d)(19[0-9]\d|20[0-4]\d)(?!\d)").search(s or "")
    return int(m.group(1)) if m else None


def folder_year(name: str) -> tuple[str, int | None]:
    """'ADHD (2020)' -> ('ADHD', 2020); '[1997] OK Computer' -> ('OK Computer', 1997); '1997 - Album' -> ('Album', 1997)."""
    n = (name or "").strip()
    for rx in (r"\s*[\(\[]\s*((?:19|20)\d\d)\s*[\)\]]\s*$", r"^\s*[\(\[]\s*((?:19|20)\d\d)\s*[\)\]]\s*[-–.]?\s*",
               r"^\s*((?:19|20)\d\d)\s*[-–.]\s+", r"\s+[-–]\s*((?:19|20)\d\d)\s*$"):
        m = _rx(rx).search(n)
        if m:
            rest = (n[:m.start()] + n[m.end():]).strip(" -–.")
            if rest:
                return rest, int(m.group(1))
    return n, None


# ---------------------------------------------------------------- names (fuzzy matching for speech-to-text)
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


# known speech-to-text spellings of artists whose names aren't said the way they're written
NAME_ALIASES = {
    "the weekend": "the weeknd", "weekend": "the weeknd", "the week end": "the weeknd", "weeknd": "the weeknd",
    "m and m": "eminem", "m&m": "eminem", "m and ms": "eminem", "m&ms": "eminem", "eminem's": "eminem",
    "eminems": "eminem", "emanem": "eminem", "eminim": "eminem", "slim shady": "eminem",
    "jay z": "jay-z", "jayz": "jay-z", "jay-z": "jay-z", "acdc": "ac/dc", "ac dc": "ac/dc", "a c d c": "ac/dc",
    "guns and roses": "guns n' roses", "guns n roses": "guns n' roses", "two pac": "2pac", "tupac": "2pac",
    "too pac": "2pac", "biggie": "the notorious b.i.g.", "biggie smalls": "the notorious b.i.g.",
    "notorious big": "the notorious b.i.g.", "the notorious big": "the notorious b.i.g.",
    "j cole": "j. cole", "jcole": "j. cole", "a$ap rocky": "a$ap rocky", "asap rocky": "a$ap rocky",
    "nf": "nf", "n f": "nf", "logic": "logic", "kendrick": "kendrick lamar", "lil wayne": "lil wayne",
    "little wayne": "lil wayne", "lil baby": "lil baby", "little baby": "lil baby", "lil uzi": "lil uzi vert",
    "little uzi": "lil uzi vert", "lil nas": "lil nas x", "little nas x": "lil nas x", "dr dre": "dr. dre",
    "doctor dre": "dr. dre", "snoop": "snoop dogg", "snoop dog": "snoop dogg", "fifty cent": "50 cent",
    "50 cent": "50 cent", "pink": "p!nk", "p!nk": "p!nk", "ke$ha": "kesha", "beyonce": "beyoncé",
    "sza": "sza", "sizza": "sza", "ye": "kanye west", "kanye": "kanye west", "travis": "travis scott",
    "post malone": "post malone", "posty": "post malone", "mgk": "machine gun kelly", "nwa": "n.w.a",
    "n w a": "n.w.a", "run dmc": "run-dmc", "rundmc": "run-dmc", "outkast": "outkast", "the beatles": "the beatles",
    "zz top": "zz top", "u two": "u2", "you two": "u2", "blink 182": "blink-182", "blink one eighty two": "blink-182",
    "sum 41": "sum 41", "sum forty one": "sum 41", "matchbox 20": "matchbox twenty", "maroon five": "maroon 5",
    "three doors down": "3 doors down", "twenty one pilots": "twenty one pilots", "21 pilots": "twenty one pilots",
    "twenty one savage": "21 savage", "the chainsmokers": "the chainsmokers", "chainsmokers": "the chainsmokers",
}


def akey(s: str) -> str:
    """The plain match key: lowercase, no accents/punctuation, '&' -> 'and', no leading 'the'."""
    s = strip_accents((s or "").lower()).replace("’", "'")
    s = s.replace("&", " and ").replace("$", "s").replace("+", " and ")
    s = _rx(r"'s\b").sub("s", s)
    s = _rx(r"[^a-z0-9 ]+").sub(" ", s)
    s = _rx(r"\s+").sub(" ", s).strip()
    s = _rx(r"^the ").sub("", s)
    return s


def _pword(w: str) -> str:
    if w.isdigit():
        return w
    w = w.replace("ph", "f").replace("ck", "k").replace("gh", "g").replace("qu", "kw").replace("q", "k")
    w = w.replace("x", "ks").replace("z", "s").replace("w", "w")
    w = _rx(r"c(?=[eiy])").sub("s", w).replace("c", "k")
    w = _rx(r"(?<=.)y").sub("i", w)
    first, rest = w[:1], w[1:]
    rest = _rx(r"[aeiouy]").sub("", rest)
    rest = _rx(r"h(?![aeiou])").sub("", rest)
    out = first + rest
    out = _rx(r"(.)\1+").sub(r"\1", out)
    return out


def pkey(s: str) -> str:
    """A sound-alike key ('joiner lucus' == 'joyner lucas', 'the weekend' == 'the weeknd')."""
    return " ".join(_pword(w) for w in akey(s).split())


def name_score(said: str, name: str) -> float:
    """0..1 how well a spoken name matches a library name."""
    from difflib import SequenceMatcher

    a, b = akey(said), akey(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    al = NAME_ALIASES.get(a) or NAME_ALIASES.get((said or "").lower().strip())
    if al and akey(al) == b:
        return 0.99
    pa, pb = pkey(a), pkey(b)
    if pa == pb and len(pa.replace(" ", "")) >= 3:
        return 0.95
    r1 = SequenceMatcher(None, a, b).ratio()
    r2 = SequenceMatcher(None, pa, pb).ratio() if pa and pb else 0.0
    # the same words in another order ("lucas joyner") or with a word left out
    wa, wb = set(a.split()), set(b.split())
    jac = len(wa & wb) / max(1, len(wa | wb))
    return max(r1 * 0.97, r2 * 0.93, jac * 0.9 if len(wa) > 1 else 0.0)


# a title's version / feature tag: "(Live)", "(Remastered 2011)", "- Radio Edit", "(feat. Logic)"
VERSION_RX = re.compile(r"\s*[\(\[][^\)\]]*\b(?:live|remix|rmx|mix|edit|version|remaster(?:ed)?|explicit|clean|mono|"
                        r"stereo|demo|acoustic|instrumental|a cappella|acapella|radio|extended|feat\.?|ft\.?|featuring|"
                        r"with|bonus|single|original|re-?recorded|taylor'?s)\b[^\)\]]*[\)\]]|\s+-\s+(?:live|remix|"
                        r"remaster(?:ed)?|radio edit|edit|single version|acoustic|instrumental|demo)\b.*$", re.I)
# a guest in a TITLE: "(feat. Logic)", "[with X]", "... ft. X" — never a plain "with" ("Dancing with Myself")
TITLE_FEAT_RX = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]"
                           r"|\s+(?:feat\.?|ft\.?|featuring)\s+(.+)$", re.I)


def song_key(title: str) -> str:
    """The song itself, whatever version: 'Lose Yourself (Live)' / 'Stan (feat. Dido)' -> 'lose yourself' / 'stan'."""
    return akey(VERSION_RX.sub("", TITLE_FEAT_RX.sub("", title or "")))


def possessive_base(s: str) -> str:
    """"eminem's" / "eminems" -> 'eminem' (only the trailing possessive)."""
    s = (s or "").strip()
    s = _rx(r"'s$|s'$").sub("", s)
    return s
