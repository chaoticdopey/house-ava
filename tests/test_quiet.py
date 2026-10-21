"""She doesn't answer the room: false wakes, whisper filler, chatter that isn't for her, and follow-ups that end.
Fake Echo, fake detector, fake brain - no device, no network beyond localhost, no whisper/Silero/LLM.

    .venv\\Scripts\\python.exe tests\\test_quiet.py
"""
import array
import asyncio
import io
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="houseava-test-")   # never the real conversation log / memory
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from aioesphomeapi import NumberInfo, SelectInfo, VoiceAssistantEventType as E  # noqa: E402

from houseava import speechgate  # noqa: E402
from houseava.brain import Router  # noqa: E402
from houseava.config import Config  # noqa: E402
from houseava.http_server import ReplyServer  # noqa: E402
from houseava.memory import Memory  # noqa: E402
from houseava.satellite import Satellite, SatelliteHub  # noqa: E402

fails = []
RATE = 16000


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


# ---- speechgate: the cheap checks ----------------------------------------------------------------------------------
for t in ("Thank you.", "  you ", "Thanks for watching!", "[Music]", "(applause)", "[Music] [Applause]", "*laughs*", "♪♪",
          "", ".", "...", "Subtitles by the Amara.org community", "Uh-huh.", "Bye.", "x"):
    check(f"noise: {t!r}", speechgate.is_noise(t))
for t in ("yes", "No.", "stop", "okay", "What time is it?", "turn off the kitchen lights", "Thank you, turn it off",
          "Music.", "2", "Applause for the cook"):
    check(f"NOT noise (a real answer/command): {t!r}", not speechgate.is_noise(t))

SPEAK = [
    ("Let's do this! \U0001F31F", "Let's do this!"),
    ("Because spinach! *giggles* Got it?", "Because spinach! Got it?"),
    ("Oh please. *rolls eyes* Fine.", "Oh please. Fine."),
    ("*winks playfully* Sure thing.", "Sure thing."),
    ("It's 2:59 AM.", "It's 2:59 AM."),
    ("No, you should *not* unplug it while it is updating.", "No, you should not unplug it while it is updating."),
    ("It is *not* going to rain today.", "It is not going to rain today."),
    ("The capital of France is **Paris**.", "The capital of France is Paris."),
    ("I am *so* done", "I am so done"),
    ("3 * 4 * 5 is 60.", "3 * 4 * 5 is 60."),
    ("2 * 3 is 6 and 4 * 5 is 20.", "2 * 3 is 6 and 4 * 5 is 20."),
    ("Half is .5", "Half is .5"),
    ("Okay , done !", "Okay, done!"),
    ("The forecast is *clear* skies all day.", "The forecast is clear skies all day."),
    ("I'd be *happy* to help.", "I'd be happy to help."),
    ("Rain is *clearly* not happening.", "Rain is clearly not happening."),
    ("You are *evil*.", "You are evil."),
    ("Give it a *wave*.", "Give it a wave."),
    ("That's a *huge* deal.", "That's a huge deal."),
    ("*laughs* Nope.", "Nope."),
    ("Fine. *sighs dramatically* Done.", "Fine. Done."),
    ("*nodding* Yep.", "Yep."),
    ("*clears throat* Attention please.", "Attention please."),
]
for raw, want in SPEAK:
    check(f"speakable: {raw!r}", speechgate.speakable(raw) == want, speechgate.speakable(raw))
check("speakable never empties a reply that had words", speechgate.speakable("*waves*") != "")


def make_wav(seconds):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\0\0" * int(RATE * seconds))
    return b.getvalue()


check("wav_seconds reads the header", abs(speechgate.wav_seconds(make_wav(2.5)) - 2.5) < 0.01)


# ---- a fake detector: "speech" = 0.1 s blocks with loud samples ----------------------------------------------------
def pcm(*parts):
    """pcm(("quiet", 1.0), ("talk", 1.5)) -> 16 kHz s16 mono bytes."""
    a = array.array("h")
    for kind, secs in parts:
        a.extend([6000 if kind == "talk" else 0] * int(RATE * secs))
    return a.tobytes()


def fake_spans(b):
    a = array.array("h")
    a.frombytes(b[: len(b) - len(b) % 2])
    blk, spans, cur = RATE // 10, [], None
    for i in range(0, len(a), blk):
        t0, loud = i / RATE, max((abs(x) for x in a[i:i + blk]), default=0) > 1000
        if loud and cur is None:
            cur = t0
        if not loud and cur is not None:
            spans.append((cur, t0))
            cur = None
    if cur is not None:
        spans.append((cur, len(a) / RATE))
    return spans


tr = speechgate.SpeechTracker(fake_spans)
check("tracker: quiet -> nothing yet", tr.check(pcm(("quiet", 1.0))) == [] and not tr.started)
check("tracker: someone talks -> start", tr.check(pcm(("quiet", 1.0), ("talk", 1.0))) == ["start"])
check("tracker: still talking -> nothing", tr.check(pcm(("quiet", 1.0), ("talk", 2.0))) == [])
check("tracker: a short pause (0.4 s) isn't the end", tr.check(pcm(("quiet", 1.0), ("talk", 2.0), ("quiet", 0.4))) == [])
check("tracker: 0.8 s of quiet after talking -> end",
      tr.check(pcm(("quiet", 1.0), ("talk", 2.0), ("quiet", 0.9))) == ["end"] and tr.ended)
tr2 = speechgate.SpeechTracker(fake_spans)
check("tracker: talk + quiet seen in one check -> start, end",
      tr2.check(pcm(("talk", 1.0), ("quiet", 1.0))) == ["start", "end"])
tr3 = speechgate.SpeechTracker(fake_spans)
tr3.check(pcm(("talk", 1.0)))
check("tracker: talked, then a slow check sees only quiet in its window -> end",
      tr3.check(pcm(("talk", 1.0), ("quiet", 4.0))) == ["end"])
tr5 = speechgate.SpeechTracker(fake_spans)
tr5.check(pcm(("talk", 1.0)))
check("tracker: 'yeah... [0.7 s pause] turn it up' - talking again (still short) is NOT the end",
      tr5.check(pcm(("talk", 1.0), ("quiet", 0.7), ("talk", 0.2))) == [] and not tr5.ended)
tr4 = speechgate.SpeechTracker(fake_spans)
check("tracker: a blip shorter than 0.3 s doesn't count", tr4.check(pcm(("quiet", 1), ("talk", 0.2), ("quiet", 1))) == [])
boom = speechgate.SpeechTracker(lambda b: 1 / 0)
check("tracker: a broken detector stops trying (no events, no crash)", boom.check(pcm(("talk", 1))) == [] and boom.failed
      and not boom.due(10 ** 9))


# ---- the hub ---------------------------------------------------------------------------------------------------
class Cfg:
    def __init__(self, **kw):
        self.d = {"http.host": "127.0.0.1", "http.port": 0, "http.public_host": "127.0.0.1", **kw}

    def get(self, k, d=None):
        return self.d.get(k, d)


class FakeClient:
    def __init__(self):
        self.events, self.numbers, self.selects = [], [], []

    def send_voice_assistant_event(self, etype, data):
        self.events.append((E(etype).name.replace("VOICE_ASSISTANT_", ""), dict(data or {})))

    def number_command(self, key, value):
        self.numbers.append((key, value))

    def select_command(self, key, value):
        self.selects.append((key, value))


class HeardRouter:
    """Records (text, room, heard); answers, or says 'ignored' for anything containing 'gossip'."""
    def __init__(self):
        self.calls = []

    def handle(self, text, room="", *, satellite="", heard=None):
        self.calls.append((text, room, heard))
        if "gossip" in text:
            return {"ok": True, "ignored": True, "say": ""}
        return {"ok": True, "say": f"okay: {text} \U0001F31F"}


