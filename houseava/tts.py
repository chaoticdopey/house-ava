"""Her voice: edge-tts (Microsoft neural) → decode → 16 kHz mono WAV (what the Echo wants),
with an OFFLINE fallback (Kokoro "Heart") for when the internet is down and Microsoft can't be
reached — so House Ava never goes silent in an outage (the whole point of a local assistant).

Echo reply delivery is "whole file via URL": the hub serves the WAV from http_server.py and hands
the Echo the URL in the TTS_END event. 16 kHz mono WAV is the safe format.

Imports edge-tts / av / kokoro lazily so the text MVP runs without them.
"""

from __future__ import annotations

import io
import logging
import time
import wave

log = logging.getLogger("houseava.tts")

_EDGE_TIMEOUT = 10          # seconds to wait for Microsoft before giving up on a reply
_EDGE_RETRY = 60            # after Edge fails, use the offline voice this long before trying Microsoft again
_state = {"edge_down_until": 0.0}
_offline = None             # lazy OfflineVoice singleton


def _get_offline():
    global _offline
    if _offline is None:
        from .offline_tts import OfflineVoice
        _offline = OfflineVoice()
    return _offline


def synth_wav(text: str, cfg) -> bytes:
    """Return 16 kHz mono 16-bit WAV bytes of `text` in her voice (Edge, else the offline voice)."""
    voice = str(cfg.get("voice.edge_voice", "en-US-AvaNeural"))
    rate = str(cfg.get("voice.rate", "+0%"))
    pitch = str(cfg.get("voice.pitch", "+0Hz"))
    volume = str(cfg.get("voice.volume", "+0%"))
    fallback = bool(cfg.get("voice.offline_fallback", True))
    offline_voice = str(cfg.get("voice.offline_voice", "af_heart") or "af_heart")

    pcm = None
    edge_down = time.time() < _state["edge_down_until"]
    if not (edge_down and fallback):                 # skip Microsoft while it's known-down (fast offline replies)
        pcm = _try_edge(text, voice, rate, pitch, volume)
        if pcm is not None:
            if _state["edge_down_until"]:
                log.info("Edge TTS is back")
            _state["edge_down_until"] = 0.0          # Microsoft answered: the outage is over

    if pcm is None and fallback:
        if not edge_down:
            _state["edge_down_until"] = time.time() + _EDGE_RETRY
            log.warning("Edge TTS unreachable — using the offline voice (%s) for the next %ds",
                        offline_voice, _EDGE_RETRY)
        pcm = _try_offline(text, offline_voice)

    if pcm is None:
        raise RuntimeError("TTS failed: Microsoft unreachable and the offline voice is unavailable")
    return _wrap_wav(pcm, 16000)


def _wrap_wav(pcm: bytes, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def _try_edge(text: str, voice: str, rate: str, pitch: str, volume: str) -> bytes | None:
    """Microsoft Edge TTS -> 16 kHz mono int16 PCM, or None if it can't be reached."""
    try:
        import asyncio

        import edge_tts  # lazy

        async def run() -> bytes:
            buf = io.BytesIO()
            comm = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch, volume=volume)
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue()

        mp3 = asyncio.run(asyncio.wait_for(run(), timeout=_EDGE_TIMEOUT))
        if not mp3:
            return None
        return _mp3_to_pcm16(mp3, 16000)
    except Exception as exc:
        log.warning("edge tts failed: %s", exc)
        return None


def _try_offline(text: str, voice: str) -> bytes | None:
    """Kokoro offline voice -> 16 kHz mono int16 PCM, or None if it isn't installed / fails."""
    try:
        ov = _get_offline()
        if not ov.installed():
            log.error("offline voice not available (kokoro_onnx or model files missing in voices/kokoro)")
            return None
        return ov.synth_pcm16(text, voice=voice, rate_hz=16000)
    except Exception as exc:
        log.error("offline tts failed: %s", exc)
        return None


def _mp3_to_pcm16(mp3: bytes, rate: int) -> bytes:
    """Decode edge-tts MP3 to mono s16le at `rate` using PyAV (ships with faster-whisper)."""
    import av  # lazy
    import numpy as np

    container = av.open(io.BytesIO(mp3))
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=rate)
    chunks = []
    for frame in container.decode(container.streams.audio[0]):
        frame.pts = None
        for rf in (resampler.resample(frame) or []):
            chunks.append(rf.to_ndarray().reshape(-1))
    for rf in (resampler.resample(None) or []):
        chunks.append(rf.to_ndarray().reshape(-1))
    container.close()
    if not chunks:
        return b""
    return np.concatenate(chunks).astype("<i2").tobytes()


def preload_offline(cfg) -> None:
    """Warm the offline voice in the background (called at hub start) so the first outage reply is quick."""
    if bool(cfg.get("voice.offline_fallback", True)):
        try:
            ov = _get_offline()
            if ov.installed():
                ov.preload()
        except Exception as exc:
            log.debug("offline preload skipped: %s", exc)
