"""Shared logger: console + a local conversation/action log file.

`get()` hands back the bare 'ava' logger until `setup()` has run, so importing
modules never creates a stray log file (e.g. inside the frozen app's _internal).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_SETUP_DONE = False


def setup(log_file: str = "ava.log", level: str = "INFO") -> logging.Logger:
    global _SETUP_DONE
    logger = logging.getLogger("ava")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")

    if sys.stdout is not None:          # windowed exe has no console
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    try:
        path = Path(log_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / log_file
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as exc:
        logger.warning("Could not open log file %s: %s", log_file, exc)

    logger.propagate = False
    _SETUP_DONE = True
    return logger


def get() -> logging.Logger:
    logger = logging.getLogger("ava")
    if not _SETUP_DONE and not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger
