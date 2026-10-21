"""Finding artists and albums in the index (fuzzy, for speech-to-text) and picking + ordering tracks for a spec."""

from __future__ import annotations

import os
import random
import re
import time

from . import index, vocab


_RXC: dict = {}


def _rx(p: str):
    """A compiled pattern from this module's own cache (never Python's shared 512-entry re cache, which the
    router's phrasebook already fills)."""
    r = _RXC.get(p)
    if r is None:
        r = _RXC[p] = re.compile(p)
    return r


# album names that are collections, not studio albums (their songs are someone's hits)
COMPILATION = re.compile(r"\b(?:greatest hits|best of|the best|hits|essential|essentials|collection|gold|anthology|"
                         r"number ones|#1'?s|no\.? ?1'?s|singles|platinum|definitive|ultimate|very best|retrospective|"
                         r"icon|20th century masters|playlist|the hits|top hits|classics)\b", re.I)
EDITION = re.compile(r"\s*[\(\[][^\)\]]*\b(?:deluxe|remaster(?:ed)?|expanded|anniversary|special|bonus|edition|"
                     r"version|reissue|explicit|clean|mono|stereo|super|platinum edition|complete)\b[^\)\]]*[\)\]]"
                     r"|\s+-\s+(?:deluxe|remaster(?:ed)?|expanded|anniversary|special)\b.*$"
                     r"|\s+\b(?:deluxe|remastered|expanded)(?: edition| version)?$", re.I)
_EDITION_SAID = re.compile(r"\b(?:deluxe|remaster(?:ed)?|expanded|anniversary|special|bonus|edition|reissue)\b", re.I)
_JUNK_TITLE = re.compile(r"\b(?:skit|interlude|intro|outro|commentary|interview|voicemail|voice ?mail|prelude|"
                         r"announcement|shout ?out)\b", re.I)
_VERSION = vocab.VERSION_RX
POOL_MAX = 15000         # a mood / genre list ranks at most this many songs (a random sample of a bigger pool)


def _sk(r: dict) -> str:
    """The song key of a row (stored by the index; worked out for a row from elsewhere)."""
    k = r.get("title_key")
    if k is None:
        k = r["title_key"] = vocab.song_key(r.get("title") or "")
    return k
_ALT_TAKE = re.compile(r"\b(?:live|remix|rmx|instrumental|a cappella|acapella|karaoke|demo|acoustic|clean|radio edit|"
                       r"sped up|slowed|reverb|nightcore|8d|cover)\b", re.I)

RNG = random.Random()
_cache: dict = {}


def _cached(name: str, build, ttl: float = 300.0):
    ver = index.changed()
    hit = _cache.get(name)
    if hit and hit[0] == ver and time.time() - hit[1] < ttl:
        return hit[2]
    val = build()
    _cache[name] = (ver, time.time(), val)
    return val


def clear_cache() -> None:
    _cache.clear()


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur]


# ---------------------------------------------------------------- artists
def artist_names(con, folder_names=()) -> dict[str, str]:
    """{match key: shown name} — every main artist in the index plus the artist folders on disk."""
    def build():
        out: dict[str, str] = {}
        for r in con.execute("SELECT main_key, main_artist, COUNT(*) n FROM tracks WHERE main_key != '' "
                             "GROUP BY main_key ORDER BY n DESC"):
            out.setdefault(r["main_key"], r["main_artist"])
        return out
    names = dict(_cached("artists", build))
    for f in folder_names:
        k = vocab.akey(f)
        if k and k not in names:
            names[k] = _rx(r"^[^\w(]+").sub("", f) or f
    return names


_PK: dict = {}


def _pk_map(names: dict) -> dict:
    """{sound-alike key: [match keys]} for these names (cached while the name list is the same)."""
    sig = (len(names), hash(tuple(sorted(names)[:50])), hash(tuple(sorted(names)[-50:])))
    if _PK.get("sig") != sig:
        pk: dict = {}
        for k in names:
            pk.setdefault(vocab.pkey(k), []).append(k)
        _PK.clear()
        _PK.update(sig=sig, map=pk)
    return _PK["map"]


