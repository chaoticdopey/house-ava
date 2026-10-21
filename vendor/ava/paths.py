"""Where Ava keeps its writable data (config, logs, reminders).

Dev runs keep everything in the project folder. The installed (frozen) app writes
to %APPDATA%\Ava so it never tries to write inside Program Files / _internal.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(os.getenv("APPDATA", str(Path.home()))) / "Ava"
    else:
        base = Path(__file__).resolve().parent.parent  # project root
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base
