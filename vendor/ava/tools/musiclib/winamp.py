"""Winamp's own window messages (its documented IPC): is it playing, pause / resume exactly, which file is current.

Read-only except pause/resume — and those are Winamp's own Pause button, sent only when its state says it's playing
(to pause) or paused (to resume), so a toggle can never start something that was stopped. Every call has a short
timeout: a hung Winamp never hangs Ava.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

WM_USER = 0x0400
WM_COMMAND = 0x0111
IPC_ISPLAYING = 104          # 1 playing, 3 paused, 0 stopped
IPC_GETLISTLENGTH = 124
IPC_GETLISTPOS = 125
IPC_GETPLAYLISTFILEW = 214   # wchar_t* in Winamp's own memory
BTN_PAUSE = 40046            # the Pause button (toggles pause while playing)
SMTO_ABORTIFHUNG = 0x0002
_STATES = {1: "playing", 3: "paused", 0: "stopped"}


def _user32():
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.user32
    except Exception:
        return None


def hwnd() -> int:
    u = _user32()
    if u is None:
        return 0
    try:
        u.FindWindowW.restype = wintypes.HWND
        return int(u.FindWindowW("Winamp v1.x", None) or 0)
    except Exception:
        return 0


def _send(h: int, msg: int, wparam: int, lparam: int, timeout_ms: int = 600) -> int | None:
    u = _user32()
    if u is None or not h:
        return None
    try:
        res = ctypes.c_size_t(0)
        ok = u.SendMessageTimeoutW(wintypes.HWND(h), msg, ctypes.c_size_t(wparam), ctypes.c_ssize_t(lparam),
                                   SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(res))
        if not ok:
            return None
        return int(res.value)
    except Exception:
        return None


def state() -> str | None:
    """'playing' / 'paused' / 'stopped', or None when Winamp isn't running (or doesn't answer)."""
    h = hwnd()
    if not h:
        return None
    v = _send(h, WM_USER, 0, IPC_ISPLAYING)
    if v is None:
        return None
    return _STATES.get(v & 0xFFFFFFFF, None)


def press_pause() -> bool:
    h = hwnd()
    if not h:
        return False
    return _send(h, WM_COMMAND, BTN_PAUSE, 0) is not None


def pause() -> bool:
    """Pause only if it's playing."""
    return state() == "playing" and press_pause()


def resume() -> bool:
    """Resume only if it's paused (never starts a stopped Winamp)."""
    return state() == "paused" and press_pause()


def list_pos() -> int | None:
    return _send(hwnd(), WM_USER, 0, IPC_GETLISTPOS)


def list_len() -> int | None:
    return _send(hwnd(), WM_USER, 0, IPC_GETLISTLENGTH)


def current_file() -> str | None:
    """The file Winamp is on (read from its memory with the documented IPC_GETPLAYLISTFILEW pointer)."""
    h = hwnd()
    if not h:
        return None
    pos = _send(h, WM_USER, 0, IPC_GETLISTPOS)
    if pos is None:
        return None
    ptr = _send(h, WM_USER, pos, IPC_GETPLAYLISTFILEW)
    if not ptr:
        return None
    try:
        u = _user32()
        k = ctypes.windll.kernel32
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(wintypes.HWND(h), ctypes.byref(pid))
        PROCESS_VM_READ, PROCESS_QUERY_INFORMATION = 0x0010, 0x0400
        k.OpenProcess.restype = wintypes.HANDLE
        hp = k.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid.value)
        if not hp:
            return None
        try:
            buf = ctypes.create_string_buffer(2 * 1024)
            got = ctypes.c_size_t(0)
            ok = k.ReadProcessMemory(hp, ctypes.c_void_p(ptr), buf, len(buf), ctypes.byref(got))
            if not ok:
                return None
            raw = buf.raw[:got.value]
            text = raw.decode("utf-16-le", errors="ignore").split("\x00", 1)[0]
            return text or None
        finally:
            k.CloseHandle(hp)
    except Exception:
        return None