def find_artist(con, said: str, folder_names=(), cutoff: float = 0.86) -> tuple[str, str, float] | None:
    """('Joyner Lucas', 'joyner lucas', score) for what was said ('joiner lucus'), else None."""
    said = (said or "").strip()
    if not said:
        return None
    names = artist_names(con, folder_names)
    tries = [said]
    base = vocab.possessive_base(said)
    if base != said:
        tries.append(base)
    if _rx(r"[a-z]s$").search(said) and not said.endswith("ss"):
        tries.append(said[:-1])                          # "eminems" (speech-to-text drops the apostrophe)
    alias = vocab.NAME_ALIASES.get(said.lower()) or vocab.NAME_ALIASES.get(vocab.akey(said))
    if alias:
        tries.insert(0, alias)
    best = None
    for t in tries:
        k = vocab.akey(t)
        if k in names:
            return names[k], k, 1.0 if t == said or t == alias else 0.98
    pk = _pk_map(names)
    for t in tries:
        p = vocab.pkey(t)
        if len(p.replace(" ", "")) >= 3 and p in pk:
            k = sorted(pk[p], key=len)[0]
            return names[k], k, 0.95
    from difflib import get_close_matches

    for t in tries:
        k0 = vocab.akey(t)
        for k in get_close_matches(k0, list(names), n=6, cutoff=0.7):
            s = vocab.name_score(t, names[k])
            if not best or s > best[2]:
                best = (names[k], k, s)
    if best and best[2] >= cutoff:
        return best
    return None


def artist_tracks(con, key: str) -> list[dict]:
    return rows(con.execute("SELECT * FROM tracks WHERE main_key=? OR primary_key=?", (key, key)))


# ---------------------------------------------------------------- albums
def base_album(name: str) -> str:
    return vocab.akey(EDITION.sub("", name or ""))


def albums(con) -> list[dict]:
    return _album_index(con)["all"]


def _album_index(con) -> dict:
    """Every album once, plus lookups by its plain name / edition-less name / sound-alike name (cached)."""
    def build():
        out, by_base, by_p = [], {}, {}
        for r in con.execute("SELECT main_key, main_artist, album, album_key, MIN(year) y, COUNT(*) n FROM tracks "
                             "WHERE album_key != '' GROUP BY main_key, album_key"):
            d = dict(r)
            d["base"] = base_album(d["album"])
            d["edition"] = bool(EDITION.search(d["album"] or ""))
            out.append(d)
            by_base.setdefault(d["base"], []).append(d)
            by_p.setdefault(vocab.pkey(d["base"]), []).append(d)
        return {"all": out, "base": by_base, "p": by_p}
    return _cached("albums", build)


def find_album(con, said: str, artist_key: str | None = None, cutoff: float = 0.84) -> dict | None:
    """The album meant ('adhd' -> ADHD, not ADHD (Deluxe) unless he said deluxe). Within an artist when given."""
    from difflib import SequenceMatcher, get_close_matches

    want = vocab.akey(said)
    wbase = base_album(said)
    if not want:
        return None
    edition = bool(_EDITION_SAID.search(said or ""))
    ix = _album_index(con)
    if artist_key:
        cands = [a for a in ix["all"] if a["main_key"] == artist_key]
    else:
        # the whole library: only names that could be it (same name, sounds alike, or close spelling)
        cands = list(ix["base"].get(wbase, [])) + list(ix["p"].get(vocab.pkey(wbase), []))
        for b in get_close_matches(wbase, list(ix["base"]), n=6, cutoff=cutoff):
            cands += ix["base"][b]
        cands += [a for a in ix["base"].get(want, [])]
    scored = []
    seen = set()
    for a in cands:
        if id(a) in seen:
            continue
        seen.add(id(a))
        if a["album_key"] == want:
            s = 1.0
        elif a["base"] == wbase:
            s = 0.97
        elif vocab.pkey(a["base"]) == vocab.pkey(wbase) and len(wbase) >= 3:
            s = 0.93
        else:
            if abs(len(a["base"]) - len(wbase)) > max(6, len(wbase)):
                continue
            s = SequenceMatcher(None, wbase, a["base"]).ratio() * 0.95
        if s >= cutoff:
            # the plain edition unless he named one; then the one with more tracks
            pref = (0 if (a["edition"] == edition) else 1, -a["n"])
            scored.append((-s, pref, a))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2]