def make_hub(transcripts, stt_delay=0.0, **cfg):
    router = HeardRouter()
    replies = ReplyServer(Cfg()).start()
    said = list(transcripts)
    stt_calls = []

    def stt(b):
        stt_calls.append(len(b))
        if stt_delay:
            time.sleep(stt_delay)
        return said.pop(0) if said else ""

    cfg = {"voice.follow_up": 6, "voice.follow_ups": 1, **cfg}
    hub = SatelliteHub(Cfg(**cfg), router=router, transcribe=stt, synth=lambda text: make_wav(1.0), replies=replies)
    hub._spans = fake_spans
    sat = Satellite(hub, {"name": "kitchen-echo", "ip": "127.0.0.1", "room": "kitchen"})
    sat.client = FakeClient()
    hub.sats = [sat]
    return hub, sat, router, replies, stt_calls


def names(sat):
    return [e[0] for e in sat.client.events]


async def settle():
    for _ in range(40):                       # let the tracker's worker-thread checks land
        await asyncio.sleep(0.01)


async def main():
    # 1. a false wake that whisper turns into "Thank you." -> quiet end, brain never asked, nothing logged
    hub, sat, router, replies, _ = make_hub(["Thank you."])
    await sat.handle_start("c", 0, None, "Alexa")
    await sat.handle_audio(pcm(("talk", 1)))
    await sat.handle_stop(False)
    check("whisper filler -> RUN_END alone (no reply, no error chime)", names(sat) == ["RUN_START", "STT_START", "RUN_END"],
          sat.client.events)
    check("... the brain is never asked and nothing is logged", not router.calls and not hub.convlog.recent(), router.calls)
    replies.stop()

    # 2. the brain says it wasn't for her -> quiet end: no STT_END on the screen, no reply, not in the log
    hub, sat, router, replies, _ = make_hub(["they were gossiping about the neighbors again"])
    await sat.handle_start("c", 0, None, "Alexa")
    await sat.handle_audio(pcm(("talk", 1)))
    await sat.handle_stop(False)
    check("ignored -> RUN_END alone", names(sat) == ["RUN_START", "STT_START", "RUN_END"], sat.client.events)
    check("... the router was told it came from a wake word", router.calls and router.calls[0][2] == "wake", router.calls)
    check("... and it isn't in the conversation log", not hub.convlog.recent(), hub.convlog.recent())
    check("... and no follow-up window opens after it", sat.reply_sent_at == 0 and sat.reply_end_est == 0)
    replies.stop()

    # 3. a real question: full turn, emoji gone from the spoken reply, follow-up window armed
    hub, sat, router, replies, stt = make_hub(["what time is it", "and tomorrow's weather?"])
    await sat.handle_start("c", 0, None, "Alexa")
    await sat.handle_audio(pcm(("talk", 1)))
    await sat.handle_stop(False)
    ev = dict(sat.client.events)
    check("a real question gets the whole turn",
          names(sat) == ["RUN_START", "STT_START", "STT_END", "INTENT_START", "INTENT_END", "TTS_START", "TTS_END",
                         "RUN_END"], names(sat))
    check("... and the reply she speaks has no emoji", ev.get("TTS_START", {}).get("text") == "okay: what time is it",
          ev.get("TTS_START"))
    check("... it's logged", len(hub.convlog.recent()) == 1)
    check("... and the follow-up window is armed (reply ~1 s long)",
          sat.reply_sent_at > 0 and 1.3 < sat.reply_end_est - sat.reply_sent_at < 1.7,
          (sat.reply_sent_at, sat.reply_end_est))

    # 4. ... the follow-up opens as her reply finishes: it's tracked, and ends when the talking does
    sat.client.events.clear()
    sat.reply_end_est = time.time()                     # (her reply just finished playing)
    await sat.handle_start("c2", 0, None, "Alexa")
    check("a turn opening as her reply ends = a follow-up, with a speech tracker",
          sat.turn is not None and sat.turn.follow_up and sat.turn.tracker is not None)
    await sat.handle_audio(pcm(("quiet", 0.6)))
    await settle()
    check("... quiet at first: nothing sent yet", names(sat) == ["RUN_START", "STT_START"], names(sat))
    await sat.handle_audio(pcm(("talk", 1.2)))
    await settle()
    check("... someone talks: STT_VAD_START (echod allows the full listen)", "STT_VAD_START" in names(sat), names(sat))
    await sat.handle_audio(pcm(("quiet", 1.0)))
    await settle()
    await sat.handle_audio(pcm(("quiet", 0.3)))          # (the next frames after a check)
    await settle()
    check("... they stop: STT_VAD_END, once", names(sat).count("STT_VAD_END") == 1, names(sat))
    await sat.handle_stop(False)                        # echod stops recording -> the hub answers
    check("... and it's answered, as a follow-up", router.calls[-1] == ("and tomorrow's weather?", "kitchen", "follow_up")
          and names(sat)[-1] == "RUN_END" and "TTS_END" in names(sat), (router.calls, names(sat)))
    # 4b. that was the one follow-up allowed (follow_ups=1): echod opens no more, so a wake word now is a normal turn
    sat.client.events.clear()
    sat.reply_end_est = time.time()
    await sat.handle_start("c3", 0, None, "Alexa")
    check("after the one follow-up allowed, the next turn by the reply is a wake-word turn",
          sat.turn is not None and not sat.turn.follow_up, sat.turn and sat.turn.follow_up)
    await sat.handle_stop(True)
    replies.stop()

    # 5. a follow-up nobody speaks into: echod's timer runs out -> it goes idle: "end of audio" then "stop" (both
    # handled as tasks by aioesphomeapi) -> quiet end, the room never transcribed
    hub, sat, router, replies, stt = make_hub(["something on the tv"])
    sat.reply_sent_at, sat.reply_end_est = time.time() - 2, time.time()     # (her reply just finished)
    await sat.handle_start("c", 0, None, "Alexa")
    await sat.handle_audio(pcm(("quiet", 6.25)))
    await settle()
    work = asyncio.ensure_future(sat.handle_stop(False))
    await asyncio.sleep(0.02)
    await sat.handle_stop(True)
    await work
    check("follow-up that timed out on the Echo -> RUN_END once, never transcribed, brain not asked",
          names(sat) == ["RUN_START", "STT_START", "RUN_END"] and not stt and not router.calls, (names(sat), stt))
    replies.stop()

    # 5b. a WAKE WORD right after her reply, taken for a follow-up: echod ends it itself (its own end of speech) and
    # sends no "stop" - so it was a wake-word turn: answered, never dropped. Short or long.
    for secs in (2.0, 5.5):
        hub, sat, router, replies, stt = make_hub(["turn off the kitchen light"])
        sat.reply_sent_at, sat.reply_end_est = time.time() - 2, time.time()
        await sat.handle_start("c", 0, None, "Alexa")
        check(f"({secs}s wake turn taken for a follow-up at first)", sat.turn.follow_up)
        await sat.handle_audio(pcm(("talk", secs - 0.6), ("quiet", 0.6)))
        await sat.handle_stop(False)                    # echod's own endpoint (no "stop" follows)
        check(f"a {secs}s wake-word command just after her reply is answered (as a wake turn), not dropped",
              router.calls == [("turn off the kitchen light", "kitchen", "wake")] and "TTS_END" in names(sat),
              (router.calls, names(sat)))
        check("... and the follow-up count starts over", sat.follow_ups_used == 0)
        replies.stop()

    # 5c. a wake word WHILE she's still talking (barge-in) is never a follow-up
    hub, sat, router, replies, _ = make_hub(["stop"])
    sat.reply_sent_at, sat.reply_end_est = time.time(), time.time() + 5
    await sat.handle_start("c", 0, None, "Alexa")
    check("a wake word over her reply (barge-in) is a normal turn", sat.turn and not sat.turn.follow_up)
    await sat.handle_stop(True)
    replies.stop()

    # 5d. follow-up off (0) -> never a follow-up
    hub, sat, router, replies, _ = make_hub(["hi"], **{"voice.follow_up": 0})
    sat.reply_sent_at, sat.reply_end_est = time.time() - 2, time.time()
    await sat.handle_start("c", 0, None, "Alexa")
    check("follow-up set to 0 -> a turn by the reply is a normal turn", sat.turn and not sat.turn.follow_up)
    await sat.handle_stop(True)
    replies.stop()

    # 6. a wake word long after her last reply is NOT a follow-up
    hub, sat, router, replies, _ = make_hub(["hi"])
    sat.reply_sent_at, sat.reply_end_est = time.time() - 60, time.time() - 55
    await sat.handle_start("c", 0, None, "Alexa")
    # (10-18: a wake turn has its own end-of-speech tracker too - 1.2 s of quiet ends it, so whoever speaks next
    # isn't recorded into it)
    check("a wake word a minute after her reply is a normal turn", sat.turn and not sat.turn.follow_up
          and sat.turn.tracker is not None and sat.turn.tracker.end_silence_s == 1.2)
    await sat.handle_stop(True)
    replies.stop()

    # 7. a turn that runs to the cutoff (TV on, never stops): the hub ends it before echod's failure chime
    hub, sat, router, replies, _ = make_hub(["blah " * 30], **{"voice.max_listen": 10})
    await sat.handle_start("c", 0, None, "Alexa")
    for _ in range(9):
        await sat.handle_audio(pcm(("talk", 1.0)))
    check("8.5 s into a 10 s max_listen -> STT_VAD_END from the hub, once",
          names(sat).count("STT_VAD_END") == 1 and sat.turn.hub_ended, names(sat))
    replies.stop()

    # 7b. on a follow-up the cutoff counts from when the talking started (echod restarts its clock there)
    hub, sat, router, replies, _ = make_hub(["x"], **{"voice.max_listen": 10, "voice.follow_up": 12})
    sat.reply_sent_at, sat.reply_end_est = time.time() - 2, time.time()
    await sat.handle_start("c", 0, None, "Alexa")
    for _ in range(9):                                  # 9 s of nobody talking: no cutoff (echod's timer's job)
        await sat.handle_audio(pcm(("quiet", 1.0)))
        await settle()
    check("follow-up with nobody talking -> the hub never cuts it off", "STT_VAD_END" not in names(sat), names(sat))
    for _ in range(6):                                  # then someone talks on and on (6 s)...
        await sat.handle_audio(pcm(("talk", 1.0)))
        await settle()
    check("... talking starts 9 s in: no cutoff 6 s later (it counts from the talking)",
          "STT_VAD_START" in names(sat) and "STT_VAD_END" not in names(sat), names(sat))
    for _ in range(4):
        await sat.handle_audio(pcm(("talk", 1.0)))
        await settle()
    check("... but 8.5 s after it started talking -> cut off", names(sat).count("STT_VAD_END") == 1, names(sat))
    replies.stop()

    # 7c. a big max_listen still gets cut off before the hub's 30 s storage cap
    hub, sat, router, replies, _ = make_hub(["x"], **{"voice.max_listen": 45})
    await sat.handle_start("c", 0, None, "Alexa")
    for _ in range(31):
        await sat.handle_audio(pcm(("talk", 1.0)))
    check("max_listen 45 -> the hub still ends the turn (by 29 s), never left to echod's failure chime",
          names(sat).count("STT_VAD_END") == 1, names(sat))
    replies.stop()

    # 7d. echod lets go of a turn the hub is still working on (its timeout / the button / a new wake word): RUN_END at
    # once, and nothing more is sent or done for it
    hub, sat, router, replies, stt = make_hub(["turn off the lights"], stt_delay=0.4)
    await sat.handle_start("c", 0, None, "Alexa")
    await sat.handle_audio(pcm(("talk", 1)))
    work = asyncio.ensure_future(sat.handle_stop(False))     # (aioesphomeapi runs these as tasks)
    await asyncio.sleep(0.1)
    await sat.handle_stop(True)
    check("echod lets go mid-transcription -> RUN_END at once", names(sat) == ["RUN_START", "STT_START", "RUN_END"],
          names(sat))
    await work
    check("... and nothing more is sent, the brain is never asked", names(sat) == ["RUN_START", "STT_START", "RUN_END"]
          and not router.calls, (names(sat), router.calls))
    replies.stop()

    # 7e. "snooze for ten minutes" over a ringing alarm: its words reach echod BEFORE the brain call (echod acts on them
    # itself, for only a few seconds after the wake word)
    hub, sat, router, replies, _ = make_hub(["snooze for ten minutes"])
    seen = []
    orig = router.handle
    router.handle = lambda *a, **k: (seen.append(list(names(sat))), orig(*a, **k))[1]
    await sat.handle_start("c", 0, None, "Alexa")
    await sat.handle_audio(pcm(("talk", 1)))
    await sat.handle_stop(False)
    check("alarm words -> STT_END goes out before the brain is asked, and only once",
          seen and "STT_END" in seen[0] and names(sat).count("STT_END") == 1, (seen, names(sat)))
    replies.stop()
    # ... exactly echod's own rules (stopsRing / SnoozeAsked): anything else waits for the brain (and isn't shown if
    # she ignores it)
    from houseava.satellite import _echod_acts_on
    for said, want in (("Stop.", True), ("turn it off please", True), ("okay stop the alarm", True), ("snooze", True),
                       ("snooze for ten minutes", True), ("stop the music", False),
                       ("we'll be right back in five minutes", False), ("turn that off, I'm trying to sleep", False),
                       ("cancel my timer", False), ("what time is it", False)):
        check(f"early STT_END for {said!r}: {want}", _echod_acts_on(said) is want)

    # 8. ignore_chatter off: filler goes to the brain as before, and the router gets no 'heard'
    hub, sat, router, replies, _ = make_hub(["Thank you."], **{"voice.ignore_chatter": False})
    await sat.handle_start("c", 0, None, "Alexa")
    await sat.handle_audio(pcm(("talk", 1)))
    await sat.handle_stop(False)
    check("ignore_chatter off -> she answers filler, router told nothing about how it was heard",
          router.calls == [("Thank you.", "kitchen", None)] and "TTS_END" in names(sat), (router.calls, names(sat)))
    replies.stop()

    # 9. listening settings land on the Echo (both wake-word slots), volume only when asked
    hub, sat, router, replies, _ = make_hub([], **{"voice.wake_threshold": 0.92, "voice.follow_ups": 1,
                                                   "voice.max_listen": 10, "voice.follow_up": 6,
                                                   "voice.follow_up_tone": "None", "voice.echo_volume": 1.0})
    ents = []
    for i, oid in enumerate(("wake_threshold_1", "wake_threshold_2", "follow_ups_1", "follow_ups_2", "max_listen_1",
                             "max_listen_2", "follow_up_1", "follow_up_2", "microphone_gain")):
        ents.append(NumberInfo(object_id=oid, key=100 + i, min_value=0.0, max_value=60.0, step=0.01))
    ents.append(SelectInfo(object_id="follow_up_tone_1", key=200, options=["Default", "None", "Chirp"]))
    ents.append(SelectInfo(object_id="follow_up_tone_2", key=201, options=["Default", "None", "Chirp"]))
    vol = []
    sat.speaker.set_volume = lambda v: vol.append(v)
    sat._apply_audio_prefs(sat.client, ents, volume=False)
    got = {next(e.object_id for e in ents if e.key == k): v for k, v in sat.client.numbers}
    want = {"wake_threshold_1": 0.92, "wake_threshold_2": 0.92, "follow_ups_1": 1, "follow_ups_2": 1, "max_listen_1": 10,
            "max_listen_2": 10, "follow_up_1": 6, "follow_up_2": 6}
    check("numbers: wake threshold / follow-ups / max listen / follow-up seconds on both slots", got == want, got)
    check("selects: follow-up tone -> None on both slots", sorted(sat.client.selects) == [(200, "None"), (201, "None")],
          sat.client.selects)
    check("volume=False leaves the Echo's volume alone", vol == [])
    check("the entities are kept for a live re-apply", len(sat.ents) == len(ents))
    # per WORD strictness once the slots are known: Hey Ava (its own scale) vs Alexa
    sat.hub.cfg.d.update({"wake.id": "hey_ava", "wake.threshold": 0.6})
    sat.wake_slots = ["hey_ava", "alexa"]
    sat.client.numbers.clear()
    sat.apply_wake_thresholds(sat.client)
    got = {next(e.object_id for e in ents if e.key == k): v for k, v in sat.client.numbers}
    check("wake strictness per word: slot 1 'Hey Ava' 0.6, slot 2 'Alexa' 0.92",
          got == {"wake_threshold_1": 0.6, "wake_threshold_2": 0.92}, got)
    replies.stop()

    # 10. turning "Hey Ava" on: the selection is really SENT (awaited), then what runs is read back by slot
    hub, sat, router, replies, _ = make_hub([], **{"wake.id": "hey_ava", "wake.threshold": 0.6,
                                                   "voice.wake_threshold": 0.92, "wake.keep_alexa": True})
    from types import SimpleNamespace as NS
    sent = []

    class WakeClient(FakeClient):
        async def get_voice_assistant_configuration(self, timeout=10, external_wake_words=None):
            running = sent[-1] if sent else ["alexa"]
            return NS(available_wake_words=[NS(id="alexa", wake_word="Alexa"), NS(id="hey_ava", wake_word="Hey Ava")],
                      active_wake_words=running, max_active_wake_words=2)

        async def set_voice_assistant_configuration(self, active_wake_words):
            sent.append(list(active_wake_words))

    sat.client = WakeClient()
    sat.ents = [NumberInfo(object_id="wake_threshold_1", key=1, min_value=0.5, max_value=0.99, step=0.01),
                NumberInfo(object_id="wake_threshold_2", key=2, min_value=0.5, max_value=0.99, step=0.01)]
    hub._wake_offer = {"id": "hey_ava", "name": "Hey Ava", "size": 10, "hash": "ab" * 32}
    await hub._setup_wake(sat, sat.client)
    check("Hey Ava: the selection is sent to the Echo (it was never awaited before)", sent == [["hey_ava", "alexa"]], sent)
    check("... the running slots are read back", sat.wake_slots == ["hey_ava", "alexa"], sat.wake_slots)
    check("... and each slot gets its word's strictness", sorted(sat.client.numbers) == [(1, 0.6), (2, 0.92)],
          sat.client.numbers)
    replies.stop()


