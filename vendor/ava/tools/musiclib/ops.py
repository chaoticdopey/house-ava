"""What the router's music steps do: build a playlist for what was asked, save / edit / list playlists, learn from
"skip" and "I love this", answer "what albums do I have by Logic". Every function returns {"ok", "say", ...}.

Nothing here writes to your music folders: it reads the index (and, for an artist no pass has reached yet, that
artist's folder names), writes .m3u8 lists into her data folder and opens them in the player in use (player.py:
Winamp on the desktop).
"""

from __future__ import annotations

import os
import re
import threading
import time

from ...log import get
from . import index, picker, player, playlists, vocab
from . import spec as specmod


_RXC: dict = {}


def _rx(p: str):
    """A compiled pattern from this module's own cache (never Python's shared 512-entry re cache, which the
    router's phrasebook already fills)."""
    r = _RXC.get(p)
    if r is None:
        r = _RXC[p] = re.compile(p)
    return r


log = get()

MAX_LIST = 1000          # an artist's whole catalog, shuffled, is capped here
DEFAULT_N = 100          # a genre / mood / decade playlist
PER_ARTIST = 40          # songs per artist when several are asked for
BRAIN_WAIT = 12.0        # seconds a 'best of' waits for the brain before using the library's own popularity


def roots(cfg) -> list[str]:
    from .. import music

    return [r for r in music.folders(cfg) if os.path.isdir(r)]


def ensure_index(cfg) -> None:
    """Start (or carry on with) the background tag index — a no-op when it's running or fresh."""
    try:
        rs = roots(cfg)
        if rs:
            index.INDEXER.start(rs)
    except Exception as exc:
        log.debug("music index start failed: %s", exc)


class Lib:
    """One operation's view of the library: the index connection + the artist folders on disk."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.roots = roots(cfg)
        self.con = index.connect()
        self._folders: list[tuple[str, str]] | None = None

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def folders(self) -> list[tuple[str, str]]:
        """[(artist folder name, root)]."""
        if self._folders is None:
            from .. import music

            self._folders = [(n, r) for r in self.roots for n in music.artist_folders(r)]
        return self._folders

    def artist(self, said: str):
        """(shown name, key) of the artist meant, else None."""
        hit = picker.find_artist(self.con, said, [n for n, _r in self.folders()])
        return (hit[0], hit[1]) if hit else None

    def artist_rows(self, key: str) -> list[dict]:
        rs = picker.artist_tracks(self.con, key)
        if rs:
            return rs
        # no pass has reached this artist's folder yet: read it by its names now, and its tags next
        for name, root in self.folders():
            if vocab.akey(name) == key:
                path = os.path.join(root, name)
                rs = index.folder_rows(path, root)
                if rs:
                    try:                                    # (into the index too: albums / years work at once)
                        gen = int(index.meta_get(self.con, "gen", 0) or 0)
                        for r in rs:
                            r["gen"] = gen
                        index.insert_missing(self.con, rs)
                        self.con.commit()
                        index._bump()
                    except Exception as exc:
                        log.debug("folder rows not saved: %s", exc)
                index.INDEXER.prioritize(path, root)
                return rs
        return []

    def row_for(self, path: str) -> dict:
        r = self.con.execute("SELECT * FROM tracks WHERE path=?", (path,)).fetchone()
        if r:
            return dict(r)
        root = next((x for x in self.roots if path.lower().startswith(x.lower().rstrip("\\/") + os.sep)), "")
        return index.make_row(path, root or os.path.dirname(os.path.dirname(os.path.dirname(path))), 0, 0, None, 0)


# ---------------------------------------------------------------- what's playing
def current_path() -> str | None:
    """The song playing now: the player's current file (Winamp's), else the session's position in the list she
    started."""
    pl = player.get()
    p = None
    try:
        p = pl.current_file()
    except Exception:
        p = None
    sess = playlists.session()
    if not p:
        try:
            pos, n = pl.position()
        except Exception:
            pos, n = None, None
        paths = sess.get("paths") or []
        if pos is not None and paths and n == len(paths) and 0 <= pos < len(paths):
            p = paths[pos]
    if not p and sess.get("kind") == "song":
        p = sess.get("path")
    return p or None


def _title_of(r: dict) -> str:
    t = r.get("title") or os.path.splitext(os.path.basename(r["path"]))[0]
    a = r.get("main_artist") or r.get("artist") or ""
    return f"{t} by {a}" if a else t


def _list_words(items: list[str], more: int = 0) -> str:
    items = [i for i in items if i]
    if more > 0:
        return ", ".join(items) + f" and {more} more"
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------- building a list for a spec
class Unresolved(Exception):
    """The names said aren't an artist or an album: let the plain song / movie search try the words."""


def _filter(rows: list[dict], sp, ag) -> list[dict]:
    out = []
    want, nope = set(sp.genres), set(sp.not_genres)
    bad = {vocab.akey(n) for n in sp.not_names}
    for r in rows:
        y = r.get("year")
        if sp.years and not (y and any(lo <= y <= hi for lo, hi in sp.years)):
            continue
        if sp.not_years and y and any(lo <= y <= hi for lo, hi in sp.not_years):
            continue
        if want or nope:
            g = picker.track_genres(r, ag)
            if want and not (g & want):
                continue
            if nope and (g & nope):
                continue
            r["_genres"] = g
        elif sp.moods:
            r["_genres"] = picker.track_genres(r, ag)
        if bad and (r.get("main_key") in bad or r.get("primary_key") in bad):
            continue
        out.append(r)
    return out


def _labels(lib, sp) -> dict:
    return {m: picker.mood_labels(lib.con, m) for m in sp.moods}