def album_tracks(con, artist_key: str, album_key: str) -> list[dict]:
    rs = rows(con.execute("SELECT * FROM tracks WHERE main_key=? AND album_key=?", (artist_key, album_key)))
    return order_album(rs)


def order_album(rs: list[dict]) -> list[dict]:
    return sorted(rs, key=lambda r: (r.get("disc") or 1, r.get("track") or 999, os.path.basename(r["path"]).lower()))


def artist_albums(rs: list[dict], studio_only: bool = False) -> list[tuple[str, int | None, list[dict]]]:
    """[(album, year, tracks in order)] oldest first (unknown years last)."""
    by: dict[str, list[dict]] = {}
    for r in rs:
        if not r.get("album_key"):
            continue
        by.setdefault(r["album_key"], []).append(r)
    out = []
    for _k, tr in by.items():
        name = tr[0]["album"]
        if studio_only and COMPILATION.search(name or ""):
            continue
        years = [t["year"] for t in tr if t.get("year")]
        out.append((name, min(years) if years else None, order_album(tr)))
    out.sort(key=lambda a: (a[1] is None, a[1] or 0, (a[0] or "").lower()))
    return out


# ---------------------------------------------------------------- feedback + mood labels
def feedback(con) -> dict[str, dict]:
    return {r["path"]: dict(r) for r in con.execute("SELECT * FROM feedback")}


def mood_labels(con, mood: str) -> dict[str, float]:
    return {r["path"]: float(r["fit"]) for r in con.execute("SELECT path, fit FROM moods WHERE mood=?", (mood,))}


# ---------------------------------------------------------------- the pool for a spec
def _like_any(col: str, keys) -> tuple[str, list]:
    keys = list(keys)
    return "(" + " OR ".join(f"{col} LIKE ?" for _ in keys) + ")", [f"%|{k}|%" for k in keys]


def artist_genres(con) -> dict[str, dict[str, int]]:
    """{artist key: {genre: songs tagged with it}} — an untagged song counts as its artist's genres."""
    def build():
        out: dict[str, dict[str, int]] = {}
        for r in con.execute("SELECT main_key, genre_keys, COUNT(*) n FROM tracks WHERE genre_keys != '' "
                             "GROUP BY main_key, genre_keys"):
            d = out.setdefault(r["main_key"], {})
            for g in r["genre_keys"].strip("|").split("|"):
                if g:
                    d[g] = d.get(g, 0) + int(r["n"])
        return out
    return _cached("artist_genres", build)


def track_genres(r: dict, ag: dict | None = None) -> set[str]:
    g = {x for x in (r.get("genre_keys") or "").strip("|").split("|") if x}
    if not g and ag is not None:
        d = ag.get(r.get("main_key") or "", {})
        if d:
            top = max(d.values())
            g = {k for k, v in d.items() if v >= max(1, top * 0.3)}
    return g