asyncio.run(main())


# ---- the brain's "ignore" ------------------------------------------------------------------------------------------
class FakeLights:
    def rooms(self):
        return {"kitchen": [], "bedroom": ["bedroom 1"]}

    def set_state(self, *a, **k):
        return {"ok": True, "say": "lights"}


class FakePlugs:
    devices = {"porch": "Porch Light"}


class FakeBrain:
    def __init__(self, action):
        self.action, self.systems = action, []

    def chat_json(self, system, user, temperature=None):
        self.systems.append(system)
        self.users = getattr(self, "users", []) + [user]
        return {"action": self.action, "say": "" if self.action == "ignore" else "sure thing"}

    def chat(self, system, user, temperature=None, history=None):
        return "a chat answer"


def router(action):
    cfg = Config(path=os.path.join(os.path.dirname(__file__), "__no_such_config__.json"))
    mem = Memory(Path(tempfile.mkdtemp()) / "memory.json")
    b = FakeBrain(action)
    return Router(cfg, b, FakeLights(), FakePlugs(), mem, lambda: "persona"), b, mem


chatter = "so then he says the car needs new brakes before friday"
r, b, mem = router("ignore")
res = r.handle(chatter, "kitchen", satellite="kitchen-echo", heard="wake")
check("brain: a wake-word turn may be ignored", res.get("ignored") is True and res.get("say") == "", res)
check("brain: ... and that speech is NOT remembered", not any(t["text"] == chatter for t in mem.turns), mem.turns)
check("brain: the wake prompt offers 'ignore'", "|answer|ignore>" in b.systems[-1] and "wake word" in b.systems[-1])
res = r.handle(chatter, "kitchen", satellite="kitchen-echo", heard="follow_up")
check("brain: a follow-up gets the stricter prompt", res.get("ignored") and "without your wake word" in b.systems[-1])
res = r.handle(chatter, "console")
check("brain: typed text is never ignored (no 'ignore' offered; a stray one = answer)",
      not res.get("ignored") and res.get("say") and "ignore" not in b.systems[-1], (res, b.systems[-1][-200:]))