def _brain_up(brain) -> bool:
    try:
        return bool(brain is not None and brain.status()[0])
    except Exception:
        return False


def brain_hits(lib, brain, artist: str, key: str, titles: list[str]) -> list[str]:
    """The artist's best-known songs, as the brain knows them (cached 30 days per artist). [] without the brain."""
    cached = index.meta_get(lib.con, "hits:" + key)
    if cached and time.time() - float(cached.get("ts", 0)) < 30 * 86400:
        return list(cached.get("titles") or [])
    if not titles or not _brain_up(brain):
        return []
    uniq = list(dict.fromkeys(t for t in titles if t))[:150]
    prompt = (f"These songs by {artist} are in a music library:\n" +
              "\n".join(f"{i + 1}. {t}" for i, t in enumerate(uniq)) +
              "\n\nWhich of them are this artist's best-known, most popular songs? Reply with ONLY the numbers of up "
              "to 20 of them, most popular first, separated by commas.")
    box: dict = {}

    def ask():
        try:
            box["out"] = brain.chat("You know popular music. Answer with numbers only.", prompt)
        except Exception as exc:
            box["err"] = exc
    th = threading.Thread(target=ask, daemon=True, name="music-hits")
    th.start()
    th.join(BRAIN_WAIT)
    out = box.get("out")
    if not out:
        return []
    picked = []
    for n in _rx(r"\d+").findall(out):
        i = int(n) - 1
        if 0 <= i < len(uniq) and uniq[i] not in picked:
            picked.append(uniq[i])
    if picked:
        try:
            index.meta_set(lib.con, "hits:" + key, {"ts": time.time(), "titles": picked})
            lib.con.commit()
        except Exception:
            pass
    return picked


def label_moods(con, brain, mood: str, rows: list[dict], limit: int = 40) -> int:
    """Ask the brain which of these songs fit the mood; cache the answer per song (the library learns moods). -> n."""
    todo = [r for r in rows if r.get("title")][:limit]
    if not todo or not _brain_up(brain):
        return 0
    have = {r["path"] for r in con.execute("SELECT path FROM moods WHERE mood=? AND src='brain'", (mood,))}
    todo = [r for r in todo if r["path"] not in have]
    if not todo:
        return 0
    lines = [f"{i + 1}. {r.get('main_artist') or r.get('artist') or '?'} - {r.get('title')}"
             f" ({(r.get('genre') or '').split(';')[0] or 'unknown genre'}, {r.get('year') or 'year?'})"
             for i, r in enumerate(todo)]
    try:
        out = brain.chat("You know popular music and the mood of songs. Answer with numbers only.",
                         f"Mood: {mood}.\nSongs:\n" + "\n".join(lines) +
                         f"\n\nWhich of these songs fit a '{mood}' mood? Reply with ONLY their numbers, comma-separated.")
    except Exception:
        return 0
    yes = {int(n) - 1 for n in _rx(r"\d+").findall(out or "")}
    now = time.time()
    con.executemany("INSERT OR REPLACE INTO moods(path, mood, fit, src, ts) VALUES(?,?,?,?,?)",
                    [(r["path"], mood, 1.0 if i in yes else -0.5, "brain", now) for i, r in enumerate(todo)])
    con.commit()
    return len(todo)


def _label_later(cfg, brain, sp, rows):
    if not sp.moods or not _brain_up(brain):
        return

    def run():
        try:
            con = index.connect()
            try:
                for m in sp.moods[:2]:
                    label_moods(con, brain, m, rows[:120])
            finally:
                con.close()
        except Exception as exc:
            log.debug("mood labels failed: %s", exc)
    threading.Thread(target=run, daemon=True, name="music-moods").start()


def _shuffle(rows):
    rows = list(rows)
    picker.RNG.shuffle(rows)
    return rows


def _pronoun_artist(lib) -> tuple[str, str] | None:
    p = current_path()
    if p:
        r = lib.row_for(p)
        if r.get("main_key"):
            return r["main_artist"], r["main_key"]
    sess = playlists.session()
    if sess.get("artist_key"):
        return sess.get("artist") or sess["artist_key"], sess["artist_key"]
    return None


def _album_split(lib, text: str):
    """'pink floyd dark side of the moon' / 'adele 25' -> the album, by trying the leading / trailing words as
    the artist."""
    words = text.split()
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        for who, alb in ((a, b), (b, a)):
            hit = lib.artist(who)
            if hit:
                al = picker.find_album(lib.con, alb, hit[1])
                if al:
                    return al
    return None


