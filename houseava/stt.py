"""Speech to text: faster-whisper.

The Echo streams mic audio as 16 kHz / 16-bit LE / mono over the ESPHome API, which faster-whisper
takes directly. A small English model on CPU is plenty for short commands; use GPU only if there's
VRAM free beside qwen2.5:14b (on a 12 GB card the VRAM budget is everything).

Lazy import so the text MVP runs without faster-whisper installed.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("houseava.stt")


class Transcriber:
    def __init__(self, cfg):
        self.model_name = str(cfg.get("stt.model", "small.en"))
        # On a 12 GB RTX A2000, large-v3-turbo int8_float16 = ~0.4 s/command vs 1.5-1.8 s for small.en on
        # the CPU, and ~1 GB of VRAM beside qwen2.5:14b. Needs the venv's nvidia-cublas/cudnn libs on LD_LIBRARY_PATH.
        self.fallback_model = str(cfg.get("stt.fallback_model", "small.en"))
        self.device = str(cfg.get("stt.device", "auto"))
        self.compute_type = str(cfg.get("stt.compute_type", "int8"))
        # Whisper invents plausible words out of TV babble / room noise when a wake word false-fires - the bedroom's
        # "Turn off bedroom light." (10-15) was exactly that, and it got acted on. But whisper's confidence can't
        # separate the two on a far-field mic: real speech to her scores avg_logprob -0.36..-0.97 (clean TTS audio
        # scores -0.02..-0.07 - which is what a -0.35 cutoff was wrongly calibrated on; it ate "How are you?"),
        # reversed-speech babble -0.46. So this is a safety net for true garbage only (-2.0), plus no-speech.
        self.min_logprob = float(cfg.get("stt.min_logprob", -2.0))
        self.max_no_speech = float(cfg.get("stt.max_no_speech", 0.6))
        self.hotwords = self._hotwords(cfg)
        self._model = None

    @staticmethod
    def _hotwords(cfg) -> str | None:
        """The house's own words, so whisper spells them right on a far-field mic: "bedroom" came out as "bit the",
        "better" and "victim" on 10-15. NOUNS ONLY (room + bulb names, her name) - never commands - so a false wake on
        room noise can't be nudged into hallucinating "turn off the bedroom light". OFF BY DEFAULT: an A/B on degraded
        TTS audio (10-15) made it WORSE ("light 1" -> "Light Line", "dim the" -> "In the") while "bedroom" was already
        right - re-test on REAL Echo recordings before turning on. stt.hotwords = "auto" (room names) or own text."""
        custom = cfg.get("stt.hotwords", "")
        if str(custom).strip().lower() != "auto":
            return str(custom).strip() or None
        words: list[str] = []
        for src in ((cfg.get("lights.rooms", {}) or {}).keys(),
                    [str(s.get("room", "")) for s in (cfg.get("satellites", []) or []) if isinstance(s, dict)]):
            for w in src:
                w = str(w).strip().lower()
                if w and w not in words:
                    words.append(w)
        return ", ".join(["Ava"] + [w.capitalize() for w in words]) if words else None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy

            dev = self.device if self.device != "auto" else "cpu"
            try:
                self._model = WhisperModel(self.model_name, device=dev, compute_type=self.compute_type)
                log.info("stt: %s on %s (%s)", self.model_name, dev, self.compute_type)
            except Exception as exc:
                if dev == "cpu":
                    raise
                # GPU model didn't load (driver / cuDNN / VRAM): stay able to hear on the CPU rather than go deaf
                fb = self.fallback_model
                log.warning("stt: %s on %s failed (%s) - falling back to %s on the CPU", self.model_name, dev, exc, fb)
                self._model = WhisperModel(fb, device="cpu", compute_type="int8")
        return self._model

    def warm_up(self) -> None:
        """Load the model and run it once on silence, so the first real command isn't the slow one."""
        self._load().transcribe(np.zeros(16000, dtype=np.float32), language="en", vad_filter=False)

    def transcribe_pcm16(self, pcm_bytes: bytes, sample_rate: int = 16000, hotwords: str | None = None,
                         beam_size: int | None = None) -> str:
        """Transcribe raw 16-bit LE mono PCM (what the Echo sends) to text; "" when it doesn't trust what it heard.
        hotwords / beam_size = a second, house-vocabulary listen (SatelliteHub.listen_again) - never the first pass."""
        if not pcm_bytes:
            return ""
        audio = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        hw = hotwords or self.hotwords
        kw = {"hotwords": hw} if hw else {}
        if beam_size:
            kw["beam_size"] = int(beam_size)
        segments, _ = self._load().transcribe(audio, language="en", vad_filter=True,
                                              condition_on_previous_text=False, **kw)
        segs = list(segments)
        if not segs:
            return ""
        text = " ".join(s.text for s in segs).strip()
        if not text:
            return ""
        logprob = sum(float(s.avg_logprob) for s in segs) / len(segs)
        nospeech = max(float(s.no_speech_prob) for s in segs)
        if logprob < self.min_logprob or nospeech > self.max_no_speech:
            log.info("stt: dropped low-confidence %r (avg_logprob %.2f, no_speech %.2f)", text, logprob, nospeech)
            return ""
        return text