check("brain: ... and typed text is remembered", any(t["text"] == chatter for t in mem.turns))
r2, b2, mem2 = router("answer")
res = r2.handle("what's the capital of france", "kitchen", heard="wake")
check("brain: a real question on a voice turn is answered + remembered",
      res.get("say") == "sure thing" and any(t["text"] == "what's the capital of france" for t in mem2.turns), res)
r4, b4, mem4 = router("remember")
res = r4.handle("we need more milk and eggs from the store", "kitchen", heard="follow_up")
check("brain: a plain statement the model wants to 'remember' from a room mic -> ignored, nothing saved",
      res.get("ignored") and not mem4.facts, (res, mem4.facts))
res = r4.handle("don't forget the plumber comes thursday", "kitchen", heard="wake")
check("brain: an explicit 'don't forget ...' is still remembered", not res.get("ignored") and mem4.facts, (res, mem4.facts))
r5, b5, mem5 = router("remember")
res = r5.handle("we need more milk", "console")
check("brain: typed text keeps the old behaviour (the model's remember goes through)", not res.get("ignored"), res)
r3, b3, _ = router("ignore")
res = r3.handle("what time is it", "kitchen", heard="follow_up")
check("brain: fast-path requests (time, lights...) never reach the ignore check", res.get("say", "").startswith("It's")
      and not b3.systems, res)