def build(lib, sp, brain=None) -> tuple[list[dict], str, str, dict]:
    """-> (tracks in play order, what it is in words, order 'shuffled'|'in order', session extras).
    Raises Unresolved when the names said aren't in the library (the router then tries the plain search)."""
    fb = picker.feedback(lib.con)
    # a saved playlist (or what its name means when there's no such list)
    if sp.saved:
        shown = playlists.find(sp.saved)
        if shown:
            paths = playlists.load(shown)
            rows = [lib.row_for(p) for p in paths]
            order = "shuffled" if sp.shuffle else "in order"
            return (_shuffle(rows) if sp.shuffle else rows), f"your {shown} playlist — {{songs}}", order, {"saved": shown}
        if sp.fallback is not None:
            fbk = sp.fallback
            if sp.shuffle is not None:
                fbk.shuffle = sp.shuffle
            rows, what, order, extra = build(lib, fbk, brain)
            return rows, what, order, dict(extra, missing=playlists.display(sp.saved))
        raise Unresolved(f"You don't have a playlist called {playlists.display(sp.saved)}.")

    if sp.pronoun:
        who = _pronoun_artist(lib)
        if not who:
            raise Unresolved("Whose? Nothing I started is playing — say the artist's name.")
        sp.names, sp.names_whole = [who[0]], who[0]

    # ---- albums
    if sp.album or sp.album_pick or sp.albums or sp.discography:
        if sp.albums:
            out, names = [], []
            for a in sp.albums:
                al = picker.find_album(lib.con, a) or _album_split(lib, a)
                if not al:
                    raise Unresolved(f"I couldn't find the album {a} in your music.")
                out += picker.album_tracks(lib.con, al["main_key"], al["album_key"])
                names.append(al["album"])
            return out, _list_words(names) + " — {songs}", "in order", {}
        if sp.album:
            key = None
            if sp.album_artist:
                hit = lib.artist(sp.album_artist)
                if not hit:
                    raise Unresolved(f"I couldn't find {sp.album_artist} in your music.")
                key = hit[1]
                lib.artist_rows(key)                      # (an artist no pass has reached: read its folder)
            al = picker.find_album(lib.con, sp.album, key)
            if not al and key:
                rows = lib.artist_rows(key)
                albs = {r["album_key"]: r for r in rows if r.get("album_key")}
                best = max(albs.values(), key=lambda r: vocab.name_score(sp.album, r["album"]), default=None)
                if best and vocab.name_score(sp.album, best["album"]) >= 0.8:
                    trs = picker.order_album([r for r in rows if r.get("album_key") == best["album_key"]])
                    return trs, f"{best['album']} by {best['main_artist']} — {{songs}}", "in order", {"artist_key": key}
            if not al and not key:
                al = _album_split(lib, sp.album)
            if not al:
                raise Unresolved(f"I couldn't find the album {sp.album} in your music.")
            trs = picker.album_tracks(lib.con, al["main_key"], al["album_key"])
            if sp.shuffle:
                trs = _shuffle(trs)
            return trs, f"{al['album']} by {al['main_artist']} — {{songs}}", "shuffled" if sp.shuffle else "in order", \
                {"artist_key": al["main_key"], "artist": al["main_artist"]}
        # an artist's albums: his first / newest / all of them in order
        hit = lib.artist(sp.names_whole or (sp.names[0] if sp.names else ""))
        if not hit:
            raise Unresolved(f"I couldn't find {sp.names_whole or 'that artist'} in your music.")
        rows = lib.artist_rows(hit[1])
        albs = picker.artist_albums(rows, studio_only=True) or picker.artist_albums(rows)
        if not albs:
            raise Unresolved(f"I don't see any albums by {hit[0]} — just loose songs.")
        if sp.discography:
            out = [t for _n, _y, trs in albs for t in trs]
            return out, f"all {len(albs)} of {hit[0]}'s albums — {{songs}}", "in order", {"artist_key": hit[1], "artist": hit[0]}
        dated = [a for a in albs if a[1]] or albs
        idx = {"first": 0, "second": 1, "third": 2}.get(sp.album_pick)
        a = dated[min(idx, len(dated) - 1)] if idx is not None else dated[-1]
        yr = f" ({a[1]})" if a[1] else ""
        return a[2], f"{a[0]}{yr} by {hit[0]} — {{songs}}", "in order", {"artist_key": hit[1], "artist": hit[0]}

    ag = picker.artist_genres(lib.con)
    # ---- artists like X
    if sp.like:
        hit = lib.artist(sp.like)
        if not hit:
            raise Unresolved(f"I couldn't find {sp.like} in your music.")
        sims = picker.similar_artists(lib.con, hit[1], 12)
        if not sims:
            raise Unresolved(f"I don't know {hit[0]}'s genre yet — their tags haven't been read.")
        out, names = [], []
        for k in sims:
            rs = _filter(lib.artist_rows(k), sp, ag)
            if not rs:
                continue
            got = picker.hits(rs, fb, n=6) if len(rs) > 6 else rs
            out += got
            names.append(got[0].get("main_artist") or k)
        if not out:
            raise Unresolved(f"I couldn't find artists like {hit[0]} in your music.")
        n = sp.count or len(out)
        out = _shuffle(out)[:n]
        return out, f"{{songs}} by artists like {hit[0]}: " + _list_words(names[:3], max(0, len(names) - 3)), "shuffled", \
            {"artist_key": hit[1], "artist": hit[0]}

    # ---- featuring
    if sp.featuring:
        hit = lib.artist(sp.featuring)
        fk = hit[1] if hit else vocab.akey(sp.featuring)
        shown = hit[0] if hit else sp.featuring
        q = "SELECT * FROM tracks WHERE credit_key LIKE ? AND main_key != ?"
        args = [f"% {fk} %", fk]
        rows = picker.rows(lib.con.execute(q, args))
        if sp.names:
            keys = {h[1] for h in (lib.artist(n) for n in sp.names) if h}
            rows = [r for r in rows if r.get("main_key") in keys or r.get("primary_key") in keys]
        rows = _filter(rows, sp, ag)
        if not rows:
            raise Unresolved(f"I couldn't find songs featuring {shown} in your music.")
        rows = picker.pick(rows, sp, fb, _labels(lib, sp), n=sp.count or len(rows), minutes=sp.minutes)
        return _shuffle(rows), f"{{songs}} featuring {shown}", "shuffled", {"artist_key": fk, "artist": shown}

    # ---- named artists (or albums)
    if sp.names and sp.leftover and (sp.genres or sp.moods or sp.years):
        # a band whose name holds a genre / mood word ("house of pain", "rage against the machine", "pop smoke")
        said = specmod._strip_edges(specmod._clean(sp.raw))
        hit = picker.find_artist(lib.con, said, [n for n, _r in lib.folders()], cutoff=0.93)
        if hit:
            sp.names, sp.names_whole = [hit[0]], hit[0]
            sp.genres, sp.moods, sp.years = [], [], []
    if sp.names:
        hits_ = []
        whole = lib.artist(sp.names_whole) if sp.names_whole and len(sp.names) > 1 else None
        if whole:
            hits_ = [whole]
        else:
            for n in sp.names:
                h = lib.artist(n)
                if h and sp.maybe_count and len(sp.names) == 1 and not n.split()[0].isdigit():
                    sp.count = sp.maybe_count                 # "a few eminem songs": the name came clean
                if not h:
                    if sp.maybe_count and len(sp.names) == 1:
                        parts = n.split(None, 1)
                        h = lib.artist(parts[1]) if len(parts) == 2 else None
                        if h:
                            sp.count = sp.maybe_count
                    if not h:
                        if len(sp.names) == 1 and not (sp.genres or sp.years or sp.moods or sp.best_of or sp.newest):
                            al = picker.find_album(lib.con, n)
                            if al:
                                trs = picker.album_tracks(lib.con, al["main_key"], al["album_key"])
                                return trs, f"{al['album']} by {al['main_artist']} — {{songs}}", "in order", \
                                    {"artist_key": al["main_key"], "artist": al["main_artist"]}
                        raise Unresolved(n)
                if h not in hits_:
                    hits_.append(h)
        rows_by = {k: _filter(lib.artist_rows(k), sp, ag) for _n, k in hits_}
        shown = _list_words([n for n, _k in hits_])
        if not any(rows_by.values()):
            what = sp.describe()
            raise Unresolved(f"I couldn't find any {what + ' ' if what != 'music' else ''}songs by {shown} "
                             "in your music.")
        extra = {"artist_key": hits_[0][1], "artist": hits_[0][0]}
        if sp.best_of:
            out = []
            for name, k in hits_:
                rs = rows_by[k]
                known = brain_hits(lib, brain, name, k, [r.get("title") or "" for r in rs])
                out += picker.hits(rs, fb, n=sp.count or 20, known_titles=known)
            poss = f"{shown}'" if shown.endswith("s") else f"{shown}'s"
            return out, f"{poss} {{n}} best-known songs", "", extra
        if sp.newest:
            out = []
            for _n, k in hits_:
                rs = [r for r in rows_by[k] if not picker._JUNK_TITLE.search(r.get("title") or "")]
                rs.sort(key=lambda r: (-(r.get("year") or 0), (r.get("album") or "").lower(), r.get("disc") or 1,
                                       r.get("track") or 999))
                out += rs[:sp.count or 15]
            poss = f"{shown}'" if shown.endswith("s") else f"{shown}'s"
            one = sp.count == 1
            return out, (f"{poss} newest song" if one else f"{poss} {{n}} newest songs"), "" if one else "newest first", extra
        if sp.moods or sp.count or sp.minutes:
            pool = [r for k in rows_by for r in rows_by[k]]
            out = picker.pick(pool, sp, fb, _labels(lib, sp), n=sp.count or (None if sp.minutes else len(pool)),
                              minutes=sp.minutes)
            return _shuffle(out), f"{{n}} {sp.describe() + ' ' if sp.describe() != 'music' else ''}songs by {shown}", \
                "shuffled", dict(extra, mood=sp.moods[0] if sp.moods else "")
        out = []
        for _n, k in hits_:
            rs = [r for r in rows_by[k] if not int((fb.get(r["path"]) or {}).get("banned") or 0)]
            if len(hits_) > 1 and len(rs) > PER_ARTIST:
                rs = picker.hits(rs, fb, n=PER_ARTIST // 2) + _shuffle(rs)[:PER_ARTIST]
                seen, keep = set(), []
                for r in rs:
                    if r["path"] not in seen:
                        seen.add(r["path"])
                        keep.append(r)
                rs = keep[:PER_ARTIST]
            out += rs
        out = _shuffle(out)[:MAX_LIST]
        return out, (f"{{n}} {shown} songs" if len(hits_) == 1 else f"{{songs}} by {shown}"), "shuffled", extra

    # ---- genres / eras / moods / hits with no artist: the whole library
    if not (sp.genres or sp.years or sp.moods or sp.best_of or sp.not_genres or sp.not_years or sp.count
            or sp.minutes):
        raise Unresolved("")
    pool = picker.candidates(lib.con, sp)
    if not pool:
        c = index.counts()
        what = sp.describe()
        if (sp.genres or sp.moods) and c["tagged"] < c["tracks"] * 0.9:
            raise Unresolved(f"I don't have any {what} yet — I'm still reading your songs' tags ({c['tagged']:,} of "
                             f"{c['tracks']:,} so far), and genres come from those. Try again in a while.")
        raise Unresolved(f"I couldn't find any {what} in your music.")
    n = sp.count or (None if sp.minutes else DEFAULT_N)
    cap = max(3, (n or 60) // 8)
    out = picker.pick(pool, sp, fb, _labels(lib, sp), n=n, minutes=sp.minutes, cap_per_artist=cap)
    return _shuffle(out), "{songs}" + (" of " + sp.describe() if sp.describe() != "music" else ""), "shuffled", \
        {"mood": sp.moods[0] if sp.moods else ""}


def _speak_count(n: int) -> str:
    return f"{n} song" if n == 1 else f"{n} songs"


def play_name(cfg, text: str, brain=None) -> dict | None:
    """'play joyner lucas' / 'play eminem and logic' / 'play the weekend': an artist (or several) named plainly —
    only on a sure match. None = not an artist: the song / movie search takes it."""
    rs = roots(cfg)
    if not rs:
        return None
    shown = playlists.find(specmod._strip_edges(specmod._clean(text)) or text, cutoff=0.95)
    if shown:                                       # "play road trip": his own playlist of that name
        return play_spec(cfg, specmod.MusicSpec(raw=text, saved=shown, playlist_word=True), brain)
    sp = specmod.parse(text)
    if sp.song_by or not (sp.names or sp.genres or sp.moods):
        return None
    with Lib(cfg) as lib:
        folders = [n for n, _r in lib.folders()]
        said = specmod._strip_edges(specmod._clean(text))
        hit = picker.find_artist(lib.con, said, folders, cutoff=0.93)
        names = [hit] if hit else []
        if not hit and len(sp.names) > 1:
            names = [picker.find_artist(lib.con, n, folders, cutoff=0.93) for n in sp.names]
            if not all(names):
                return None
        if not names:
            return None
    plain = specmod.MusicSpec(raw=text, names=[h[0] for h in names], names_whole=names[0][0] if len(names) == 1 else "",
                              shuffle=sp.shuffle)
    return play_spec(cfg, plain, brain)


def play_album_named(cfg, text: str, artist: str = "") -> dict | None:
    """'play thriller' when no song or movie is called that, 'play rumours by fleetwood mac': the ALBUM of that
    name, in order — only on a sure match. None = no such album."""
    if not roots(cfg) or not (text or "").strip():
        return None
    with Lib(cfg) as lib:
        key = None
        if artist:
            hit = lib.artist(artist)
            if not hit:
                return None
            key = hit[1]
            lib.artist_rows(key)
        al = picker.find_album(lib.con, text, key, cutoff=0.9)
        if not al and not key:
            al = _album_split(lib, text)
        if not al:
            return None
    sp = specmod.MusicSpec(raw=text, album=al["album"], album_artist=al["main_artist"], shuffle=False)
    return play_spec(cfg, sp)


def play_text(cfg, text: str, brain=None, shuffle=None, playlist: bool = False, said: str = "",
              drop_names: bool = False) -> dict:
    """'play some sad 90s rap' and friends: build the list, open it in the player. {"ok": False, "fallback": True} =
    nothing in the library by those names — the caller tries the plain song / movie search."""
    if playlist and not (text or "").strip():
        return {"ok": True, "say": "Sure — what should go on it? Say something like 'make a playlist of 90s rock' or "
                                   "'make a playlist called road trip with Eminem and Logic'."}
    sp = specmod.parse(text)
    if shuffle is not None:
        sp.shuffle = shuffle
    if drop_names:
        sp.names, sp.names_whole, sp.leftover, sp.maybe_count = [], "", "", None
    return play_spec(cfg, sp, brain)


def play_spec(cfg, sp, brain=None) -> dict:
    if not roots(cfg):
        return {"ok": False, "say": "I can't see your music folder. Set it on the Home Lab page, under Media."}
    if not sp.saved and not (sp.album or sp.albums or sp.album_pick or sp.discography) and sp.raw:
        said = specmod._strip_edges(specmod._clean(sp.raw))
        shown = playlists.find(said, cutoff=0.95) if said else None
        if shown:                                   # "play road trip" / "shuffle chill": his own playlist by that name
            sp = specmod.MusicSpec(raw=sp.raw, saved=shown, playlist_word=True, shuffle=sp.shuffle)
    ensure_index(cfg)
    with Lib(cfg) as lib:
        try:
            rows, what, order, extra = build(lib, sp, brain)
        except Unresolved as u:
            return {"ok": False, "fallback": True, "say": str(u) or f"I couldn't find {sp.raw} in your music.",
                    "matches": []}
        if not rows:
            return {"ok": False, "say": f"I couldn't find any {sp.describe()} in your music."}
        if sp.shuffle is True and order != "shuffled":
            rows, order = _shuffle(rows), "shuffled"
        r = playlists.play(rows, _phrase(what, rows, ""), cfg, artist_key=extra.get("artist_key", ""),
                           artist=extra.get("artist", ""), saved=extra.get("saved", ""),
                           mood=extra.get("mood", "") or (sp.moods[0] if sp.moods else ""))
    if not r.get("ok"):
        return r
    _label_later(cfg, brain, sp, rows)
    say = "Playing " + _phrase(what, rows, order) + "."
    if extra.get("missing"):
        say = f"You don't have a playlist called {extra['missing']}, so I made one. " + say
    if r.get("player") and r["player"] != player.get().name:     # it went elsewhere: "... in your music player."
        say = say[:-1] + f" in {r['player']}."
    return {"ok": True, "say": say, "count": len(rows), "kind": "playlist", "playlist_file": r.get("playlist_file")}


def _phrase(fmt: str, rows: list[dict], order: str) -> str:
    """'{songs} of sad 90s hip hop' + 100 rows + 'shuffled' -> '100 songs of sad 90s hip hop, shuffled'."""
    n = len(rows)
    out = fmt.replace("{songs}", _speak_count(n)).replace("{n}", str(n))
    if n == 1 and "newest song" in out:
        out += ": " + _title_of(rows[0])
    if order and n > 1:
        out += ", " + order
    return out


# ---------------------------------------------------------------- saved playlists
def _current_list() -> tuple[list[str], str]:
    sess = playlists.session()
    if sess.get("kind") == "list" and sess.get("paths"):
        return list(sess["paths"]), sess.get("label") or "this list"
    p = current_path()
    if p:
        return [p], "this song"
    return [], ""


def save_current(cfg, name: str) -> dict:
    if not (name or "").strip():
        return {"ok": True, "say": "What should I call it? Say 'save this as' and a name, like 'save this as my gym "
                                   "playlist'."}
    paths, _what = _current_list()
    if not paths:
        return {"ok": False, "say": "There's nothing playing that I started — play something first, then say "
                                    "'save this as' and a name."}
    old = playlists.find(name, cutoff=0.95)
    note = ""
    if old:
        playlists.remove(old)                        # (kept in Playlists\Deleted, never lost)
        note = " It replaces the old one, which I kept in the Deleted folder."
    with Lib(cfg) as lib:
        rows = [lib.row_for(p) for p in paths]
    shown = playlists.save(name, rows)
    return {"ok": True, "say": f"Saved {_speak_count(len(rows))} as your {shown} playlist.{note}", "name": shown}


def create(cfg, name: str, text: str = "", brain=None) -> dict:
    if playlists.find(name, cutoff=0.95):
        shown = playlists.find(name, cutoff=0.95)
        return {"ok": False, "say": f"You already have a playlist called {shown}. Say 'add this song to {shown}', or "
                                    "pick another name."}
    if not (text or "").strip():
        shown = playlists.save(name, [])
        return {"ok": True, "say": f"Made an empty playlist called {shown}. While something plays, say 'add this song "
                                   f"to {shown}'."}
    if not roots(cfg):
        return {"ok": False, "say": "I can't see your music folder. Set it on the Home Lab page, under Media."}
    ensure_index(cfg)
    sp = specmod.parse(text)
    with Lib(cfg) as lib:
        try:
            rows, what, order, extra = build(lib, sp, brain)
        except Unresolved as u:
            return {"ok": False, "say": (str(u) or f"I couldn't find {text} in your music.") + " I didn't make the playlist."}
        shown = playlists.save(name, rows)
        r = playlists.play(rows, f"your {shown} playlist", cfg, saved=shown, artist_key=extra.get("artist_key", ""),
                           artist=extra.get("artist", ""), mood=extra.get("mood", ""))
    tail = " Playing it now." if r.get("ok") else ""
    return {"ok": True, "say": f"Made your {shown} playlist: {_phrase(what, rows, '')}.{tail}", "name": shown,
            "count": len(rows), "kind": "playlist"}


def _song_for_text(cfg, lib, text: str, brain=None) -> list[dict]:
    """'lose yourself' / 'lose yourself by eminem' / 'some eminem' / 'the album adhd' -> rows to add."""
    from .. import music

    sp = specmod.parse(text)
    if not sp.structured():
        ranked, _how = music.find_song_scored(cfg, text, 3)
        if ranked and ranked[0][0] >= 0.75:
            return [lib.row_for(ranked[0][1])]
    try:
        rows, _what, _o, _e = build(lib, sp, brain)
    except Unresolved:
        return []
    if not sp.album and not sp.albums and not sp.count and len(rows) > 25:
        rows = picker.hits(rows, picker.feedback(lib.con), n=25)
    return rows


def add(cfg, name: str, what: str = "song", text: str = "", brain=None) -> dict:
    with Lib(cfg) as lib:
        if what == "spec":
            rows = _song_for_text(cfg, lib, text, brain)
            if not rows:
                return {"ok": False, "say": f"I couldn't find {text} in your music."}
            label = _title_of(rows[0]) if len(rows) == 1 else _speak_count(len(rows))
        else:
            p = current_path()
            if not p:
                return {"ok": False, "say": "I can't tell what's playing — play something from your library first."}
            cur = lib.row_for(p)
            if what == "album":
                if not cur.get("album_key"):
                    return {"ok": False, "say": "I don't know which album this song is on."}
                rows = picker.album_tracks(lib.con, cur["main_key"], cur["album_key"]) or [cur]
                label = f"the album {cur['album']} ({_speak_count(len(rows))})"
            else:
                rows = [cur]
                label = _title_of(cur)
        shown = playlists.find(name)
        existing = playlists.load(shown) if shown else []
        have = {os.path.normcase(p) for p in existing}
        new = [r for r in rows if os.path.normcase(r["path"]) not in have]
        allrows = [lib.row_for(p) for p in existing] + new
        shown2 = playlists.save(shown or name, allrows)
    if not new:
        return {"ok": True, "say": f"{label[0].upper() + label[1:]} is already on {shown2}."}
    started = "" if shown else f"Started a new playlist called {shown2} with "
    if started:
        return {"ok": True, "say": f"{started}{label}."}
    return {"ok": True, "say": f"Added {label} to {shown2} — it has {_speak_count(len(allrows))} now."}


def remove(cfg, name: str, what: str = "song", text: str = "", brain=None) -> dict:
    shown = playlists.find(name)
    if not shown:
        return {"ok": False, "say": f"You don't have a playlist called {playlists.display(name)}."}
    with Lib(cfg) as lib:
        if what == "spec":
            rows = _song_for_text(cfg, lib, text, brain)
        else:
            p = current_path()
            rows = [lib.row_for(p)] if p else []
        if not rows:
            return {"ok": False, "say": "I can't tell which song you mean — play it, then say 'remove this song from "
                                        f"{shown}'."}
        drop = {os.path.normcase(r["path"]) for r in rows}
        existing = playlists.load(shown)
        keep = [p for p in existing if os.path.normcase(p) not in drop]
        if len(keep) == len(existing):
            return {"ok": True, "say": f"{_title_of(rows[0])} isn't on {shown}."}
        playlists.save(shown, [lib.row_for(p) for p in keep])
    gone = len(existing) - len(keep)
    what_s = _title_of(rows[0]) if gone == 1 else _speak_count(gone)
    return {"ok": True, "say": f"Took {what_s} off {shown} — {_speak_count(len(keep))} left. The song itself is "
                               "untouched."}


def show(cfg, name: str) -> dict:
    shown = playlists.find(name)
    if not shown:
        return {"ok": False, "say": f"You don't have a playlist called {playlists.display(name)}."}
    paths = playlists.load(shown)
    if not paths:
        return {"ok": True, "say": f"{shown} is empty."}
    with Lib(cfg) as lib:
        first = [_title_of(lib.row_for(p)) for p in paths[:5]]
    more = len(paths) - len(first)
    return {"ok": True, "say": f"{shown} has {_speak_count(len(paths))}: " + _list_words(first, more) + "."}


def list_all(cfg=None) -> dict:
    sv = playlists.saved()
    if not sv:
        return {"ok": True, "say": "You don't have any saved playlists yet. While a list plays, say 'save this as' and "
                                   "a name."}
    items = [f"{n} ({len(playlists.read_m3u8(p))})" for n, p in sv.items()]
    return {"ok": True, "say": f"You have {len(sv)} playlist{'s' if len(sv) != 1 else ''}: " + _list_words(items) +
                               ". The numbers are how many songs."}


def rename(cfg, old: str, new: str) -> dict:
    try:
        r = playlists.rename(old, new)
    except FileExistsError as e:
        return {"ok": False, "say": f"You already have a playlist called {e}."}
    except Exception as exc:
        return {"ok": False, "say": f"I couldn't rename it: {exc}"}
    if not r:
        return {"ok": False, "say": f"You don't have a playlist called {playlists.display(old)}."}
    return {"ok": True, "say": f"Renamed {r[0]} to {r[1]}."}


def delete(cfg, name: str) -> dict:
    try:
        shown = playlists.remove(name)
    except Exception as exc:
        return {"ok": False, "say": f"I couldn't delete it: {exc}"}
    if not shown:
        return {"ok": False, "say": f"You don't have a playlist called {playlists.display(name)}."}
    return {"ok": True, "say": f"Deleted your {shown} playlist. Only the list is gone — your songs are untouched."}


def delete_confirm_text(name: str) -> str:
    shown = playlists.find(name, cutoff=0.9) or playlists.display(name)
    return f"Delete your {shown} playlist? It only removes the list, not the songs. Say yes to delete it."


# ---------------------------------------------------------------- while it plays: feedback
def _fb(con, path: str, col: str, inc: int = 1) -> None:
    con.execute("INSERT OR IGNORE INTO feedback(path, ts) VALUES(?, ?)", (path, time.time()))
    if col == "banned":
        con.execute("UPDATE feedback SET banned=1, ts=? WHERE path=?", (time.time(), path))
    else:
        con.execute(f"UPDATE feedback SET {col}={col}+?, ts=? WHERE path=?", (inc, time.time(), path))
    con.commit()


def _mood_fb(con, path: str, delta: float) -> None:
    mood = playlists.session().get("mood") or ""
    if not mood:
        return
    r = con.execute("SELECT fit FROM moods WHERE path=? AND mood=?", (path, mood)).fetchone()
    fit = max(-1.0, min(1.0, (float(r["fit"]) if r else 0.0) + delta))
    con.execute("INSERT OR REPLACE INTO moods(path, mood, fit, src, ts) VALUES(?,?,?,?,?)",
                (path, mood, fit, "you", time.time()))
    con.commit()


def session_live(max_age: float = 6 * 3600) -> bool:
    s = playlists.session()
    return bool(s and time.time() - float(s.get("ts", 0)) < max_age)


def note_skip(cfg=None) -> None:
    """'next' / 'skip' while her list plays: that song counts down a little (and for the list's mood)."""
    try:
        if not session_live():
            return
        p = current_path()
        if not p:
            return
        con = index.connect()
        try:
            _fb(con, p, "skips")
            _mood_fb(con, p, -0.5)
        finally:
            con.close()
    except Exception as exc:
        log.debug("skip feedback failed: %s", exc)


def _not_playing() -> str:
    return f"I can't tell what's playing — I only know songs I put on in {player.get().name}."


def _skipped(done: str) -> tuple[bool, str]:
    """The player's next(): (True, done) when it skipped, else (False, its own reason) — so a room speaker that
    didn't answer is never reported as skipped (Winamp's media key says `done`, exactly as before)."""
    nx = player.get().next()
    nx = nx if isinstance(nx, dict) else {"ok": bool(nx)}
    if nx.get("ok"):
        return True, done
    return False, nx.get("say") or "I couldn't skip it."


def love(cfg=None) -> dict:
    p = current_path()
    if not p:
        return {"ok": False, "say": _not_playing()}
    con = index.connect()
    try:
        _fb(con, p, "loves")
        _mood_fb(con, p, +1.0)
        r = con.execute("SELECT * FROM tracks WHERE path=?", (p,)).fetchone()
    finally:
        con.close()
    title = _title_of(dict(r)) if r else os.path.splitext(os.path.basename(p))[0]
    return {"ok": True, "say": f"Noted — you love {title}. I'll play it more."}


def ban(cfg=None) -> dict:
    p = current_path()
    if not p:
        return {"ok": False, "say": _not_playing()}
    con = index.connect()
    try:
        _fb(con, p, "banned")
        r = con.execute("SELECT * FROM tracks WHERE path=?", (p,)).fetchone()
    finally:
        con.close()
    title = _title_of(dict(r)) if r else os.path.splitext(os.path.basename(p))[0]
    _, said = _skipped("Skipped it.")            # (the ban is kept either way)
    return {"ok": True, "say": f"Okay — I won't put {title} on again. {said}"}


def skip_artist(cfg) -> dict:
    p = current_path()
    if not p:
        ok, said = _skipped("Skipped. I can't tell who's playing, so I couldn't drop the rest of their songs.")
        return {"ok": ok, "say": said}
    sess = playlists.session()
    paths = list(sess.get("paths") or [])
    with Lib(cfg) as lib:
        cur = lib.row_for(p)
        who, key = cur.get("main_artist") or "this artist", cur.get("main_key") or ""
        try:
            _fb(lib.con, p, "skips")
        except Exception:
            pass
        low = [os.path.normcase(x) for x in paths]
        at = low.index(os.path.normcase(p)) if os.path.normcase(p) in low else -1
        rest = [lib.row_for(x) for x in paths[at + 1:]] if at >= 0 else []
        keep = [r for r in rest if (r.get("main_key") or "") != key]
    if not keep:
        ok, said = _skipped(f"Skipped {who}.")
        return {"ok": ok, "say": said}
    r = playlists.play(keep, (sess.get("label") or "the list") + f" without {who}", cfg,
                       mood=sess.get("mood", ""), artist_key=sess.get("artist_key", ""), artist=sess.get("artist", ""))
    if not r.get("ok"):
        return r
    return {"ok": True, "say": f"Skipped {who} — the rest of the list is on without them ({_speak_count(len(keep))})."}


def more_like(cfg, brain=None) -> dict:
    p = current_path()
    if not p:
        return {"ok": False, "say": _not_playing() + " Say 'play artists like' and a name."}
    with Lib(cfg) as lib:
        cur = lib.row_for(p)
        key, who = cur.get("main_key") or "", cur.get("main_artist") or ""
        fb = picker.feedback(lib.con)
        sims = picker.similar_artists(lib.con, key, 8) if key else []
        out = picker.hits(lib.artist_rows(key), fb, n=5) if key else []
        names = []
        for k in sims:
            rs = lib.artist_rows(k)
            got = picker.hits(rs, fb, n=5) if len(rs) > 5 else rs
            if got:
                out += got
                names.append(got[0].get("main_artist") or k)
        if len(out) <= 5:
            g = picker.track_genres(cur, picker.artist_genres(lib.con))
            if g:
                sp = specmod.MusicSpec(genres=sorted(g, key=lambda x: (bool(vocab.PARENTS.get(x)) is False, x))[:1])
                out += picker.pick(picker.candidates(lib.con, sp), sp, fb, n=40, cap_per_artist=4)
        out = [r for r in out if os.path.normcase(r["path"]) != os.path.normcase(p)]
        if not out:
            return {"ok": False, "say": f"I don't know enough about {who or 'this song'} yet to find more like it."}
        out = _shuffle(out)
        r = playlists.play(out, f"more like {_title_of(cur)}", cfg, artist_key=key, artist=who)
    if not r.get("ok"):
        return r
    also = f": {who}, " + _list_words(names[:3], max(0, len(names) - 3)) if names else ""
    return {"ok": True, "say": f"Playing more like {_title_of(cur)}{also} — {_speak_count(len(out))}."}


# ---------------------------------------------------------------- questions about the library
def albums_by(cfg, artist: str) -> dict:
    if not roots(cfg):
        return {"ok": False, "say": "I can't see your music folder. Set it on the Home Lab page, under Media."}
    with Lib(cfg) as lib:
        hit = lib.artist(artist)
        if not hit:
            return {"ok": True, "say": f"I don't see {artist} in your music."}
        albs = picker.artist_albums(lib.artist_rows(hit[1]))
    if not albs:
        return {"ok": True, "say": f"You have songs by {hit[0]}, but no albums — just loose tracks."}
    items = [f"{n} ({y})" if y else n for n, y, _t in albs]
    if len(items) > 12:
        return {"ok": True, "say": f"You have {len(items)} albums by {hit[0]}, including " + _list_words(items[:8]) + "."}
    return {"ok": True, "say": f"You have {len(items)} album{'s' if len(items) != 1 else ''} by {hit[0]}: " +
                               _list_words(items) + "."}


def count_by(cfg, artist: str) -> dict:
    if not roots(cfg):
        return {"ok": False, "say": "I can't see your music folder. Set it on the Home Lab page, under Media."}
    with Lib(cfg) as lib:
        hit = lib.artist(artist)
        if not hit:
            return {"ok": True, "say": f"I don't see {artist} in your music."}
        rows = lib.artist_rows(hit[1])
    albs = len({r.get("album_key") for r in rows if r.get("album_key")})
    tail = f" on {albs} album{'s' if albs != 1 else ''}" if albs else ""
    return {"ok": True, "say": f"You have {_speak_count(len(rows))} by {hit[0]}{tail}."}


def status(cfg) -> dict:
    c = index.counts()
    if not c["tracks"]:
        if roots(cfg):
            ensure_index(cfg)
            return {"ok": True, "say": "I've only just started reading your music library — it takes a few hours the "
                                       "first time over the network. Artist and album requests work meanwhile."}
        return {"ok": False, "say": "I can't see your music folder. Set it on the Home Lab page, under Media."}
    left = c["tracks"] - c["tagged"]
    if c["building"] or c.get("resuming"):
        tail = f" I've read the tags of {c['tagged']:,} so far and I'm still going through the rest in the background."
    elif left > 0:
        tail = f" {left:,} of them had tags I couldn't read, so I go by their folder names."
    else:
        tail = " Every song's tags are read."
    return {"ok": True, "say": f"Your music library has {c['tracks']:,} songs by {c['artists']:,} artists.{tail}"}


def reindex(cfg) -> dict:
    rs = roots(cfg)
    if not rs:
        return {"ok": False, "say": "I can't see your music folder. Set it on the Home Lab page, under Media."}
    started = index.INDEXER.start(rs, force=True)
    if not started:
        return {"ok": True, "say": "I'm already reading your music library in the background."}
    return {"ok": True, "say": "Okay — I'm re-reading your music library in the background. Songs that haven't "
                               "changed are skipped, so it goes quickly after the first time."}