def candidates(con, sp, artist_keys=None) -> list[dict]:
    """Every track that fits the spec's hard filters (artists, genres, years, exclusions)."""
    where, args = [], []
    if artist_keys:
        ks = list(artist_keys)
        where.append("(main_key IN (%s) OR primary_key IN (%s))" % (",".join("?" * len(ks)), ",".join("?" * len(ks))))
        args += ks + ks
    if sp.years:
        where.append("(" + " OR ".join("(year BETWEEN ? AND ?)" for _ in sp.years) + ")")
        for lo, hi in sp.years:
            args += [lo, hi]
    for lo, hi in sp.not_years:
        where.append("(year IS NULL OR year NOT BETWEEN ? AND ?)")
        args += [lo, hi]
    # the genres asked for (or a mood's genres) narrow it in the database; an untagged song goes by its artist's genres
    gl = set(sp.genres)
    if not gl and sp.moods and not artist_keys:
        for m in sp.moods:
            gl |= set((vocab.MOODS.get(m) or {}).get("genres") or ())
        if any(not (vocab.MOODS.get(m) or {}).get("genres") for m in sp.moods):
            gl = set()                                   # (a mood with no genres of its own: the whole library)
    if gl:
        likes, la = _like_any("genre_keys", sorted(gl))
        where.append(f"({likes[1:-1]} OR genre_keys = '' OR genre_keys IS NULL)")
        args += la
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    if artist_keys or sp.best_of:
        rs = rows(con.execute("SELECT * FROM tracks" + cond, args))
    else:
        # (a 100-song list from a 250,000-song library: a random sample of the rows that fit is plenty)
        ids = [r[0] for r in con.execute("SELECT rowid FROM tracks" + cond, args)]
        if len(ids) > POOL_MAX * 2:
            ids = RNG.sample(ids, POOL_MAX * 2)
        rs = []
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            rs += rows(con.execute("SELECT * FROM tracks WHERE rowid IN (%s)" % ",".join("?" * len(chunk)), chunk))
    ag = artist_genres(con) if (sp.genres or sp.not_genres or sp.moods) else None
    out = []
    want = set(sp.genres)
    nope = set(sp.not_genres)
    for r in rs:
        g = track_genres(r, ag) if ag is not None else set()
        if want and not (g & want):
            continue
        if nope and (g & nope):
            continue
        if not want and not artist_keys and (g & vocab.NOT_MUSIC):
            continue                                     # no comedy / kids / christmas unless asked for
        r["_genres"] = g
        out.append(r)
    if sp.not_names:
        bad = {vocab.akey(n) for n in sp.not_names}
        out = [r for r in out if r.get("main_key") not in bad and r.get("primary_key") not in bad]
    return out


# ---------------------------------------------------------------- scoring + picking
_MOOD_WORDS: dict = {}


def _mood_words(mood: str):
    if mood not in _MOOD_WORDS:
        ws = [vocab.akey(w) for w in (vocab.MOODS.get(mood) or {}).get("words") or ()]
        ws = [w for w in ws if w]
        _MOOD_WORDS[mood] = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in sorted(ws, key=len, reverse=True))
                                       + r")(?![a-z0-9])") if ws else None
    return _MOOD_WORDS[mood]


def mood_score(r: dict, mood: str, labels: dict[str, float]) -> float:
    d = vocab.MOODS.get(mood) or {}
    g = r.get("_genres") or set()
    s = 0.0
    mg = set(d.get("genres") or ())
    if g & mg:
        s += 1.0 + 0.25 * min(2, len(g & mg) - 1)
    if g & set(d.get("avoid") or ()):
        s -= 2.5
    bpm = r.get("bpm")
    rng = d.get("bpm")
    if bpm and rng:
        s += 1.0 if rng[0] <= bpm <= rng[1] else -1.0
    wrx = _mood_words(mood)
    if wrx is not None and wrx.search(_sk(r)):
        s += 0.8
    if r["path"] in labels:
        s += 2.5 * labels[r["path"]]                     # the brain's (or his) judgment, cached per song
    return s


def popularity(rs: list[dict]) -> dict[tuple, float]:
    """(artist key, title key) -> how 'known' a song is in this library: on several albums, on a hits collection."""
    seen: dict[tuple, set] = {}
    comp: dict[tuple, bool] = {}
    for r in rs:
        tk = _sk(r)
        if not tk:
            continue
        key = (r.get("main_key") or "", tk)
        seen.setdefault(key, set()).add(r.get("album_key") or r["path"])
        if COMPILATION.search(r.get("album") or ""):
            comp[key] = True
    return {k: 2.0 * len(v) + (3.0 if comp.get(k) else 0.0) for k, v in seen.items()}


def best_version(group: list[dict]) -> dict:
    """Of the same song on several albums: the original studio take (not live / remix / the hits collection)."""
    def rank(r):
        alt = bool(_ALT_TAKE.search((r.get("title") or "") + " " + (r.get("album") or "")))
        comp = bool(COMPILATION.search(r.get("album") or ""))
        return (alt, comp, r.get("year") or 9999, len(r["path"]))
    return sorted(group, key=rank)[0]