# follow-ups hear the room: the clock/weather fast paths only answer a question; alarm/security/memory need the wake word
for said in ("it's almost dinner time", "we had a great time", "ugh this weather is awful", "back to normal I guess",
             "the hallway is all clear", "remember that guy from the store", "code red, the kids spilled juice"):
    r7, b7, mem7 = router("ignore")
    try:
        res = r7.handle(said, "kitchen", satellite="kitchen-echo", heard="follow_up")
    except AttributeError as exc:                   # (a fast path ran: FakeLights has no restore / panic_red)
        res = {"crashed": str(exc)}
    check(f"brain: follow-up {said!r} -> to the brain (ignored), no fast path, nothing saved",
          res.get("ignored") and b7.systems and not mem7.facts, res)
r8, b8, _ = router("ignore")
res = r8.handle("what's the weather tomorrow?", "kitchen", heard="follow_up")
check("brain: follow-up that ASKS the weather still gets the weather fast path", not res.get("ignored") and not b8.systems,
      res)
r9, b9, mem9 = router("remember")
res = r9.handle("remember that guy from the store", "kitchen", heard="follow_up")
check("brain: a follow-up can't save a memory even when the model says 'remember'", res.get("ignored") and not mem9.facts,
      (res, mem9.facts))
r10, b10, mem10 = router("answer")
r10.handle("tell me a joke and ask me if I want another", "kitchen", heard="wake")
r10.handle("yes", "kitchen", heard="follow_up")
check("brain: a follow-up is judged knowing what she just said ('yes' to her question)",
      'You had just said: "sure thing"' in b10.users[-1] and "short reply like 'yes'" in b10.systems[-1], b10.users[-1])
r10.handle("what's two plus two", "kitchen", heard="wake")
check("brain: ... (a wake-word turn isn't given that)", "You had just said" not in b10.users[-1], b10.users[-1])

memx = Memory(Path(tempfile.mkdtemp()) / "memory.json")
mine = memx.add_turn("user", "tv dialogue", "kitchen")
memx.add_turn("user", "a real question", "bedroom")        # (another Echo, while the kitchen's was being judged)
check("memory: dropping an ignored turn works even after another turn was added",
      memx.drop_turn(mine) and [t["text"] for t in memx.turns] == ["a real question"], [t["text"] for t in memx.turns])
check("memory: ... and on disk", "tv dialogue" not in memx.path.read_text(encoding="utf-8"))

print(f"\n==== RESULT: {'ALL PASSED' if not fails else 'FAILURES: ' + str(fails)} ====")
sys.exit(1 if fails else 0)
