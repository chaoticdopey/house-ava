"""Who plays her lists: the one seam between the music library and an actual player.

ops / playlists never press a key or start a program themselves; they ask `get()` for the player in use and call:

    open_list(m3u8, cfg)   start this .m3u8 (Now Playing) from its first song -> {"ok", "player", "say"?}
    current_file()         the file it's on now, when it can tell
    position()             (index, length) in the list it was given, when it can tell
    next()                 on to the next song -> {"ok", "say"?}
    state()                "playing" / "paused" / "stopped", or None (the same contract as media_session.Player)
    pause() / resume()     an exact pause / play — never a toggle that could start something that was stopped

Desktop Ava: WinampPlayer, the default — Winamp's own messages, the media key for "next", exactly as before.
A service playing on other speakers sets its own player for a request with `use(p)` (a context variable, so a worker
started with asyncio.to_thread sees it; a plain threading.Thread does not) or for the whole process with
`set_default(p)`. A player with a `key` gets its own Now Playing / session files ("Now Playing - kitchen.m3u8"), so two
rooms never share "what's playing". `set_data_dir` moves the index, playlists and session out of Ava's data folder.

Nothing from Ava is imported here until a WinampPlayer method runs (or data_dir() falls back to Ava's own folder), so
the library imports on Linux without any of the desktop's PC-control code.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import subprocess
from pathlib import Path


class Player:
    name = "your music player"     # how she says it: "... in {name}", "songs I put on in {name}"
    key = ""                       # "" = the shared Now Playing / session files (desktop); "kitchen" = its own files

    def open_list(self, m3u8_path: str, cfg=None) -> dict:
        return {"ok": False, "player": self.name, "say": "I made the playlist but there's no player to play it."}

    def current_file(self) -> str | None:
        return None

    def position(self) -> tuple[int | None, int | None]:
        return None, None

    def next(self) -> dict:
        return {"ok": False, "say": "I can't skip on this player."}

    def state(self) -> str | None:
        return None

    def pause(self) -> bool:
        return False

    def resume(self) -> bool:
        return False


class WinampPlayer(Player):
    """Winamp (else your default player for playlists). Each method looks its helper up when it runs, so stubs of
    music.winamp_path / winamp.* / media.media still catch every call."""

    name = "Winamp"

    def open_list(self, m3u8_path, cfg=None):
        from .. import music

        player = music.winamp_path(cfg)
        try:
            if player:
                subprocess.Popen([player, str(m3u8_path)], close_fds=True)
                return {"ok": True, "player": "Winamp"}
            os.startfile(str(m3u8_path))  # noqa: S606 — your default player for playlists
            return {"ok": True, "player": "your music player"}
        except Exception as exc:
            return {"ok": False, "say": f"I made the playlist but couldn't start the player: {exc}"}

    def current_file(self):
        from . import winamp

        return winamp.current_file()

    def position(self):
        from . import winamp

        return winamp.list_pos(), winamp.list_len()

    def next(self):
        from .. import media      # the global next-track key (Winamp has no "next" message of its own)

        return media.media("next")

    def state(self):
        from . import winamp

        return winamp.state()

    def pause(self):
        from . import winamp

        return winamp.pause()

    def resume(self):
        from . import winamp

        return winamp.resume()


# ---------------------------------------------------------------- which player is in use
_WINAMP = WinampPlayer()
_DEFAULT: Player | None = None
_CUR: contextvars.ContextVar = contextvars.ContextVar("musiclib_player", default=None)


def get() -> Player:
    """The player for this request (`use`), else the process default (`set_default`), else Winamp."""
    p = _CUR.get()
    if p is not None:
        return p
    return _DEFAULT if _DEFAULT is not None else _WINAMP


def set_default(p: Player | None) -> None:
    global _DEFAULT
    _DEFAULT = p


@contextlib.contextmanager
def use(p: Player):
    """`with player.use(kitchen): ops.play_text(...)` — this context (and threads started with asyncio.to_thread
    inside it) plays on `kitchen`; the player before comes back afterwards."""
    tok = _CUR.set(p)
    try:
        yield p
    finally:
        _CUR.reset(tok)


# ---------------------------------------------------------------- where the index, playlists and session live
_DATA: Path | None = None


def set_data_dir(path) -> None:
    """A folder of its own for the index, playlists and session (None = back to Ava's data folder)."""
    global _DATA
    _DATA = Path(path) if path else None


def data_dir() -> Path:
    if _DATA is None:
        from ... import paths

        return paths.data_dir()
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return _DATA
