"""Is that speech for her? Keeps an Echo from answering the room.

An Echo kept waking up and talking when nobody had spoken to her. Three things did it, all fixed here and
in satellite.py / brain.py:

  * whisper turns silence and noise into stock phrases ("Thank you.", "you", subtitle credits) -> `is_noise`
    drops those, so a false wake ends quietly instead of getting an answer.
  * everything that reached the brain got answered, even people talking to each other or the TV -> the brain's
    "ignore" intent (brain.Router, heard=...) lets her stay quiet; the hub never logs or remembers that speech.
  * follow-up turns (the mic re-opened after her reply, no wake word) never ended: echod leaves ending them to the
    client - Home Assistant sends STT_VAD_START / STT_VAD_END from its own voice detector - and House Ava sent
    neither, so every follow-up recorded the room until echod's limit. `SpeechTracker` is that detector (Silero,
    the one bundled with faster-whisper).

`speakable` drops emoji and *stage directions* the model sometimes adds, so TTS doesn't read them out.
"""

from __future__ import annotations

import re

_RATE = 16000                 # the Echo's mic audio: 16 kHz, 16-bit LE, mono
_BYTES_PER_S = _RATE * 2

# what whisper says when there's no speech in the clip (or only noise / a chime), as a whole utterance.
# Deliberately NOT here: yes / no / okay / yeah / stop / music (a real "Alexa, music"), digits - real answers/commands.
_HALLUCINATIONS = {
    "thank you", "thank you very much", "thank you so much", "thanks", "thanks for watching",
    "thank you for watching", "thanks for watching and see you next time", "thank you for watching and see you next time",
    "please subscribe", "like and subscribe", "subscribe", "see you next time", "see you in the next video",
    "you", "bye", "bye bye", "goodbye", "the", "a", "i", "so", "oh", "uh", "um", "uh huh", "hmm", "mm", "mhm",
    "huh", "ah", "foreign", "blank audio", "inaudible", "no audio",
}
# whisper's sound captions: "[Music]", "(applause)", "*laughs*", "♪ ♪" - the whole transcript one bracketed/noted thing
_CAPTION = re.compile(r"^\s*(?:[\[(*][^\])*]{0,40}[\])*]\s*)+$|^[\s♪♫.,!-]*$")
_CREDITS = re.compile(r"^(?:subtitles?|captions?|transcri(?:bed|ption)|translated|translation) by\b")
_NON_WORD = re.compile(r"[^a-z0-9' ]+")


def normalize(text: str) -> str:
    """'  Thank you!! ' -> 'thank you'; brackets/music notes/punctuation gone, whitespace collapsed."""
    t = (text or "").lower().replace(chr(0x2019), "'")
    t = _NON_WORD.sub(" ", t)
    return " ".join(t.split()).strip(" '")


def is_noise(text: str) -> bool:
    """True when a transcript is nothing anyone said to her: empty, a sound caption, a lone letter, or one of whisper's
    stock inventions for silence and noise."""
    if _CAPTION.match(text or ""):
        return True
    t = normalize(text)
    if not t or (len(t) == 1 and not t.isdigit()):
        return True
    return t in _HALLUCINATIONS or bool(_CREDITS.match(t))


# emoji + pictographs, dingbats, variation selectors / joiners, flags
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⬀-⯿︎️‍⃣]")
# *giggles* / *rolls eyes* / *winks playfully*: a stage direction = asterisks that START with an action (a verb in its
# -s / -ing / -ed form, or one of a few set phrases), maybe after an adverb. Only those go - "*not*", "*clear* skies",
# "*happy*" are emphasis and stay (as plain words), and "3 * 4 * 5" is arithmetic (spaces inside), never touched.
_ACTS = (r"(?:giggl|chuckl|cackl|smil|wav|pout|whistl)(?:es|ing|ed)|(?:laugh|snicker|snort|gasp|wink|smirk|cough|"
         r"yawn|facepalm|sniffl|groan|scoff|huff)(?:s|ing|ed)|(?:sigh|nod|grin|shrug|clap|hug|blush)(?:s|es|ing|ed|"
         r"ding|ning|ging|ping)|rolls? (?:her |my |his )?eyes|rolling (?:her |my |his )?eyes|eye ?rolls?|clears? "
         r"(?:her |my |his )?throat|clearing (?:her |my |his )?throat|evil laugh|dramatic (?:sigh|pause|gasp)|"
         r"happy dance|sips? tea|mic drop")