def hits(rs: list[dict], fb: dict, n: int = 20, known_titles=()) -> list[dict]:
    """An artist's best-known songs: the brain's picks when it gave some, then what this library says is popular."""
    pop = popularity(rs)
    known = {vocab.song_key(t) for t in known_titles or ()}
    groups: dict[tuple, list[dict]] = {}
    for r in rs:
        if _JUNK_TITLE.search(r.get("title") or ""):
            continue
        tk = _sk(r)
        if tk:
            groups.setdefault((r.get("main_key") or "", tk), []).append(r)
    scored = []
    for key, grp in groups.items():
        f = [fb.get(x["path"]) or {} for x in grp]
        loves = sum(int(x.get("loves") or 0) for x in f)
        skips = sum(int(x.get("skips") or 0) for x in f)
        if any(int(x.get("banned") or 0) for x in f):
            continue
        s = pop.get(key, 0.0) + 2.0 * loves - 1.0 * skips + (8.0 if key[1] in known else 0.0)
        scored.append((s, RNG.random(), best_version(grp)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [r for _s, _x, r in scored[:n]]


def pick(rs: list[dict], sp, fb: dict, labels_by_mood: dict | None = None, n: int | None = None,
         minutes: float | None = None, cap_per_artist: int | None = None) -> list[dict]:
    """Rank the pool for the spec (moods, feedback, a little chance) and take n songs (or `minutes` of them)."""
    labels_by_mood = labels_by_mood or {}
    scored = []
    seen_titles = set()
    pop = popularity(rs) if sp.best_of else {}
    if len(rs) > POOL_MAX:
        # a big pool: rank a random sample (plus every song he or the brain has an opinion on) — a 100-song list
        # never needs all 250,000 scored
        known = set(fb)
        for lab in labels_by_mood.values():
            known |= set(lab)
        rs = [r for r in rs if r["path"] in known] + RNG.sample(rs, POOL_MAX)
    for r in rs:
        f = fb.get(r["path"]) or {}
        if int(f.get("banned") or 0):
            continue
        if _JUNK_TITLE.search(r.get("title") or "") or (r.get("duration") and r["duration"] < 40):
            continue
        tk = (r.get("main_key"), _sk(r))
        if tk in seen_titles:
            continue                                      # the same song twice (album + hits collection)
        seen_titles.add(tk)
        s = 0.0
        for m in sp.moods:
            s += mood_score(r, m, labels_by_mood.get(m, {}))
        s += 1.0 * int(f.get("loves") or 0) - 0.7 * int(f.get("skips") or 0)
        if sp.best_of:
            s += pop.get(tk, 0.0)
        # a different mix every time — smaller for a mood, so chance never outweighs what makes a song fit it
        s += RNG.random() * (0.6 if sp.moods else 1.5)
        scored.append((s, r))
    if sp.moods:
        scored = [x for x in scored if x[0] > -1.0] or scored
    scored.sort(key=lambda x: -x[0])
    out, per = [], {}
    total = 0.0
    want = n if n else (None if minutes else 100)
    for s, r in scored:
        a = r.get("main_key") or ""
        if cap_per_artist and per.get(a, 0) >= cap_per_artist:
            continue
        out.append(r)
        per[a] = per.get(a, 0) + 1
        total += float(r.get("duration") or 210.0)
        if want and len(out) >= want:
            break
        if minutes and total >= minutes * 60.0:
            break
    return out


def similar_artists(con, key: str, n: int = 10) -> list[str]:
    """Artists who share the most genre with this one (their specific genres count most)."""
    ag = artist_genres(con)
    mine = ag.get(key) or {}
    if not mine:
        return []
    tot = sum(mine.values()) or 1
    weight = {g: v / tot for g, v in mine.items()}
    # a sub-genre ("trap") says more than a family ("hip hop")
    for g in list(weight):
        if not vocab.PARENTS.get(g):
            weight[g] *= 0.5
    out = []
    for k, d in ag.items():
        if k == key or not d:
            continue
        t = sum(d.values()) or 1
        s = sum(weight.get(g, 0.0) * (v / t) for g, v in d.items())
        if s > 0:
            out.append((s, k))
    out.sort(key=lambda x: -x[0])
    return [k for _s, k in out[:n]]
