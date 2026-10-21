"""Speaker verification for House Ava — pure numpy MFCC voice profiles.

Ported from the desktop Ava's voiceprint module. Enrolls ~12 s of speech into a
mean/std profile. Each captured command is scored against enrolled profiles to
identify who's speaking. This is a voice *filter* for permissions, not a security system.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("houseava.voiceprint")

_SR = 16000
_FRAME = 400          # 25 ms
_HOP = 160            # 10 ms
_NFFT = 512
_NMEL = 26
_NCEP = 13

_FILTERS: np.ndarray | None = None
_DCT: np.ndarray | None = None


def _mel(f):
    return 2595.0 * math.log10(1.0 + f / 700.0)


def _imel(m):
    return 700.0 * (10 ** (m / 2595.0) - 1.0)


def _filters() -> np.ndarray:
    global _FILTERS
    if _FILTERS is None:
        lo, hi = _mel(80), _mel(_SR / 2)
        pts = np.array([_imel(m) for m in np.linspace(lo, hi, _NMEL + 2)])
        bins = np.floor((_NFFT + 1) * pts / _SR).astype(int)
        fb = np.zeros((_NMEL, _NFFT // 2 + 1), dtype=np.float32)
        for i in range(1, _NMEL + 1):
            l, c, r = bins[i - 1], bins[i], bins[i + 1]
            for k in range(l, c):
                fb[i - 1, k] = (k - l) / max(1, c - l)
            for k in range(c, r):
                fb[i - 1, k] = (r - k) / max(1, r - c)
        _FILTERS = fb
    return _FILTERS


def _dct() -> np.ndarray:
    global _DCT
    if _DCT is None:
        n = np.arange(_NMEL)
        _DCT = np.array([[math.cos(math.pi * k * (2 * i + 1) / (2 * _NMEL)) for i in n] for k in range(_NCEP)],
                        dtype=np.float32)
    return _DCT


def mfcc(pcm_int16: np.ndarray) -> np.ndarray:
    """(frames, 12) MFCCs (c1..c12) for int16 mono 16 kHz audio, voiced frames only."""
    x = pcm_int16.astype(np.float32) / 32768.0
    if x.size < _FRAME * 4:
        return np.zeros((0, _NCEP - 1), dtype=np.float32)
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])
    n = 1 + (len(x) - _FRAME) // _HOP
    idx = np.arange(_FRAME)[None, :] + _HOP * np.arange(n)[:, None]
    frames = x[idx] * np.hamming(_FRAME).astype(np.float32)
    energy = (frames ** 2).mean(axis=1)
    keep = energy > max(1e-6, np.percentile(energy, 40))
    frames = frames[keep]
    if frames.shape[0] < 10:
        return np.zeros((0, _NCEP - 1), dtype=np.float32)
    spec = np.abs(np.fft.rfft(frames, n=_NFFT)) ** 2
    mel = np.log(spec @ _filters().T + 1e-8)
    ceps = mel @ _dct().T
    return ceps[:, 1:].astype(np.float32)


def utterance_vector(pcm_int16: np.ndarray) -> np.ndarray | None:
    """24-dim summary: mean and std of MFCCs over the utterance."""
    c = mfcc(pcm_int16)
    if c.shape[0] < 10:
        return None
    return np.concatenate([c.mean(axis=0), c.std(axis=0)])


class VoiceProfile:
    """A set of voice samples: one per enrollment session. A command matches if close to ANY sample."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.samples: list[dict] = []
        self.load()

    def load(self) -> bool:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict) and "mu" in d:
                    d = {"samples": [{"mu": d["mu"], "sigma": d["sigma"], "threshold": d["threshold"],
                                      "at": d.get("enrolled_at", 0), "label": "sample 1"}]}
                self.samples = []
                for s in d.get("samples", []):
                    self.samples.append({
                        "mu": np.array(s["mu"], dtype=np.float32),
                        "sigma": np.array(s["sigma"], dtype=np.float32),
                        "threshold": float(s["threshold"]), "at": float(s.get("at", 0)),
                        "label": str(s.get("label", f"sample {len(self.samples) + 1}")),
                    })
                return True
        except Exception as exc:
            log.warning("voice profile load failed: %s", exc)
        return False

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"samples": [
                {"mu": s["mu"].tolist(), "sigma": s["sigma"].tolist(), "threshold": s["threshold"],
                 "at": s["at"], "label": s["label"]} for s in self.samples]}), encoding="utf-8")
        except Exception as exc:
            log.error("voice profile save failed: %s", exc)

    def delete(self) -> None:
        self.samples = []
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass

    @property
    def enrolled(self) -> bool:
        return bool(self.samples)

    @property
    def count(self) -> int:
        return len(self.samples)

    def enroll(self, pcm_int16: np.ndarray, label: str = "") -> bool:
        """ADD a sample from ~10-15 s of speech (2 s chunks)."""
        chunk = _SR * 2
        vecs = []
        for start in range(0, len(pcm_int16) - chunk + 1, chunk // 2):
            v = utterance_vector(pcm_int16[start:start + chunk])
            if v is not None:
                vecs.append(v)
        if len(vecs) < 4:
            return False
        m = np.stack(vecs)
        mu = m.mean(axis=0)
        sigma = m.std(axis=0) + 1e-3
        dists = np.array([float(np.mean(np.abs((v - mu) / sigma))) for v in vecs])
        thr = float(dists.mean() + 2.5 * dists.std() + 0.15)
        self.samples.append({"mu": mu, "sigma": sigma, "threshold": thr, "at": time.time(),
                             "label": label or f"sample {len(self.samples) + 1}"})
        self.save()
        log.info("Voice sample %d enrolled from %d chunks (threshold %.2f).", len(self.samples), len(vecs), thr)
        return True

    def score(self, pcm_int16: np.ndarray) -> tuple[float, float] | None:
        if not self.enrolled:
            return None
        v = utterance_vector(pcm_int16)
        if v is None:
            return None
        best = None
        for s in self.samples:
            d = float(np.mean(np.abs((v - s["mu"]) / s["sigma"])))
            ratio = d / max(1e-6, s["threshold"])
            if best is None or ratio < best[0] / max(1e-6, best[1]):
                best = (d, s["threshold"])
        return best

    def matches(self, pcm_int16: np.ndarray, sensitivity: float = 1.0) -> tuple[bool, float | None]:
        sc = self.score(pcm_int16)
        if sc is None:
            return True, None
        d, thr = sc
        return d <= thr * max(0.3, sensitivity), d

    def info(self) -> dict:
        """Serializable summary for the admin API."""
        return {
            "enrolled": self.enrolled,
            "samples": len(self.samples),
            "labels": [s["label"] for s in self.samples],
            "dates": [s["at"] for s in self.samples],
        }
