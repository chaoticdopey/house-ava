"""Offline voice: Kokoro-82M ("Heart") via ONNX — for when Microsoft Edge TTS can't be reached.

House Ava's whole point is working when the internet is down: local lights, local brain (your server's
LLM), local STT (whisper). Edge TTS was her one online dependency — this removes it, so she never
goes silent in an outage. Model files live in <data_dir>/voices/kokoro or <project>/voices/kokoro
(the same files desktop Ava ships). Produces 16 kHz mono int16 PCM, what the Echo wants.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from .config import data_dir

log = logging.getLogger("houseava.offline_tts")

MODEL = "kokoro-v1.0.fp16.onnx"
VOICES = "voices-v1.0.bin"
DEFAULT_VOICE = "af_heart"          # "Heart — US female (best)"
GAIN = 1.3                          # Kokoro renders ~2 dB quieter than Edge's Ava; match her loudness

# English voices, best first (per Kokoro's own grades) — offered in the admin UI
CHOICES = [
    ("af_heart", "Heart - US female (best)"),
    ("af_bella", "Bella - US female"),
    ("af_nicole", "Nicole - US female, soft"),
    ("af_sarah", "Sarah - US female"),
    ("bf_emma", "Emma - British female"),
    ("am_michael", "Michael - US male"),
    ("bm_george", "George - British male"),
]


def _candidates() -> list[Path]:
    return [data_dir() / "voices" / "kokoro",
            Path(__file__).resolve().parent.parent / "voices" / "kokoro"]


def model_dir() -> Path | None:
    for d in _candidates():
        if (d / MODEL).is_file() and (d / VOICES).is_file():
            return d
    return None


def _resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or pcm.size == 0:
        return pcm
    n_out = int(round(len(pcm) * dst / src))
    xs = np.linspace(0, len(pcm) - 1, n_out)
    return np.interp(xs, np.arange(len(pcm)), pcm).astype(np.float32)


class OfflineVoice:
    def __init__(self):
        self._k = None
        self._lock = threading.Lock()

    @staticmethod
    def installed() -> bool:
        if model_dir() is None:
            return False
        try:
            import importlib.util
            return importlib.util.find_spec("kokoro_onnx") is not None
        except Exception:
            return False

    @property
    def loaded(self) -> bool:
        return self._k is not None

    def _engine(self):
        with self._lock:
            if self._k is None:
                d = model_dir()
                if d is None:
                    raise RuntimeError("offline voice files missing (voices/kokoro)")
                from kokoro_onnx import Kokoro
                log.info("loading offline voice from %s", d)
                self._k = Kokoro(str(d / MODEL), str(d / VOICES))
            return self._k

    def preload(self) -> None:
        """Load the model in the background so the first offline reply isn't delayed."""
        def work():
            try:
                self._engine()
                log.info("offline voice ready")
            except Exception as exc:
                log.warning("offline voice preload failed: %s", exc)
        threading.Thread(target=work, daemon=True, name="houseava-offline-voice").start()

    def synth_pcm16(self, text: str, voice: str = DEFAULT_VOICE, rate_hz: int = 16000) -> bytes:
        """Text -> 16 kHz mono int16 PCM bytes (resampled from Kokoro's 24 kHz)."""
        k = self._engine()
        lang = "en-gb" if (voice or "a").startswith("b") else "en-us"
        pcm, sr = k.create(text, voice=voice or DEFAULT_VOICE, speed=1.0, lang=lang)
        pcm = np.asarray(pcm, dtype=np.float32) * GAIN
        np.clip(pcm, -1.0, 1.0, out=pcm)
        pcm = _resample(pcm, int(sr), rate_hz)
        return (pcm * 32767.0).astype("<i2").tobytes()