_STAGE = re.compile(r"(?<![\w*])\*(?:[a-z]+ly\s+)?(?:" + _ACTS + r")\b[^*\n]{0,36}\*(?![\w*])", re.I)
_BOLD = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]{1,60}?)(?<=\S)\*(?![\w*])")


def speakable(text: str) -> str:
    """The reply as it should be heard: no emoji, no *stage directions*, **bold** / *emphasis* kept as plain words,
    tidy spacing. Never empty if the reply had any words in it."""
    raw = str(text or "")
    t = _STAGE.sub(" ", raw)
    t = _BOLD.sub(r"\1", t)
    t = _ITALIC.sub(r"\1", t)
    t = _EMOJI.sub("", t)
    t = re.sub(r"\s+([,!?;:]|\.(?!\d))", r"\1", " ".join(t.split()))
    return t.strip() or _EMOJI.sub("", raw).strip()


def wav_seconds(wav: bytes) -> float:
    """How long a reply WAV plays (header first, else its size as 16 kHz mono s16)."""
    try:
        import io
        import wave

        with wave.open(io.BytesIO(wav)) as w:
            return w.getnframes() / float(w.getframerate() or _RATE)
    except Exception:
        return max(0, len(wav or b"") - 44) / _BYTES_PER_S


# ---- end of speech for follow-up turns -----------------------------------------------------------------------------
def silero_spans(pcm: bytes) -> list[tuple[float, float]]:
    """Speech in 16 kHz s16 mono PCM as [(start_s, end_s)], from the Silero VAD bundled with faster-whisper.
    No padding, short gaps split: the ends have to be where the talking actually stopped."""
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    # (short min_speech: a word just starting again after a pause must show up at once, or its pause would look like
    # the end; SpeechTracker applies its own 0.3 s before it calls anything a START)
    opts = VadOptions(threshold=0.5, min_speech_duration_ms=60, min_silence_duration_ms=250, speech_pad_ms=0)
    return [(s["start"] / _RATE, s["end"] / _RATE) for s in get_speech_timestamps(audio, opts)]


def warm_up() -> bool:
    """Load the VAD model now (it's small) so the first follow-up doesn't wait on it. False if it can't load."""
    try:
        silero_spans(b"\x00\x00" * (_RATE // 2))
        return True
    except Exception:
        return False


class SpeechTracker:
    """Watches one follow-up turn's audio as it arrives and says when someone starts talking ("start") and when
    they've finished ("end") - what Home Assistant's own detector tells echod with STT_VAD_START / STT_VAD_END.

    Only the last `window_s` of audio is looked at each time (cheap however long the turn runs). Speech has to last
    `min_speech_s` to count, and `end_silence_s` of quiet after it ends the turn. Not thread-safe: one check at a
    time (the hub runs `check` in a worker thread and skips while one is running)."""

    def __init__(self, spans=silero_spans, *, hop_s: float = 0.3, window_s: float = 3.0,
                 min_speech_s: float = 0.3, end_silence_s: float = 0.8):
        self.spans = spans
        self.hop = int(hop_s * _BYTES_PER_S)
        self.window = int(window_s * _BYTES_PER_S)
        self.min_speech_s = min_speech_s
        self.end_silence_s = end_silence_s
        self.started = False          # someone has talked in this turn
        self.ended = False            # ...and stopped (the turn should end now)
        self.failed = False           # the VAD itself broke: stop trying, leave the turn to echod's limit
        self._checked = 0             # audio length (bytes) at the last check

    def due(self, nbytes: int) -> bool:
        return not (self.ended or self.failed) and nbytes - self._checked >= self.hop

    def check(self, pcm: bytes) -> list[str]:
        """Look at the audio so far; returns the new events, in order: [], ["start"], ["end"] or ["start", "end"]."""
        if self.ended or self.failed:
            return []
        self._checked = len(pcm)
        tail = pcm[-self.window:] if len(pcm) > self.window else pcm
        if len(tail) % 2:
            tail = tail[1:]
        try:
            raw = list(self.spans(bytes(tail)))
        except Exception:
            self.failed = True
            return []
        tail_s = len(tail) / _BYTES_PER_S
        out: list[str] = []
        if not self.started and any(s[1] - s[0] >= self.min_speech_s for s in raw):   # (a blip doesn't START a turn...)
            self.started = True
            out.append("start")
        if self.started:
            # (...but any speech at all keeps it going: "yeah... [pause] turn it up" mustn't end on the pause because
            # "turn" is still short.) Nothing in the window = they stopped longer ago than it (a slow check): the end.
            quiet = tail_s - raw[-1][1] if raw else tail_s
            if quiet >= self.end_silence_s:
                self.ended = True
                out.append("end")
        return out
