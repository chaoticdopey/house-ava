"""The Echo satellite hub — Phase C. One always-on aioesphomeapi connection per Echo.

She is the ESPHome API CLIENT (the role Home Assistant normally plays), connecting OUT to each
flashed Echo at <ip>:6053 with its noise PSK. Run it on an always-on box so the connections
never drop and the satellites never go deaf. No Home Assistant anywhere.

    python -m houseava.satellite add --name kitchen --ip kitchen-echo.local --room kitchen --psk <key>
    python -m houseava.satellite list
    python -m houseava.satellite run          # connect to every configured Echo and serve voice turns

Per voice turn (built against aioesphomeapi 46.x and TECHO5's own docs):
  handle_start  -> return 0 (port 0 = audio comes over the API; None would make the device abort)
                   RUN_START, STT_START
  handle_audio  -> collect 16 kHz / 16-bit LE / mono frames. echod ends a wake-word turn itself when the talking
                   stops; a FOLLOW-UP (mic re-opened after her reply) it leaves to us, like Home Assistant's own
                   detector: STT_VAD_START / STT_VAD_END from speechgate.SpeechTracker. Any turn about to hit
                   echod's max_listen is ended by the hub (STT_VAD_END) so it's judged, not failed with a chime.
  handle_stop   -> STT (faster-whisper) -> noise / "not meant for her" checks (speechgate, the brain's ignore
                   intent): RUN_END alone, no reply, nothing logged - else -> House Ava's router (with
                   THIS Echo's room, so "turn the light off" / "play music" mean here) -> STT_END{text} ->
                   INTENT_START -> INTENT_END -> TTS_START{text} -> reply WAV served by ReplyServer ->
                   TTS_END{url} -> RUN_END
Room-aware by default: a request is handled for the satellite that heard it.
Multi-room: when two Echoes hear the same wake word, the first one wins; the others get
ERROR{duplicate_wake_up_detected} (echod handles it).

Music (houseava/music.py): on every (re)connect the hub finds the Echo's media_player ("speaker"), follows its state
(a song that ends -> the next one) and plays songs on it by URL — media_player_command always on the hub's loop, even
when the router (a worker thread) asks.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import re
import sys
import threading
import time

from .config import Config

log = logging.getLogger("houseava.satellite")

_DEDUP_SECONDS = 1.2        # wake-word hits closer together than this, on different units, = the same person
_MAX_AUDIO = 16000 * 2 * 30  # 30 s of 16 kHz s16 mono — a hard cap so a stuck stream can't eat memory


_FOLLOW_UP_GRACE = 4.0      # a turn starting within this long after her reply should have finished = a follow-up
_FOLLOW_UP_EARLY = 1.0      # ... or this long before (the estimate is rough); any earlier = a wake word over her reply
# what echod acts on itself over a ringing alarm/timer when it hears it (STT_END) - exactly its own rules
# (voice/stopring.go stopsRing, ring/spoken.go SnoozeAsked) - sent at once, not after the brain
_ECHOD_STOP_WORDS = {"stop", "turn", "off", "it", "that", "the", "alarm", "timer", "ringing", "please", "ok", "okay",
                     "now", "shut", "up"}


def server_tz_rule(path: str = "/etc/localtime") -> str:
    """This box's own time zone as a POSIX TZ rule, for the Echo's clock: a TZif (v2+) file ends with the rule
    on its last line. "UTC0" when there's none to read (Windows, a container with no zoneinfo)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return "UTC0"
    if not data.startswith(b"TZif"):
        return "UTC0"
    rule = data.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode("ascii", "replace").strip()
    looks_right = (0 < len(rule) <= 64 and rule.isprintable() and any(c.isdigit() for c in rule)
                   and (rule[0].isalpha() or rule[0] == "<"))
    if not looks_right:
        return "UTC0"
    return rule


def _echod_acts_on(text: str) -> bool:
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    if "snooze" in words:
        return True
    return bool(words) and set(words) <= _ECHOD_STOP_WORDS and ("stop" in words or "off" in words)


class Turn:
    def __init__(self, conversation_id: str, follow_up: bool = False):
        self.conversation_id = conversation_id or ""
        self.audio = bytearray()
        self.received = 0                   # bytes of audio that came in (past the storage cap too)
        self.started = time.time()
        self.done = False
        # follow_up = the Echo re-opened the mic after her reply (no wake word). echod won't end those itself, so the
        # hub's SpeechTracker does (STT_VAD_START / STT_VAD_END). hub_ended = the hub ended this turn: the talking
        # finished, or it ran up to the cutoff just before echod's own limit (which would end it with a failure
        # chime, or - on a follow-up - by going idle and dropping any reply). A follow-up the Echo closed on its own
        # = its follow-up time ran out with nobody talking to her.
        self.follow_up = follow_up
        self.tracker = None
        self.checking = False
        self.hub_ended = False
        self.vad_started = False            # STT_VAD_START went out: echod's max_listen clock restarted...
        self.listen_from = 0                # ... at this much audio (the hub's cutoff counts from here too)
        self.canceled = False               # echod gave up on it while the hub was still working: send nothing more
        self.let_go = asyncio.Event()       # ... (set then, for whoever is waiting to hear it)
        self.stt_sent = False
        self.intercom: "Satellite | None" = None   # a recording for THAT Echo (intercom), not words for her


class SatSpeaker:
    """One Echo's media_player as music.SpeakerPlayer drives it: play_url / command / set_volume / has_media.
    aioesphomeapi's media_player_command is a plain (non-async) send that must run on the hub's loop: from the loop
    it goes straight out; from any other thread (the router's worker, a song-change timer) it's handed to the loop.
    With the Echo disconnected it does nothing but say so in the log."""

    _COMMANDS = ("play", "pause", "stop", "mute", "unmute")

    def __init__(self, sat: "Satellite"):
        self.sat = sat

    def has_media(self) -> bool:
        return self.sat.connected and self.sat.media_key is not None and self.sat.client is not None

    def play_url(self, url: str) -> bool:
        calls = getattr(self.sat.hub, "calls", None)
        if hasattr(calls, "busy") and calls.busy(self.sat):     # a live call owns the speaker (livecall.py)
            log.info("%s: on a call, so no music right now", self.sat.name)
            return False
        return self._send("play " + url.rsplit("/", 1)[-1], media_url=url)

    def command(self, name: str) -> bool:
        from aioesphomeapi import MediaPlayerCommand

        if name not in self._COMMANDS:
            raise ValueError(f"unknown speaker command {name!r}")
        return self._send(name, command=getattr(MediaPlayerCommand, name.upper()))

    def set_volume(self, v: float) -> bool:
        return self._send(f"volume {v:.2f}", volume=max(0.0, min(1.0, float(v))))

    def _send(self, what: str, **kw) -> bool:
        sat, loop = self.sat, self.sat.hub._loop
        if loop is None or not self.has_media():
            log.info("%s: not connected, so no %s", sat.name, what)
            return False

        def go() -> None:
            if not self.has_media():                     # (it dropped while this waited for the loop)
                log.info("%s: not connected, so no %s", sat.name, what)
                return
            try:
                sat.client.media_player_command(sat.media_key, device_id=sat.media_device_id, **kw)
            except Exception as exc:
                log.warning("%s: %s failed: %s", sat.name, what, exc)

        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            go()
        else:
            loop.call_soon_threadsafe(go)
        return True


class Satellite:
    """One Echo: its connection, its room, its current voice turn and its speaker (media_player)."""

    def __init__(self, hub: "SatelliteHub", conf: dict):
        self.hub = hub
        self.conf = dict(conf or {})                 # its own satellites entry (per-Echo overrides: see pref())
        self.name = str(conf.get("name") or conf.get("ip"))
        self.ip = str(conf.get("ip", ""))
        self.psk = conf.get("psk") or None
        self.room = str(conf.get("room", "") or "")
        self.client = None
        self.logic = None                            # its ReconnectLogic
        self.connected = False                       # (client is set before it connects; this says it did)
        self.last_change = time.time()               # when connected last flipped (the watchdog uses it)
        self.forced_at = 0.0                         # when the watchdog last force-dropped it (0 = not since it connected)
        self.turn: Turn | None = None
        self.media_key: int | None = None            # the media_player entity ("speaker" on echod)
        self.media_device_id = 0
        self.presence_key: int | None = None         # the occupancy binary sensor, for "arm the house"
        self.media_state = ""                        # idle / playing / paused, as the Echo last said
        self.volume: float | None = None
        self.muted = False
        self.speaker = SatSpeaker(self)
        self.ents: list = []                         # its entities (on connect), to re-apply settings when they change
        self.bt_pair_key: int | None = None          # Bluetooth pairing mode (a phone can pair and play through it)
        self.mic_mute_key: int | None = None         # echod's "Microphone mute" switch ("stop listening for an hour")
        self.unmute_handle = None                    # the loop timer that turns the mic back on
        self.bt_pairing = False                      # ... on right now, as the Echo last said
        self.bt_phone_ok = False                     # this Echo's firmware lets a PHONE play into it (bluetooth_phone_pairing)
        self.bt_reconnect_key: int | None = None     # the "Bluetooth reconnect phone" button ("connect my phone")
        self.bt_name = ""                            # the name phones see it as
        self.wake_slots: list[str] = []              # the wake word running in each slot, as the Echo last said
        self.reply_sent_at = 0.0                     # when the last spoken reply was handed over (TTS_END)...
        self.reply_end_est = 0.0                     # ...and about when it finishes playing (follow-ups open then)
        self.follow_ups_used = 0                     # follow-ups echod has opened since the last wake word
        self.running: Turn | None = None             # the turn the hub is still working on (transcribing / thinking)
        self.intercom_to: "Satellite | None" = None  # "intercom the kitchen": the next turn here is a recording for it...
        self.intercom_until = 0.0                    # ...if it starts by then (her "Go ahead." opens the mic)
        self.intercom_from = ""                      # the Echo that last intercommed INTO this one ("reply" goes there)

    # ---- the speaker ---------------------------------------------------------------------------
    async def setup_media(self, cli) -> None:
        """Find the Echo's media_player and follow its state. Runs on every (re)connect."""
        from aioesphomeapi import MediaPlayerEntityState, MediaPlayerInfo

        ents, _services = await cli.list_entities_services()
        try:                                              # live calls between the Echoes (livecall.py)
            info0 = await cli.device_info()
            self.hub.calls.learn(self, ents, _services, str(info0.friendly_name or info0.name or ""))
        except Exception as exc:
            log.warning("%s: live-call setup failed: %s", self.name, exc)
        players = [e for e in ents if isinstance(e, MediaPlayerInfo)]
        mp = next((e for e in players if e.object_id == "speaker"), players[0] if players else None)
        if mp is None:
            self.media_key = None
            log.info("%s: no media player, so no music on this one", self.name)
            return
        self.media_key, self.media_device_id = mp.key, mp.device_id

        from aioesphomeapi import BinarySensorInfo, BinarySensorState, ButtonInfo, SwitchInfo, SwitchState
        pres = next((e for e in ents if isinstance(e, BinarySensorInfo) and e.object_id == "presence"), None)
        self.presence_key = pres.key if pres else None
        pd = next((e for e in ents if isinstance(e, SwitchInfo) and e.object_id == "presence_detection"), None)
        if pd is not None:                                # the occupancy sensor only reports when this is on
            try:
                cli.switch_command(pd.key, True)
            except Exception:
                pass
        # "pair my phone" = the phone plays INTO the Echo (A2DP sink): echod's "Bluetooth phone" switch
        # (bluetooth_phone_pairing) makes the Echo findable by a phone. bluetooth_pairing is the OTHER direction -
        # the Echo scanning for earbuds / a speaker to play OUT to ("put your earbuds in pairing mode") - never a phone.
        bt = next((e for e in ents if isinstance(e, SwitchInfo) and e.object_id == "bluetooth_phone_pairing"), None) \
            or next((e for e in ents if isinstance(e, SwitchInfo) and e.object_id == "bluetooth_pairing"), None)
        self.bt_pair_key = bt.key if bt else None
        mm = next((e for e in ents if isinstance(e, SwitchInfo) and e.object_id == "mic_mute"), None)
        self.mic_mute_key = mm.key if mm else None
        self.bt_phone_ok = any(isinstance(e, SwitchInfo) and e.object_id == "bluetooth_phone_pairing" for e in ents)
        rb = next((e for e in ents if isinstance(e, ButtonInfo) and e.object_id == "bluetooth_reconnect_phone"), None)
        self.bt_reconnect_key = rb.key if rb else None   # "connect my phone": an already-paired phone reconnects
        # the volume buttons (echod reports each press): during an intruder alert a press stops the alarm music -
        # over the blast the wake word can't be heard, and the volume button is what he reaches for (10-20)
        self.vol_button_keys = {e.key for e in ents if type(e).__name__ == "EventInfo"
                                and getattr(e, "object_id", "") in ("button_volume_down", "button_volume_up")}
        try:
            info = await cli.device_info()
            self.bt_name = str(info.friendly_name or info.name or "")   # (echod's Bluetooth alias = the friendly name)
        except Exception:
            pass

        def on_state(st) -> None:
            try:
                self.hub.calls.on_state(self, st)            # the phone sensor / Drop In / Do Not Disturb
            except Exception:
                log.exception("%s: call state handling failed", self.name)
            if isinstance(st, MediaPlayerEntityState) and st.key == self.media_key:
                self.on_media_state(st)
            elif getattr(st, "key", None) in getattr(self, "vol_button_keys", ()) and \
                    type(st).__name__ == "Event" and getattr(st, "event_type", "") in ("press", "hold"):
                self.hub.alert_button(self)
            elif self.bt_pair_key is not None and isinstance(st, SwitchState) and st.key == self.bt_pair_key:
                self.bt_pairing = bool(st.state)
            elif self.presence_key is not None and isinstance(st, BinarySensorState) and st.key == self.presence_key:
                try:
                    self.hub.security.on_presence(self.room, bool(st.state))
                except Exception:
                    log.exception("%s: presence handling failed", self.name)
                try:
                    self.hub.routines.on_presence(self.room, bool(st.state))     # "when someone's in the kitchen"
                except Exception:
                    log.exception("%s: presence routines failed", self.name)

        cli.subscribe_states(on_state)
        log.info("%s: speaker found (%s)%s", self.name, mp.object_id or mp.name,
                 "; presence sensor ready" if self.presence_key else "")
        self._apply_audio_prefs(cli, ents)

    def pref(self, name: str, default=None):
        """This Echo's own value from its satellites entry, else the global voice.<name>. Speakers differ: the Show 8's
        factory tuning already lifts the bass, so the +4/+4 that helps the tiny Show 5 made it boomy AND quieter
        (echod's shelves sit before its protection compressor, which then clamps down) - 10-15."""
        v = self.conf.get(name)
        return v if v is not None else self.hub.cfg.get(f"voice.{name}", default)

    def _apply_audio_prefs(self, cli, ents, volume: bool = True) -> None:
        """On every (re)connect (and when the settings change): max the volume, apply the loudness EQ, follow-up
        listening, and how sure/how long/how often she listens - so the little Show 5 is as loud as it gets, keeps
        listening after a reply, and doesn't answer the room - all across reboots."""
        self.ents = list(ents)
        cfg = self.hub.cfg
        vol = cfg.get("voice.echo_volume", None) if volume else None
        if vol is not None:
            try:
                self.speaker.set_volume(float(vol))
                log.info("%s: volume set to %.0f%%", self.name, float(vol) * 100)
            except Exception as exc:
                log.warning("%s: couldn't set volume: %s", self.name, exc)
        from aioesphomeapi import NumberInfo, SelectInfo, SwitchInfo
        nums = [e for e in ents if isinstance(e, NumberInfo)]
        by_id = {e.object_id: e for e in nums}
        switches = {e.object_id: e for e in ents if isinstance(e, SwitchInfo)}
        selects = {e.object_id: e for e in ents if isinstance(e, SelectInfo)}

        def set_num(e, want, label):
            try:
                v = max(e.min_value, min(e.max_value, float(want)))
                cli.number_command(e.key, v)
                log.info("%s: %s set to %+g", self.name, label, v)
            except Exception as exc:
                log.warning("%s: couldn't set %s: %s", self.name, label, exc)

        def set_switch(oid, want):
            e = switches.get(oid)
            if e is None or want is None:
                return
            try:
                cli.switch_command(e.key, bool(want))
                log.info("%s: %s %s", self.name, oid, "on" if want else "off")
            except Exception as exc:
                log.warning("%s: couldn't set %s: %s", self.name, oid, exc)

        def set_select(oid, want):
            e = selects.get(oid)
            if e is None or not want:
                return
            if want not in list(e.options):
                log.warning("%s: %s has no option %r (has %s)", self.name, oid, want, list(e.options))
                return
            try:
                cli.select_command(e.key, want)
                log.info("%s: %s set to %s", self.name, oid, want)
            except Exception as exc:
                log.warning("%s: couldn't set %s: %s", self.name, oid, exc)

        for cfgname, oid in (("bass", "bass"), ("treble", "treble"), ("stop_word", "stop_word_sensitivity")):
            want = self.pref(cfgname)
            if cfgname == "stop_word" and want is not None:
                try:
                    if float(want) <= 0:
                        want = 1.0                 # echod's "off" is the top of the range (0.5-1.0), never 0 -> 0.5
                except (TypeError, ValueError):
                    pass                           # not a number: set_num logs it and the rest still applies
            if want is not None and oid in by_id:
                set_num(by_id[oid], want, oid)
        # over loud music the wake word that nearly fired can't be rescued, but echod can duck the track for a few
        # seconds so the NEXT "Alexa" lands (off by default on the Show builds; it got ignored over a song)
        set_switch("duck_on_near_miss", self.pref("duck_on_near_miss"))
        # music vs a turn (it wasn't hearing people with music playing): ducked, the track under
        # his voice reads as more speech to the Echo's end-of-speech, so every command over music ran to the 8.5 s
        # cutoff and whisper got one word out of it. music_on_turn "Pause" stops the song for the turn and picks it up
        # after. echo_canceller "WebRTC" = echod's stronger canceller + suppressor for the wake word over playback.
        set_select("media_on_turn", self.pref("music_on_turn"))
        if self.pref("music_duck_db") is not None and "media_duck_level" in by_id:
            set_num(by_id["media_duck_level"], self.pref("music_duck_db"), "media_duck_level")
        set_select("microphone_echo_canceller", self.pref("echo_canceller"))
        # speaker_eq = the vendor's tuning (a FIR lifting 125-400 Hz by up to 27 dB + its compressor). The bass shelf
        # only reaches -6 and sits in front of it, so on the Show 8 "way too much bass" at bass -6 is the tuning itself:
        # off sends the music as recorded (and bass/treble then do nothing - they're a stage of the tuning).
        set_switch("speaker_eq", self.pref("speaker_eq"))
        # the microphone itself - how well she hears across a room. mic_gain = the array's analog gain in dB (echod's
        # vendor default 20; more = hears further, applied live); mic_mixing = all seven mics or the centre one;
        # mic_leveling = echod's auto-leveler lifting quiet, distant speech; mic_denoise = its noise reduction.
        if cfg.get("voice.mic_gain", None) is not None and "microphone_gain" in by_id:
            set_num(by_id["microphone_gain"], cfg.get("voice.mic_gain"), "microphone_gain")
        set_select("microphone_mixing", cfg.get("voice.mic_mixing", None))
        set_switch("microphone_leveling", cfg.get("voice.mic_leveling", None))
        set_switch("microphone_noise_reduction", cfg.get("voice.mic_denoise", None))
        # follow-up: keep the mic open N seconds after each reply so you can talk again without the wake
        # word. echod does it on-device (per-wake-word slots follow_up_1 / follow_up_2); 0 turns it off.
        fu = cfg.get("voice.follow_up", None)
        if fu is not None:
            for e in nums:
                if e.object_id.startswith("follow_up_"):      # NOT follow_ups_* (the count) or the tone select
                    set_num(e, fu, e.object_id)
        # per wake-word slot (_1, _2): how many follow-ups one wake opens (echod's 0 = no limit), the longest a turn
        # may record - and how sure a wake must be, which depends on the slot's WORD (apply_wake_thresholds)
        self.apply_wake_thresholds(cli)
        for cfgname, prefix in (("follow_ups", "follow_ups_"), ("max_listen", "max_listen_")):
            want = cfg.get(f"voice.{cfgname}", None)
            if want is None:
                continue
            for e in nums:
                if e.object_id.startswith(prefix):
                    set_num(e, want, e.object_id)
        # the sound a follow-up opens with ("None" = silent, like Alexa's follow-up mode)
        tone = cfg.get("voice.follow_up_tone", None)
        if tone:
            for e in ents:
                if isinstance(e, SelectInfo) and e.object_id.startswith("follow_up_tone_"):
                    if tone not in list(e.options):
                        log.warning("%s: %s has no option %r (has %s)", self.name, e.object_id, tone, list(e.options))
                        continue
                    try:
                        cli.select_command(e.key, tone)
                        log.info("%s: %s set to %s", self.name, e.object_id, tone)
                    except Exception as exc:
                        log.warning("%s: couldn't set %s: %s", self.name, e.object_id, exc)

    def wake_threshold_for(self, slot: int) -> float | None:
        """How sure the Echo must be before slot `slot` (0-based) wakes. echod ignores a model's own cutoff and uses
        the per-slot threshold on that model's raw scale (lib/wake/models.go), so it's per WORD: the custom "Hey Ava"
        (calibrated at 0.43 by its trainer) gets wake.threshold, the stock words (Alexa) voice.wake_threshold."""
        cfg = self.hub.cfg
        word = self.wake_slots[slot] if 0 <= slot < len(self.wake_slots) else ""
        ava = bool(word and word == str(cfg.get("wake.id", "hey_ava")))
        key = "wake.threshold" if ava else "voice.wake_threshold"
        # (10-19: this Echo's own value first - the one by the TV / a phone playing shows false-wakes, the other
        # doesn't; "you keep waking up" tunes just the Echo that heard it)
        v = self.conf.get("hey_ava_threshold" if ava else "wake_threshold")
        if v is None:
            v = cfg.get(key, None)
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    def apply_wake_thresholds(self, cli) -> None:
        from aioesphomeapi import NumberInfo

        for e in self.ents:
            if not (isinstance(e, NumberInfo) and e.object_id.startswith("wake_threshold_")):
                continue
            try:
                slot = int(e.object_id.rsplit("_", 1)[1]) - 1
            except ValueError:
                continue
            want = self.wake_threshold_for(slot)
            if want is None:
                continue
            try:
                v = max(e.min_value, min(e.max_value, want))
                cli.number_command(e.key, v)
                word = self.wake_slots[slot] if slot < len(self.wake_slots) else "?"
                log.info("%s: %s (%s) set to %+g", self.name, e.object_id, word, v)
            except Exception as exc:
                log.warning("%s: couldn't set %s: %s", self.name, e.object_id, exc)

    def set_bt_pairing(self, on: bool) -> bool:
        """Bluetooth pairing mode on/off (echod turns it off by itself after 3 minutes, or once a phone has paired).
        Sent on the hub's loop, from any thread. -> sent?"""
        loop, key = self.hub._loop, self.bt_pair_key
        if loop is None or key is None or not self.connected or self.client is None:
            return False

        def go() -> None:
            if not self.connected or self.client is None:
                return
            try:
                self.client.switch_command(key, bool(on))
                log.info("%s: Bluetooth pairing %s", self.name, "on" if on else "off")
            except Exception as exc:
                log.warning("%s: Bluetooth pairing %s failed: %s", self.name, "on" if on else "off", exc)

        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            go()
        else:
            loop.call_soon_threadsafe(go)
        return True

    def set_mic_mute(self, on: bool) -> bool:
        """echod's microphone mute on/off (muted = no wake word at all, the ring shows it). Sent on the hub's loop, from
        any thread. -> sent?"""
        loop, key = self.hub._loop, self.mic_mute_key
        if loop is None or key is None or not self.connected or self.client is None:
            return False

        def go() -> None:
            if not self.connected or self.client is None:
                return
            try:
                self.client.switch_command(key, bool(on))
                log.info("%s: microphone %s", self.name, "muted" if on else "unmuted")
            except Exception as exc:
                log.warning("%s: microphone mute %s failed: %s", self.name, on, exc)

        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            go()
        else:
            loop.call_soon_threadsafe(go)
        return True

    def press_bt_reconnect(self) -> bool:
        """Press echod's "Bluetooth reconnect phone" button: the last paired phone connects again (its Bluetooth must be
        on). Sent on the hub's loop, from any thread. -> sent?"""
        loop, key = self.hub._loop, self.bt_reconnect_key
        if loop is None or key is None or not self.connected or self.client is None:
            return False

        def go() -> None:
            if not self.connected or self.client is None:
                return
            try:
                self.client.button_command(key)
                log.info("%s: Bluetooth reconnect phone pressed", self.name)
            except Exception as exc:
                log.warning("%s: Bluetooth reconnect phone failed: %s", self.name, exc)

        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            go()
        else:
            loop.call_soon_threadsafe(go)
        return True

    def on_media_state(self, st) -> None:
        state = getattr(st.state, "name", str(st.state)).lower()
        self.media_state, self.volume, self.muted = state, float(st.volume), bool(st.muted)
        music = self.hub.music
        if music is not None and state in ("idle", "playing", "paused", "announcing"):
            music.on_state(self.name, state, self.volume, self.muted)    # (announcing: a reply, never "it resumed")

    # ---- events --------------------------------------------------------------------------------
    def event(self, kind: str, data: dict | None = None) -> None:
        from aioesphomeapi import VoiceAssistantEventType as E

        etype = getattr(E, "VOICE_ASSISTANT_" + kind)
        try:
            self.client.send_voice_assistant_event(etype, {k: str(v) for k, v in (data or {}).items()})
        except Exception as exc:                        # the device went away mid-turn
            log.warning("%s: event %s failed: %s", self.name, kind, exc)

    # ---- the voice callbacks -------------------------------------------------------------------
    def _is_follow_up(self, now: float) -> bool:
        """echod sends the wake phrase on follow-ups too, so tell them apart by timing - a follow-up opens the moment
        her reply finishes playing - but only when echod would really open one: follow-up on, not past
        voice.follow_ups since the last wake word, and not while she's still talking (a wake word then is a barge-in,
        a normal turn). A wake word that still lands in the window is caught in run_turn: echod ends that turn itself
        with its own end of speech, which it never does for a follow-up."""
        cfg = self.hub.cfg
        try:
            window = float(cfg.get("voice.follow_up", 0) or 0)
            limit = int(cfg.get("voice.follow_ups", 0) or 0)
        except (TypeError, ValueError):
            return False
        if window <= 0 or (limit > 0 and self.follow_ups_used >= limit):
            return False
        sent, end = self.reply_sent_at, self.reply_end_est
        return sent > 0 and end - _FOLLOW_UP_EARLY <= now <= end + _FOLLOW_UP_GRACE

    async def handle_start(self, conversation_id, flags, audio_settings, wake_word_phrase) -> int:
        now = time.time()
        follow_up = self._is_follow_up(now)
        self.reply_sent_at = self.reply_end_est = 0.0   # (one reply opens at most one follow-up window)
        self.follow_ups_used = self.follow_ups_used + 1 if follow_up else 0
        # which word woke her (no speech in it) - what wake strictness gets tuned on
        log.info("%s: turn started by %s", self.name, "a follow-up" if follow_up else repr(wake_word_phrase or "?"))
        # an intercom recording? ("intercom the kitchen" -> "Go ahead." opened this mic via start_conversation)
        ic = self.intercom_to if (self.intercom_to is not None and now <= self.intercom_until) else None
        self.intercom_to, self.intercom_until = None, 0.0
        if ic is not None or self.hub.claim_wake(self):     # (a recording is never "another room answering")
            self.turn = Turn(conversation_id, follow_up=follow_up)
            if ic is not None:
                # a recording for the other Echo, never a follow-up: echod ends it with its own end of speech, like a
                # wake-word turn, and run_turn delivers the audio instead of transcribing it
                self.turn.intercom, self.turn.follow_up = ic, False
                log.info("%s: recording an intercom message for %s", self.name, ic.name)
            elif follow_up:
                self.turn.tracker = self.hub.new_tracker()
            else:
                # a wake-word turn ends when the person who woke her stops talking (1.2 s quiet) - echod's own end of
                # speech often never fires over room noise, and the turn then recorded whoever spoke NEXT (10-18:
                # someone else in the room, 2.3 s after the speaker had finished). Also answers sooner.
                quiet = self.hub.wake_end_silence()
                if quiet > 0:
                    self.turn.tracker = self.hub.new_tracker(end_silence_s=quiet)
            self.event("RUN_START")
            self.event("STT_START")
        else:
            self.turn = None
            self.event("ERROR", {"code": "duplicate_wake_up_detected", "message": "Another room is answering."})
        return 0                                        # MUST be 0, not None (None = error on the device)

    async def handle_audio(self, data: bytes, data2: bytes | None = None) -> None:
        t = self.turn
        if t is None or t.done:
            return
        t.received += len(data)
        if len(t.audio) < _MAX_AUDIO:
            t.audio.extend(data)
        if t.hub_ended:
            return
        # about to hit echod's max_listen: end it here instead, so the words get judged (and usually ignored - a turn
        # this long is the TV or the room) rather than echod sounding its failure chime. On a follow-up echod's clock
        # starts when someone starts talking (STT_VAD_START); one nobody talks into is left to echod's follow-up timer.
        if (not t.follow_up or t.vad_started) and t.received - t.listen_from >= self.hub.cutoff_bytes():
            self.end_speech(t, "ran to the cutoff")
        elif t.tracker is not None and not t.checking and t.tracker.due(len(t.audio)):
            t.checking = True
            asyncio.ensure_future(self._track_speech(t))

    def end_speech(self, t: Turn, why: str) -> None:
        """Tell echod the talking is over (STT_VAD_END, as Home Assistant's detector would): it stops recording and
        thinks, and the hub answers - or quietly doesn't."""
        if t.hub_ended or t.done or self.turn is not t:
            return
        t.hub_ended = True
        self.event("STT_VAD_END")
        log.info("%s: %s turn %s (%.1fs in)", self.name, "follow-up" if t.follow_up else "wake", why,
                 len(t.audio) / 32000)

    async def _track_speech(self, t: Turn) -> None:
        """A follow-up turn's end of speech, as Home Assistant would report it: STT_VAD_START when someone starts
        talking (echod then allows the full max_listen), STT_VAD_END when they've finished (echod stops recording and
        the turn is answered). Without these a follow-up records the room until echod gives up."""
        try:
            events = await asyncio.to_thread(t.tracker.check, bytes(t.audio))
        except Exception:
            log.exception("%s: follow-up speech check failed", self.name)
            events = []
        finally:
            t.checking = False
        if t.done or t.hub_ended or self.turn is not t:  # (the turn ended while this ran)
            return
        for ev in events:
            if ev == "start" and not t.follow_up:
                t.vad_started = True                     # (a wake turn: echod already knows someone's talking)
            elif ev == "start":
                t.vad_started, t.listen_from = True, t.received
                self.event("STT_VAD_START")
            elif ev == "end":
                self.end_speech(t, "speech ended")

    async def handle_stop(self, abort: bool) -> None:
        t, self.turn = self.turn, None
        if t is None or t.done:
            # echod gave up on (or moved on from) a turn the hub is still working on - its timeout, the action button,
            # a new wake word while it was thinking: close that run NOW (a new turn may be waiting on it) and send
            # nothing more for it, so nothing lands in whatever turn comes next
            r = self.running
            if abort and r is not None and not r.canceled:
                r.canceled = True
                r.let_go.set()
                self.event("RUN_END")
                log.info("%s: the Echo let go of the turn being worked on; dropped it", self.name)
            return
        t.done = True
        if abort:
            self.event("RUN_END")
            return
        self.running = t
        try:
            await self.hub.run_turn(self, t)
        finally:
            if self.running is t:
                self.running = None


class SatelliteHub:
    """Holds one connection per configured Echo and runs the voice loop for each."""

    def __init__(self, cfg: Config | None = None, router=None, transcribe=None, synth=None, replies=None, music=None):
        from .app import build_router
        from .http_server import ReplyServer

        self.cfg = cfg or Config()
        self.router = router or build_router(self.cfg, announce=self._announce)
        self._transcribe = transcribe          # test seams: transcribe(pcm) -> str, synth(text) -> wav bytes
        self._synth = synth
        self.replies = replies or ReplyServer(self.cfg)
        self.music = music                     # music.MusicService: made in run() (music.enabled), or a test's
        from .convlog import ConversationLog
        from .security import Security
        self.convlog = ConversationLog()       # a rolling log of what's asked of each Echo, for the app
        self.security = Security(self.cfg, convlog=self.convlog, announce=self._announce)  # "arm the house"
        from .routines import RoutineEngine
        self.routines = RoutineEngine(self.cfg, self.router, hub=self)   # automations made in the app (routines.py)
        if hasattr(self.router, "__dict__"):
            self.router.security = self.security
            self.router.bluetooth = self.bluetooth_cmd       # "pair my phone": play your own music through the Echo
            self.router.intercom = self.intercom_cmd         # "announce ..." / "intercom the kitchen" / "reply ..."
            self.router.microphone = self.mic_cmd            # "turn your mic up": hear across the room
            self.router.routines = self.routines             # "good night" -> that routine
            self.router.live_call = self.call_cmd            # "call the bedroom" - live two-way (livecall.py)
            self.router.mic_mute = self.mute_cmd             # "stop listening for an hour" (echod mic_mute)
            self.router.wake_tune = self.wake_cmd            # "you keep waking up" - this Echo own strictness
        from .livecall import LiveCalls
        self.calls = LiveCalls(self)
        self.sats: list[Satellite] = []
        self._last_wake: tuple[float, Satellite | None] = (0.0, None)
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- speech --------------------------------------------------------------------------------
    def transcribe(self, pcm: bytes) -> str:
        if self._transcribe is None:
            from .stt import Transcriber

            self._stt = Transcriber(self.cfg)
            self._transcribe = self._stt.transcribe_pcm16
        return self.listen_again(pcm, self._transcribe(pcm))

    # the house's own words, for a second listen
    _LIGHTISH = re.compile(r"\b(?:lights?|lamps?|bulbs?|lite|life|mic|might|living|dining|bedroom|denim\w*|daniel|"
                           r"kitchen)\b", re.I)

    def listen_again(self, pcm: bytes, text: str) -> str:
        """A light command whisper garbled on the far-field mic ("You're not dining room light." / "Turn on Daniel
        Light" / "bedroom mic one" - 10-19 replays of the kept turns): listen to the SAME audio again with the house's
        words as hotwords, and use that only when it reads as a clear light command and the first didn't. Never on
        speech without a light / room word (hotwords alone made it hallucinate room names on room noise - 10-17)."""
        stt = getattr(self, "_stt", None)
        if not text or stt is None or not self.cfg.get("stt.listen_again", True) or not self._LIGHTISH.search(text):
            return text
        lights = getattr(self.router, "lights", None)
        if lights is None:
            return text

        def parsed(t: str) -> list:
            try:
                return list(lights.parse_all(t) or [])
            except Exception:
                return []

        def clear(cmds: list) -> bool:              # an explicit command for known lights (not a guessed toggle)
            return bool(cmds) and all(c.action not in ("toggle", "refuse") and not getattr(c.target, "unknown", "")
                                      and not getattr(c.target, "ask", None) for c in cmds)

        first = parsed(text)
        if clear(first):
            return text
        rooms = [str(r).strip() for r in (self.cfg.get("lights.rooms", {}) or {}).keys()] or \
            ["living room", "dining room", "bedroom"]
        hot = ", ".join(["Ava", "turn on", "turn off", "lights", "light"] + [r.capitalize() for r in rooms] +
                        ["light one", "light two"])
        t0 = time.time()
        try:
            again = stt.transcribe_pcm16(pcm, hotwords=hot, beam_size=8)
        except Exception as exc:
            log.warning("stt: second listen failed: %s", exc)
            return text
        again = re.sub(r"\s*,\s*", " ", again or "").strip()         # ("Turn on, Living room, light")
        if again and again != text and clear(parsed(again)):
            log.info("stt: listened again (%.1fs): %r -> %r", time.time() - t0, text, again)
            return again
        return text

    def synth(self, text: str) -> bytes:
        if self._synth is None:
            from . import tts

            self._synth = lambda s: tts.synth_wav(s, self.cfg)
        return self._synth(text)

    # ---- multi-room ----------------------------------------------------------------------------
    def claim_wake(self, sat: Satellite) -> bool:
        """True if `sat` should answer: no OTHER unit started a turn in the last moment."""
        now = time.time()
        last_t, last_sat = self._last_wake
        if last_sat is not None and last_sat is not sat and now - last_t < _DEDUP_SECONDS:
            return False
        self._last_wake = (now, sat)
        return True

    # ---- one voice turn ------------------------------------------------------------------------
    @staticmethod
    def _end_quietly(sat: Satellite, why: str) -> None:
        """End a turn with no reply, no error chime and no red ring: RUN_END alone puts echod straight back to idle
        (an ERROR is its "something failed" sound + light - wrong for "nobody was talking to me"). The words are
        never logged: speech that wasn't for her is nobody's business."""
        sat.event("RUN_END")
        log.info("%s: %s - ended quietly", sat.name, why)

    STOP_WAIT = 0.6     # how long a follow-up the hub didn't end waits to hear whether echod's timer ended it

    def _keep_audio(self, sat: Satellite, turn: Turn, kind: str, text: str) -> None:
        """Keep what each voice turn heard (WAV + what whisper made of it) so the speech model can be
        tuned on REAL Echo audio. stt.keep_audio (off by default), newest stt.keep_audio_max (200, ~30 MB) kept in
        ~/.houseava/turn_audio/, older ones deleted; local to this box only. Never the intercom (that's a message)."""
        if not bool(self.cfg.get("stt.keep_audio", False)) or not turn.audio:
            return
        try:
            import json
            from .config import data_dir
            from .tts import _wrap_wav
            d = data_dir() / "turn_audio"
            d.mkdir(parents=True, exist_ok=True)
            stem = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}-" + \
                "".join(ch if ch.isalnum() else "_" for ch in (sat.room or sat.name))
            (d / f"{stem}.wav").write_bytes(_wrap_wav(bytes(turn.audio), 16000))
            (d / f"{stem}.json").write_text(json.dumps({"t": time.time(), "satellite": sat.name, "room": sat.room,
                                                        "kind": kind, "heard": text,
                                                        "seconds": round(len(turn.audio) / 32000, 2)}))
            keep = max(10, int(self.cfg.get("stt.keep_audio_max", 200) or 200))
            wavs = sorted(d.glob("*.wav"))
            for old in wavs[:-keep]:
                old.unlink(missing_ok=True)
                old.with_suffix(".json").unlink(missing_ok=True)
        except Exception as exc:
            log.warning("keeping the turn's audio failed: %s", exc)

    async def run_turn(self, sat: Satellite, turn: Turn) -> None:
        from .speechgate import is_noise, speakable, wav_seconds

        if turn.intercom is not None:               # the mic was open for an intercom message: deliver it, don't answer it
            if sat.running is turn:                 # hand it off: echod sends a routine Stop after our RUN_END, which must
                sat.running = None                  # not "cancel" a delivery that is just taking its time
            await self._deliver_intercom(sat, turn)
            return

        gate = bool(self.cfg.get("voice.ignore_chatter", True))
        if turn.follow_up and not turn.hub_ended:
            # A follow-up the hub didn't end. If echod's follow-up timer ran out (nobody talked to her), it goes idle:
            # "end of audio" and then, right behind it, "stop" (conversation.go idle()) - which cancels this turn. If no
            # "stop" comes, echod ended it with its own end of speech, which it only runs on a WAKE-WORD turn (a wake
            # word just after her reply, taken for a follow-up): answer it as one.
            try:
                await asyncio.wait_for(turn.let_go.wait(), self.STOP_WAIT)
            except asyncio.TimeoutError:
                pass
            if turn.canceled:
                log.info("%s: follow-up: nothing said - ended quietly", sat.name)
                return                                          # (RUN_END already went with the cancel)
            turn.follow_up, sat.follow_ups_used = False, 0
        kind = "follow-up" if turn.follow_up else "wake"
        try:
            text = (await asyncio.to_thread(self.transcribe, bytes(turn.audio))).strip()
            self._keep_audio(sat, turn, kind, text)
        except Exception as exc:
            if turn.canceled:
                return
            log.exception("%s: speech-to-text failed", sat.name)
            sat.event("ERROR", {"code": "stt-failed", "message": str(exc)[:120]})
            sat.event("RUN_END")
            return
        if turn.canceled:                       # (echod let go of it meanwhile: it's been closed, act on nothing)
            return
        if not text:
            return self._end_quietly(sat, f"{kind} turn: no words")
        if gate and is_noise(text):
            return self._end_quietly(sat, f"{kind} turn: only noise (whisper filler)")
        if not turn.follow_up and _echod_acts_on(text):
            # echod acts on the words itself over a ringing alarm/timer ("snooze for ten minutes", "stop") - only for
            # a few seconds after the wake word silenced it, so they go now, not after the brain
            sat.event("STT_END", {"text": text})
            turn.stt_sent = True
        heard = ("follow_up" if turn.follow_up else "wake") if gate else None
        try:
            res = await asyncio.to_thread(self._ask_router, text, sat, heard)
            if (res or {}).get("ignored"):
                if turn.canceled:
                    return
                return self._end_quietly(sat, f"{kind} turn: {len(text.split())} words not meant for her")
            say = speakable(str((res or {}).get("say", "") or "Done.")) or "Done."
        except Exception as exc:
            log.exception("%s: the brain failed on a %s turn", sat.name, kind)
            say = "Sorry, my brain hiccupped on that one."
            res = {"ok": False, "say": say, "error": str(exc)}
        if (res or {}).get("silent"):
            # done without a word (intruder alert / back to normal): the turn just ends - no reply, no chime
            self.convlog.add("voice", sat.room, sat.name, text, "(done quietly) " + str(res.get("say", "")))
            if turn.canceled:
                return
            if not turn.stt_sent:
                sat.event("STT_END", {"text": text})
            sat.event("RUN_END")
            log.info("%s (%s): %r -> done quietly (%s)", sat.name, sat.room or "no room", text, res.get("say", ""))
            return
        if (res or {}).get("intercom_open"):        # "intercom the kitchen": say "Go ahead." and open the mic for the message
            await self._open_intercom(sat, turn, text, res)
            return
        self.convlog.add("voice", sat.room, sat.name, text, say)   # what was asked of this Echo
        if turn.canceled:
            log.info("%s: the Echo let go of the turn before the reply; not speaking it", sat.name)
            return
        if not turn.stt_sent:
            sat.event("STT_END", {"text": text})
        sat.event("INTENT_START")
        sat.event("INTENT_END", {"conversation_id": turn.conversation_id, "continue_conversation": "0"})
        sat.event("TTS_START", {"text": say})
        try:
            wav = await asyncio.to_thread(self.synth, say)
            if turn.canceled:
                return
            url = self.replies.publish(wav, reach_ip=sat.ip)
            now = time.time()
            sat.reply_sent_at, sat.reply_end_est = now, now + 0.5 + wav_seconds(wav)   # (+ fetch & decode)
            sat.event("TTS_END", {"url": url})
        except Exception as exc:
            if turn.canceled:
                return
            log.exception("%s: text-to-speech failed", sat.name)
            sat.event("ERROR", {"code": "tts-failed", "message": str(exc)[:120]})
        sat.event("RUN_END")
        log.info("%s (%s): %r -> %r", sat.name, sat.room or "no room", text, say)

    def _ask_router(self, text: str, sat: Satellite, heard: str | None = None) -> dict:
        """router.handle(text, room, satellite=<this Echo>, heard=<"wake"/"follow_up">) — a router that doesn't take
        those keywords gets what it does take."""
        try:
            params = inspect.signature(self.router.handle).parameters
            anykw = any(p.kind is p.VAR_KEYWORD for p in params.values())
            kw = {k: v for k, v in (("satellite", sat.name), ("heard", heard))
                  if (k in params or anykw) and not (k == "heard" and v is None)}
        except (TypeError, ValueError):
            kw = {}
        return self.router.handle(text, sat.room, **kw)

    # ---- Bluetooth: a phone playing its own music through an Echo --------------------------------
    def bluetooth_cmd(self, action: str, satellite: str = "", room: str = "", named: bool = False) -> dict:
        """"pair" = pairing mode on that Echo for 3 minutes (a phone pairs, is trusted, and from then on reconnects by
        itself and plays through it); "stop" = pairing mode off. Which Echo: the one that heard it, else the one in
        `room`, else the only one - unless the room was `named` ("in the bedroom"): then that room's or none. A phone
        that's playing can only be let go from the phone (echod's disconnect button is for speakers / earbuds it plays
        OUT to)."""
        in_room = next((s for s in self.sats if room and s.room.lower() == room.lower()), None)
        if named:
            sat = in_room
            if sat is None:
                return {"ok": False, "say": f"There's no Echo in the {room}."}
        else:
            sat = next((s for s in self.sats if satellite and s.name == satellite), None) or in_room \
                or (self.sats[0] if len(self.sats) == 1 else None)
        if sat is None:
            return {"ok": False, "say": "Which Echo? Say it to the one you want, or tell me the room."}
        where = f"the {sat.room} Echo" if sat.room else sat.name
        if not sat.connected:
            return {"ok": False, "say": f"I can't reach {where} right now."}
        if sat.bt_pair_key is None:
            return {"ok": False, "say": f"{where[0].upper() + where[1:]} doesn't have Bluetooth."}
        if action == "connect":                          # an already-paired phone: reconnect, no pairing dance
            if sat.bt_reconnect_key is None:
                action = "pair"                          # (this Echo's firmware can't take a phone that way: offer pairing)
            elif not sat.press_bt_reconnect():
                return {"ok": False, "say": f"I couldn't tell {where} to reconnect your phone."}
            else:
                return {"ok": True, "say": "Reconnecting your phone. Make sure its Bluetooth is on; if it was never "
                                           "paired with me, say pair my phone instead."}
        if action == "pair":
            if not sat.bt_phone_ok:
                return {"ok": False, "say": f"{where[0].upper() + where[1:]}'s firmware can't take a phone over Bluetooth "
                                            f"- only earbuds or a speaker. An Echo on TECHO5 1.2.15 or newer can."}
            if self.music is not None:                   # our queue off, so it can't pick up again after their music
                p = self.music.players.get(sat.name)
                if p is not None and p.has_music():
                    p.stop()
            if not sat.set_bt_pairing(True):
                return {"ok": False, "say": f"I couldn't turn on Bluetooth on {where}."}
            name = (sat.bt_name or "echo show").replace("-", " ").replace("_", " ")
            return {"ok": True, "say": f"Bluetooth pairing is on for three minutes. On your phone, open Bluetooth and "
                                       f"pick {name}. If a code pops up, it matches the one on my screen. After that "
                                       f"your phone connects by itself whenever it's nearby."}
        sat.set_bt_pairing(False)
        if action == "disconnect":
            return {"ok": True, "say": "Pairing's off. To unhook a phone that's playing, turn Bluetooth off on the "
                                       "phone or tap disconnect there."}
        return {"ok": True, "say": "Bluetooth pairing's off."}

    # ---- the microphone: "turn your mic up" --------------------------------------------------------
    MIC_STEP = 6                       # dB per "up" / "down" (about twice as loud to the wake-word detector)

    def mic_cmd(self, action: str, value=None) -> dict:
        """"up" / "down" (by MIC_STEP dB), "set" (to `value` dB), "what". Changes the Echo's analog mic gain live
        (every Echo - one setting) and remembers it; the app's slider shows the new value."""
        try:
            cur = int(float(self.cfg.get("voice.mic_gain", 20) or 20))
        except (TypeError, ValueError):
            cur = 20
        if action == "what":
            return {"ok": True, "say": f"My mic gain is {cur} dB. The factory setting is 20, and it goes up to 59."}
        if action == "set":
            try:
                want = int(float(value))
            except (TypeError, ValueError):
                return {"ok": False, "say": "Tell me a number from 0 to 59."}
        else:
            want = cur + (self.MIC_STEP if action == "up" else -self.MIC_STEP)
        want = max(0, min(59, want))
        if want == cur:
            edge = "as high as it goes" if cur >= 59 else "as low as it goes" if cur <= 0 else "already there"
            return {"ok": True, "say": f"My mic's {edge}, at {cur} dB."}
        self.cfg.set("voice.mic_gain", want)
        try:
            self.cfg.save()
        except Exception:
            log.exception("couldn't save the mic gain")
        n = self.reapply_prefs(volume=False)
        log.info("mic gain %d -> %d dB (%s) on %d Echo(es)", cur, want, action, n)
        how = "up" if want > cur else "down"
        return {"ok": True, "say": f"Mic {how} to {want} dB." + (" That's the max." if want >= 59 else "")}

    # ---- listening -----------------------------------------------------------------------------
    def cutoff_bytes(self) -> int:
        """Audio length at which the hub ends a turn itself: 1.5 s before echod's max_listen would (and never past
        what the hub keeps of a turn)."""
        try:
            limit = float(self.cfg.get("voice.max_listen", None) or 15)
        except (TypeError, ValueError):
            limit = 15.0
        return int(min(max(3.0, limit - 1.5), _MAX_AUDIO / 32000 - 1.0) * 32000)

    def new_tracker(self, end_silence_s: float = 0.8):
        """A SpeechTracker for a turn (test seam: self._spans = a fake detector)."""
        from .speechgate import SpeechTracker, silero_spans

        return SpeechTracker(getattr(self, "_spans", None) or silero_spans, end_silence_s=end_silence_s)

    def wake_end_silence(self) -> float:
        """Quiet (s) that ends a wake-word turn (voice.wake_end_silence, default 1.2; 0 = leave it to echod)."""
        try:
            return max(0.0, float(self.cfg.get("voice.wake_end_silence", 1.2)))
        except (TypeError, ValueError):
            return 1.2

    def reapply_prefs(self, volume: bool = False) -> int:
        """The voice settings changed (admin page): push them to every connected Echo now instead of at its next
        reconnect. volume=False leaves the Echo's volume alone (it's changed by voice too). Thread-safe."""
        loop, n = self._loop, 0
        if loop is None:
            return 0
        for s in self.sats:
            if s.connected and s.client is not None and s.ents:
                loop.call_soon_threadsafe(s._apply_audio_prefs, s.client, s.ents, volume)
                n += 1
        return n

    # ---- announce / intercom between the Echoes (like Alexa's Announce and Drop In) ----------------
    # "announce dinner's ready" = Ava says it on every OTHER Echo (the one you're at gets "Announced."); "tell the
    # kitchen ..." / "announce ... in the bedroom" = one room. "intercom the kitchen" = she says "Go ahead.", the mic
    # opens (an announcement with start_conversation), what you say is RECORDED and played in the kitchen in YOUR voice
    # ("Intercom from the bedroom." first), then "Sent." here; "intercom the kitchen, dinner's ready" sends it in her
    # voice right away. "reply ..." / "reply" goes back to whoever intercommed you last. Walkie-talkie, not a live call:
    # echod only streams the mic during a turn, so a message is a few seconds behind.
    @staticmethod
    def _wav_pcm(wav: bytes) -> bytes:
        """The 16 kHz mono PCM inside a WAV from synth (b"" if it isn't one, e.g. a test stub)."""
        import io
        import wave
        try:
            with wave.open(io.BytesIO(wav), "rb") as w:
                if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
                    return b""
                return w.readframes(w.getnframes())
        except Exception:
            return b""

    @staticmethod
    def _concat_wav(parts: list, gap_s: float = 0.3) -> bytes:
        """One 16 kHz mono WAV of the PCM parts with a little silence between them."""
        from .tts import _wrap_wav
        gap = b"\0" * int(16000 * 2 * gap_s)
        return _wrap_wav(gap.join(p for p in parts if p), 16000)

    async def _announce_on(self, sat: Satellite, text: str, start_conversation: bool = False, force: bool = False,
                           requeue: bool = False):
        """Say `text` on ONE Echo as an announcement (ducks its music; start_conversation = open its mic after).
        An Echo on a live call gets it after the call (it would cut the call / silence the ring for good) - force =
        skip the check BEFORE synthesis (the held-line drain). The check right before sending always runs: a call
        that started while the line was being synthesized still wins (it's held again; requeue = at the FRONT of the
        queue, and "held" is returned so the drain stops)."""
        if sat.client is None:
            return False
        if not force and self.calls.queued(sat):
            if start_conversation:
                return "busy"                   # an open-the-mic line is never queued for later (livecall.hold)
            self.calls.hold(sat, text, start_conversation)
            return True
        try:
            wav = await asyncio.to_thread(self.synth, text)
            if self.calls.busy(sat) if requeue else self.calls.queued(sat):
                if start_conversation:
                    return "busy"
                self.calls.hold(sat, text, start_conversation, front=requeue)
                return "held" if requeue else True
            url = self.replies.publish(wav, reach_ip=sat.ip)
            res = await sat.client.send_voice_assistant_announcement_await_response(
                media_id=url, timeout=60, text=text, start_conversation=start_conversation)
            return bool(getattr(res, "success", True))     # False = echod cut it off (another announcement, "stop")
        except Exception as exc:
            log.warning("%s: announcement failed: %s", sat.name, exc)
            return False

    def _announce_sat_bg(self, sat: Satellite, text: str) -> None:
        """_announce_on from any thread, not waited for (the router's reply shouldn't wait for other rooms)."""
        loop = self._loop
        if loop is not None:
            asyncio.run_coroutine_threadsafe(self._announce_on(sat, text), loop)

    def _canon_room(self, want: str) -> str:
        """"master bedroom" / "front room" -> the Echo room it names ("bedroom") - only when the WHOLE phrase is one
        of that room's names (so "master bedroom dinner" still fails and the router can split it)."""
        from . import lightcmd
        w = " ".join((want or "").lower().split())
        if not w or any((s.room or "").lower() == w or s.name.lower() == w for s in self.sats):
            return w
        pairs = lightcmd._aliases({(s.room or "").lower(): [] for s in self.sats if s.room},
                                  self.cfg.get("lights.aliases", {}) or {})
        return next((canon for spoken, canon in pairs if spoken == w), w)

    def intercom_cmd(self, action: str, satellite: str = "", room: str = "", target: str = "",
                     msg: str = "", named: bool = False) -> dict:
        """The router's side: action = "announce" (msg to every other Echo, or the `target` room), "intercom" (the
        `target` room: with msg = in her voice now, without = "Go ahead." + record), "reply" (to whoever intercommed
        this Echo last). `satellite` = the Echo that heard it, `room` its room. Thread-safe (sends go to the loop)."""
        src = next((s for s in self.sats if satellite and s.name == satellite), None)
        if src is None and room:
            src = next((s for s in self.sats if (s.room or "").lower() == room.lower()), None)
        want = " ".join((target or "").lower().split())
        for junk in ("the ", "my "):
            if want.startswith(junk):
                want = want[len(junk):]
        want = self._canon_room(want.removesuffix(" echo").strip())

        def in_room(w: str) -> list:
            return [s for s in self.sats if w and (s.room or "").lower() == w]

        def label(s: Satellite) -> str:
            return f"the {s.room}" if s.room else s.name

        if action == "announce":
            if not msg:
                return {"ok": False, "say": "Announce what?"}
            if want in ("", "all", "everyone", "everybody", "house", "whole house", "every room", "all rooms",
                        "other rooms", "the other rooms", "family", "whole family"):
                targets = [s for s in self.sats if s.client is not None and s.connected and s is not src]
                if not targets:
                    return {"ok": False, "say": "There's no other Echo to announce to." if src else "No Echo is connected."}
                where = "everywhere"
            else:
                if not in_room(want):
                    return {"ok": False, "unknown_room": True, "say": f"There's no Echo in the {want}."}
                targets = [s for s in in_room(want) if s.client is not None and s.connected]
                if not targets:
                    return {"ok": False, "say": f"The {want} Echo isn't connected right now."}
                where = f"the {want}"
            lead = f"From {label(src)}: " if src is not None and src.room and src not in targets else ""
            for t in targets:
                self._announce_sat_bg(t, lead + msg)
                if src is not None and t is not src:
                    t.intercom_from = src.name        # "reply ..." there goes back here (it sounds the same as an intercom)
            log.info("announce (%s) -> %s: %r", satellite or room or "console", [t.name for t in targets], msg)
            return {"ok": True, "say": "Announced." if where == "everywhere" else f"Told {where}."}

        if action == "reply":
            tgt = next((s for s in self.sats if src is not None and src.intercom_from and s.name == src.intercom_from), None)
            if tgt is None:
                return {"ok": False, "say": "Nobody's intercommed you yet."}
        else:
            if not want:
                return {"ok": False, "say": "Intercom which room?"}
            cands = in_room(want)
            if not cands:
                return {"ok": False, "unknown_room": True, "say": f"There's no Echo in the {want}."}
            tgt = cands[0]
        if src is not None and tgt is src:
            return {"ok": False, "say": "That's this Echo. Name another room."}
        if tgt.client is None or not tgt.connected:
            return {"ok": False, "say": f"The {tgt.room or tgt.name} Echo isn't connected right now."}
        if msg:                                                   # one-shot, in her voice
            lead = f"From {label(src)}: " if src is not None else ""
            self._announce_sat_bg(tgt, lead + msg)
            if src is not None:
                tgt.intercom_from = src.name
            log.info("intercom (%s) -> %s: %r", satellite or room or "console", tgt.name, msg)
            return {"ok": True, "say": f"Sent to {label(tgt)}."}
        if src is None:
            return {"ok": False, "say": "The intercom records from an Echo - say it to the one you're at."}
        return {"ok": True, "say": "Go ahead.", "intercom_open": True, "intercom_target": tgt}

    def call_cmd(self, action: str, satellite: str = "", room: str = "", target: str = "", source: str = "",
                 on: bool | None = None) -> dict:
        """Live two-way calls (livecall.py), from the router's thread. action = call / answer / hangup / drop_in /
        dnd / status. `satellite`/`room` = the Echo it was said to; `source` = the room to call FROM (typed chat:
        "call the kitchen from the bedroom"); `target` = the room to call / the Echo a setting is for."""
        loop = self._loop
        if loop is None:
            return {"ok": False, "say": "The Echoes aren't connected right now."}

        def find(w: str):
            w = " ".join((w or "").lower().split())
            for junk in ("the ", "my "):
                if w.startswith(junk):
                    w = w[len(junk):]
            w = self._canon_room(w.removesuffix(" echo").strip())      # ("call the master bedroom")
            return next((s for s in self.sats if w and ((s.room or "").lower() == w or s.name.lower() == w)), None)

        here = next((s for s in self.sats if satellite and s.name == satellite), None) or \
            (find(room) if room else None)
        if action == "call":
            src = find(source) if source else here
            dst = find(target)
            if dst is None:
                return {"ok": False, "unknown_room": True, "say": f"There's no Echo in the {target}."}
            if src is None:
                others = [s for s in self.sats if s is not dst and s.connected]
                if len(others) == 1:
                    src = others[0]                      # the app's chat: the only other Echo
                else:
                    return {"ok": False, "say": "Say it to the Echo you want to call from."}
            res = asyncio.run_coroutine_threadsafe(self.calls.call(src, dst), loop).result(12)
            if res.get("fallback"):                       # no house word / old firmware: the walkie-talkie
                log.info("live call %s -> %s not possible (%s) - walkie-talkie instead", src.name, dst.name, res.get("why"))
                if here is not src:
                    # the app / another room asked: only the person AT src can leave a message (opening src's mic
                    # with nobody known to be there would record the room) - say how instead
                    return {"ok": False, "say": "The live intercom isn't working right now - check the house word on "
                                                f"both Echoes. At an Echo, say: intercom the {dst.room or dst.name}."}
                r = self.intercom_cmd("intercom", satellite=src.name, room=src.room, target=dst.room or dst.name)
                if r.get("intercom_open"):
                    r["say"] = "The live intercom isn't working right now, so leave a message. Go ahead."
                return r
            return res
        if action in ("answer", "hangup"):
            want = ("Ringing",) if action == "answer" else ("Ringing", "Calling", "In call")
            cand = [s for s in self.sats if self.calls.of(s).phase in want]
            if target:
                t = find(target)
                cand = [s for s in cand if s is t]
            if not cand:
                return {"ok": False, "say": "Nothing's ringing." if action == "answer" else "There's no call to hang up."}
            ok = all(asyncio.run_coroutine_threadsafe(self.calls.press(s, "phone_" + action), loop).result(6)
                     for s in cand[:1 if action == "answer" else len(cand)])
            return {"ok": ok, "say": ("Answered." if action == "answer" else "Hung up.") if ok else "That didn't work."}
        if action in ("drop_in", "dnd"):
            t = find(target) if target else here
            if t is None:
                return {"ok": False, "say": "Which Echo?"}
            key = "intercom_drop_in" if action == "drop_in" else "intercom_do_not_disturb"
            if on is None:
                cur = self.calls.of(t).drop_in if action == "drop_in" else self.calls.of(t).dnd
                what = "Drop In" if action == "drop_in" else "Do not disturb for calls"
                return {"ok": True, "say": f"{what} is {'on' if cur else 'off'} for the {t.room or t.name}."}
            async def flip():                             # on the hub loop: the API connection isn't thread-safe
                return self.calls.set_switch(t, key, bool(on))
            ok = asyncio.run_coroutine_threadsafe(flip(), loop).result(6)
            what = "Drop In" if action == "drop_in" else "Do not disturb for calls"
            return {"ok": ok, "say": f"{what} is {'on' if on else 'off'} for the {t.room or t.name}." if ok
                    else "I couldn't change that."}
        if action == "status":
            return {"ok": True, "echoes": {s.name: self.calls.status(s) for s in self.sats}}
        return {"ok": False, "say": "I'm not sure what to do with the call."}

    async def _open_intercom(self, sat: Satellite, turn: Turn, text: str, res: dict) -> None:
        """"Intercom the kitchen": close this turn quietly (the words still show on the screen), then "Go ahead." as
        an announcement that opens the mic (start_conversation) - the turn that opens is the recording (handle_start)."""
        target = res.get("intercom_target")
        self.convlog.add("voice", sat.room, sat.name, text, f"(intercom to {target.room or target.name})")
        if turn.canceled:
            return
        if not turn.stt_sent:
            sat.event("STT_END", {"text": text})
        sat.event("INTENT_START")
        sat.event("INTENT_END", {"conversation_id": turn.conversation_id, "continue_conversation": "0"})
        sat.event("RUN_END")
        # armed BEFORE the announcement (echod opens the mic the instant it reports the announcement finished, so
        # arming after could lose the race); _go_ahead shortens the window once it has played, and DISARMS if it
        # didn't (cut off by another announcement, synth down, Echo gone) - otherwise the next real command within
        # the window would be shipped to the other room as a recording.
        sat.intercom_to, sat.intercom_until = target, time.time() + 15.0
        log.info("%s: intercom to %s - saying 'Go ahead' and opening the mic", sat.name, target.name)
        asyncio.ensure_future(self._go_ahead(sat, target, str(res.get("say") or "Go ahead.")))

    async def _go_ahead(self, sat: Satellite, target: Satellite, say: str) -> None:
        ok = await self._announce_on(sat, say, start_conversation=True)
        if sat.intercom_to is not target:           # the mic already opened (handle_start took the arm) - done
            return
        if ok is True:
            sat.intercom_until = min(sat.intercom_until, time.time() + 4.0)   # it opens right after it finished
            return
        sat.intercom_to, sat.intercom_until = None, 0.0
        log.warning("%s: 'Go ahead' didn't play (%s) - intercom to %s disarmed", sat.name, ok, target.name)
        await self._announce_on(sat, "A call came in, so the intercom closed. Say intercom again." if ok == "busy"
                                else "I couldn't open the intercom. Try again in a moment.")

    async def _deliver_intercom(self, sat: Satellite, turn: Turn) -> None:
        """Play what was just recorded on `sat` in the other room, in the speaker's own voice, then "Sent." here."""
        from . import speechgate
        target, pcm = turn.intercom, bytes(turn.audio)
        sat.event("RUN_END")                        # the recording's done; nothing is spoken in THIS turn
        if turn.canceled:
            return
        # judge SPEECH, not length: a recording nobody talked into runs to the hub's cutoff (8+ s of room noise,
        # since echod's endpoint detector never fires without speech) - and trim the silence around what was said
        try:
            spans = await asyncio.to_thread(speechgate.silero_spans, pcm)
        except Exception as exc:
            log.warning("%s: speech check failed (%s) - sending the recording as is", sat.name, exc)
            spans = [(0.0, len(pcm) / 32000.0)] if len(pcm) >= 16000 else []
        if not spans:
            await self._announce_on(sat, "I didn't catch anything. Say intercom again, and talk after I say go ahead.")
            return
        a = max(0, int((spans[0][0] - 0.25) * 32000)) // 2 * 2
        b = min(len(pcm), int((spans[-1][1] + 0.35) * 32000)) // 2 * 2
        if b > a:
            pcm = pcm[a:b]
        where = sat.room or sat.name
        try:
            lead = await asyncio.to_thread(self.synth, f"Intercom from the {where}.")
        except Exception as exc:
            log.warning("%s: no lead-in for the intercom (%s) - sending the voice alone", sat.name, exc)
            lead = b""
        wav = self._concat_wav([self._wav_pcm(lead), pcm])
        if self.calls.queued(target):                # its speaker belongs to a live call (or its held lines) now
            await self._announce_on(sat, f"The {target.room or target.name} is on a call. Try again in a bit.")
            return
        try:
            if target.client is None or not target.connected:
                raise RuntimeError("not connected")
            url = self.replies.publish(wav, reach_ip=target.ip)
            await target.client.send_voice_assistant_announcement_await_response(
                media_id=url, timeout=90, text=f"Intercom from the {where}")
            target.intercom_from = sat.name
            self.convlog.add("voice", sat.room, sat.name,
                             f"(intercom to {target.room or target.name}, {len(pcm) / 32000:.1f}s of voice)", "(delivered)")
            log.info("%s: intercom message (%.1fs) delivered to %s", sat.name, len(pcm) / 32000, target.name)
            await self._announce_on(sat, "Sent.")
        except Exception as exc:
            log.warning("%s: intercom to %s failed: %s", sat.name, target.name, exc)
            await self._announce_on(sat, f"I couldn't reach the {target.room or target.name} Echo.")

    # ---- announcements (timers / reminders speaking up on their own) ---------------------------
    def alert_button(self, sat) -> None:
        """A volume button on `sat`: if the intruder alert is blasting there, the router stops the music (on a thread:
        it talks to the music service) and she says so."""
        fn = getattr(self.router, "alert_button", None)
        if fn is None or not getattr(self.router, "_alert", None):
            return

        def go():
            try:
                res = fn(sat.room, sat.name)
                if res and res.get("say"):
                    log.info("%s: volume button during the intruder alert - alarm music stopped", sat.name)
                    if not res.get("silent"):
                        self._announce(res["say"], sat.room)
            except Exception:
                log.exception("%s: alert button failed", sat.name)
        threading.Thread(target=go, daemon=True, name="alert-button").start()

    def _announce(self, text: str, room: str = "") -> object:
        """From any thread. -> the future (a routine waits on it so its announcements play in order)."""
        loop = self._loop
        if loop is None:
            return None
        return asyncio.run_coroutine_threadsafe(self.announce(text, room), loop)

    async def announce(self, text: str, room: str = "") -> None:
        want = (room or "").lower()
        targets = [s for s in self.sats if s.client is not None and (not want or (s.room or "").lower() == want)] \
            or [s for s in self.sats if s.client is not None]
        if not targets:
            return
        held = [s for s in targets if self.calls.queued(s)]
        for s in held:                                    # on a live call: said once it ends (after what waited)
            self.calls.hold(s, text)
        targets = [s for s in targets if s not in held]
        if not targets:
            return
        wav = await asyncio.to_thread(self.synth, text)
        for s in targets:
            if self.calls.queued(s):                      # a call started while it was being synthesized
                self.calls.hold(s, text)
                continue
            try:
                url = self.replies.publish(wav, reach_ip=s.ip)
                await s.client.send_voice_assistant_announcement_await_response(media_id=url, timeout=60, text=text)
            except Exception as exc:
                log.warning("%s: announcement failed: %s", s.name, exc)

    # ---- connections ---------------------------------------------------------------------------
    async def run(self) -> None:
        confs = self.cfg.get("satellites", []) or []
        if not confs:
            raise SystemExit("No satellites configured yet. Flash an Echo, then: "
                             "python -m houseava.satellite add --name <n> --ip <ip> --room <room> --psk <key>")
        self._loop = asyncio.get_running_loop()
        self.replies.start()
        log.info("reply server on port %s", self.replies.port)
        self._load_wake()
        self._start_admin()
        self._start_facecast()
        if getattr(self, "_spans", None) is None:          # load the follow-up speech detector now, not mid-turn
            from .speechgate import warm_up

            if not await asyncio.to_thread(warm_up):
                log.warning("speech detector (Silero VAD) didn't load: follow-ups will end on the Echo's own timer")
        self.sats = [Satellite(self, c) for c in confs]
        self.start_music()
        self.routines.start()                            # the clock for time / sunrise / sunset routines
        await asyncio.gather(*(self._connect(s) for s in self.sats))
        asyncio.create_task(self._watchdog())            # force a reconnect if one ever gets stuck
        if self._transcribe is None:                     # load the speech model now (GPU: ~10 s), not on the first command
            asyncio.create_task(asyncio.to_thread(self._warm_stt))
        await asyncio.Event().wait()

    def _warm_stt(self) -> None:
        try:
            from .stt import Transcriber
            t = Transcriber(self.cfg)
            t.warm_up()
            self._stt = t                                # (the second listen uses it too)
            self._transcribe = t.transcribe_pcm16
            log.info("speech-to-text ready (%s)", t.model_name)
        except Exception as exc:
            log.warning("speech-to-text warm-up failed (it loads on the first command instead): %s", exc)

    async def _watchdog(self, period: float = 30.0, stuck_after: float = 60.0) -> None:
        """ReconnectLogic normally recovers on its own, but a connect to a device caught mid-boot hangs in its
        handshake with no retry (every Echo reboot/unplug). A satellite down longer than `stuck_after` gets its
        connection thrown away and a new client + ReconnectLogic built - on the FIRST strike: the gentle forced
        disconnect that used to come first never helped (both Echo reboots on 10-15 came back only on the rebuild,
        3.5 min later), and a rebuild while the Echo is still booting simply gets rebuilt again next strike."""
        while True:
            await asyncio.sleep(period)
            await self._watchdog_tick(time.time(), stuck_after)

    async def _watchdog_tick(self, now: float, stuck_after: float = 60.0) -> None:
        for sat in self.sats:
            if sat.connected or sat.client is None:
                continue
            if now - sat.last_change < stuck_after:
                continue
            await self._rebuild(sat, now)

    async def _rebuild(self, sat: Satellite, now: float | None = None) -> None:
        """Stop the old ReconnectLogic, force-drop the old client, connect from scratch."""
        now = now or time.time()
        down = now - (sat.last_change or now)
        sat.last_change = now                            # don't hammer; give the new attempt room
        sat.forced_at = now
        log.warning("%s: down %.0fs — rebuilding its connection", sat.name, down)
        old_logic, old_client = sat.logic, sat.client
        for what, call in (("stop", old_logic.stop() if old_logic is not None else None),
                           ("disconnect", old_client.disconnect(force=True) if old_client is not None else None)):
            if call is None:
                continue
            try:
                await asyncio.wait_for(call, 10)
            except Exception as exc:
                log.warning("%s: old connection %s: %s", sat.name, what, exc)
        try:
            await self._connect(sat)
        except Exception as exc:
            log.warning("%s: rebuilding the connection failed: %s", sat.name, exc)

    def _start_admin(self) -> None:
        try:
            from .admin import AdminServer
            self._admin = AdminServer(self.cfg, hub=self)
            self._admin.start()
        except Exception:
            log.exception("admin server failed to start (non-fatal)")

    def _load_wake(self) -> None:
        """Load the custom wake-word .tflite once and serve it from the reply server, so each Echo can
        download it when the hub offers it on connect."""
        self._wake_offer = None
        if not self.cfg.get("wake.enabled", True):
            return
        try:
            import hashlib
            import json
            from pathlib import Path
            wid = str(self.cfg.get("wake.id", "hey_ava"))
            base = Path(__file__).resolve().parent.parent / "wakeword"
            mp = Path(str(self.cfg.get("wake.model", "") or "").strip() or (base / "hey_ava_V3.tflite"))
            jp = Path(str(self.cfg.get("wake.manifest", "") or "").strip() or (base / "hey_ava_V3.json"))
            model = mp.read_bytes()
            manifest = json.loads(jp.read_text(encoding="utf-8"))
            manifest["model"] = f"{wid}.tflite"              # the device fetches this filename BESIDE the manifest
            # serve the manifest (what the offer URL points at) and the model (fetched beside it)
            self.replies.register_wake(f"{wid}.json", json.dumps(manifest).encode(), "application/json")
            self.replies.register_wake(f"{wid}.tflite", model, "application/octet-stream")
            self._wake_offer = {"id": wid, "name": str(self.cfg.get("wake.name", "Hey Ava")),
                                "size": len(model), "hash": hashlib.sha256(model).hexdigest()}
            log.info("wake word '%s' loaded (%d bytes) from %s", wid, len(model), mp)
        except Exception:
            log.exception("wake word didn't load (non-fatal); the Echo keeps its stock wake word")
            self._wake_offer = None

    async def _setup_wake(self, sat: Satellite, cli) -> None:
        """Offer the custom wake word to this Echo and make it active (keeping Alexa too if there's room)."""
        offer = getattr(self, "_wake_offer", None)
        if not offer:
            # custom word off (wake.enabled false: Alexa only):
            # the Echo REMEMBERS its last selection, so tell it to run Alexa alone instead of leaving Hey Ava armed
            if not self.cfg.get("wake.enabled", True):
                try:
                    resp = await cli.get_voice_assistant_configuration(timeout=10)
                    alexa = next((w.id for w in resp.available_wake_words
                                  if w.id == "alexa" or "alexa" in (w.wake_word or "").lower()), None)
                    if alexa and list(resp.active_wake_words) != [alexa]:
                        await cli.set_voice_assistant_configuration(active_wake_words=[alexa])
                        resp = await cli.get_voice_assistant_configuration(timeout=20)
                    sat.wake_slots = list(resp.active_wake_words)
                    log.info("%s: wake words now %s (custom word off)", sat.name, sat.wake_slots)
                    sat.apply_wake_thresholds(cli)
                except Exception as exc:
                    log.warning("%s: couldn't set Alexa as the only wake word: %s", sat.name, exc)
            return
        try:
            from aioesphomeapi.model import VoiceAssistantExternalWakeWord
            host = str(self.cfg.get("http.public_host", "") or self.replies.host_for(sat.ip))
            url = f"http://{host}:{self.replies.port}/wake/{offer['id']}.json"   # the MANIFEST; model fetched beside it
            ext = VoiceAssistantExternalWakeWord(id=offer["id"], wake_word=offer["name"],
                                                 trained_languages=["en"], model_type="micro",
                                                 model_size=offer["size"], model_hash=offer["hash"], url=url)
            log.info("%s: offering wake word '%s' at %s (sha %s…)", sat.name, offer["id"], url, offer["hash"][:12])
            resp = await cli.get_voice_assistant_configuration(timeout=10, external_wake_words=[ext])
            avail = {w.id: (w.wake_word or "") for w in resp.available_wake_words}
            active = [offer["id"]]
            if self.cfg.get("wake.keep_alexa", True):
                alexa = next((wid for wid, ww in avail.items() if wid == "alexa" or "alexa" in ww.lower()), None)
                if alexa and len(active) < max(1, resp.max_active_wake_words):
                    active.append(alexa)
            await asyncio.sleep(1.5)                          # let the offer settle before selecting (it downloads on select)
            # re-send the offer right before selecting, so its URL is current when the device fetches
            await cli.get_voice_assistant_configuration(timeout=10, external_wake_words=[ext])
            # (async in aioesphomeapi: NOT awaiting it - as this did until 10-12 - never sends it, which is why the Echo
            # never downloaded "Hey Ava")
            await cli.set_voice_assistant_configuration(active_wake_words=active)
            # what it actually runs, slot by slot: it downloads + loads the model on that selection, and a word it
            # couldn't load is left out (echod voice.go OnWakeWord)
            now = await cli.get_voice_assistant_configuration(timeout=40, external_wake_words=[ext])
            sat.wake_slots = list(now.active_wake_words)
            log.info("%s: wake words asked %s, running %s by slot", sat.name, active, sat.wake_slots)
            sat.apply_wake_thresholds(cli)                 # each slot's own strictness, now its word is known
        except Exception as exc:
            log.warning("%s: couldn't set the custom wake word: %s", sat.name, exc)

    def _start_facecast(self) -> None:
        self.facecast = None
        if not self.cfg.get("facecast.enabled", True):
            return
        try:
            from .facecast import FaceCastServer
            self.facecast = FaceCastServer(self.cfg)
            self.facecast.start()
        except Exception:
            log.exception("facecast (the screen face) failed to start (non-fatal)")
            self.facecast = None

    async def _setup_face(self, sat: Satellite, cli) -> None:
        """Point this Echo at our face stream and switch its dashboard to streamed, so the idle screen is Ava."""
        if getattr(self, "facecast", None) is None:
            return
        try:
            from aioesphomeapi import SelectInfo
            ents, services = await cli.list_entities_services()
            svc = {s.name: s for s in services}
            sels = {e.object_id: e for e in ents if isinstance(e, SelectInfo)}
            host = str(self.cfg.get("http.public_host", "") or self.replies.host_for(sat.ip))
            addr = f"{host}:{self.facecast.port}"
            if "dashboard_server" in svc:
                await cli.execute_service(svc["dashboard_server"], {"address": addr, "key": self.facecast.key})
            if "screen_dashboard" in sels:
                cli.select_command(sels["screen_dashboard"].key, "Streamed")
            if "screen_dashboard_return" in sels:
                cli.select_command(sels["screen_dashboard_return"].key, "Never")
            if "dashboard_show" in svc:
                await cli.execute_service(svc["dashboard_show"], {})
            log.info("%s: face streaming from %s", sat.name, addr)
        except Exception as exc:
            log.warning("%s: couldn't set up the face: %s", sat.name, exc)

    def start_music(self) -> None:
        """The music library + one player per Echo (music.enabled, off until you turn it on), handed to the router. If it can't
        load, music requests get a plain "it isn't set up" and everything else carries on."""
        if self.music is None and self.cfg.get("music.enabled", False):
            try:
                from .music import MusicService

                self.music = MusicService(self.cfg, self.sats, publish_host=self.replies.host_for)
            except Exception:
                log.exception("music is off: the music library didn't load")
                self.music = None
        elif self.music is not None:
            for s in self.sats:
                if s.name not in self.music.players:
                    self.music.add_speaker(s.name, s.room, s.speaker, s.ip)
        if self.music is not None:
            try:
                self.music.start()
            except Exception:
                log.exception("music is off: its song server didn't start")
                self.music = None
        if hasattr(self.router, "music"):
            self.router.music = self.music

    async def on_connect(self, sat: Satellite, cli) -> None:
        log.info("%s connected (%s, room %s)", sat.name, sat.ip, sat.room or "-")
        sat.connected = True
        sat.forced_at = 0.0                          # a clean connect: the watchdog's next move is the gentle one again
        sat.last_change = time.time()
        cli.subscribe_voice_assistant(handle_start=sat.handle_start, handle_stop=sat.handle_stop,
                                      handle_audio=sat.handle_audio)
        self._sync_time(sat, cli)                        # so its screen clock isn't stuck on UTC
        try:
            await sat.setup_media(cli)
        except Exception as exc:                         # voice still works without the speaker
            log.warning("%s: couldn't set up its speaker: %s", sat.name, exc)
        try:                                             # live-call events (esphome.techo5_phone)
            cli.subscribe_service_calls(lambda call, s=sat: self.calls.on_service_call(s, call))
        except Exception as exc:
            log.warning("%s: couldn't follow its call events: %s", sat.name, exc)
        await self._setup_face(sat, cli)                 # Ava's face on the idle screen
        await self._setup_wake(sat, cli)                 # the "Hey Ava" wake word
        self._restore_mute(sat)                          # a "stop listening for an hour" that outlived a restart

    # ---- "stop listening for an hour" (echod's mic_mute) / "you keep waking up" (this Echo's wake strictness) ----
    def _mute_file(self):
        from .config import data_dir
        return data_dir() / "mic_mute.json"

    def _mutes(self) -> dict:
        try:
            import json
            d = json.loads(self._mute_file().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_mutes(self, d: dict) -> None:
        try:
            import json
            self._mute_file().write_text(json.dumps(d), encoding="utf-8")
        except Exception as exc:
            log.warning("couldn't save the mic-mute times: %s", exc)

    def _schedule_unmute(self, sat: Satellite, until: float) -> None:
        loop = self._loop
        if loop is None:
            return
        if sat.unmute_handle is not None:
            sat.unmute_handle.cancel()

        def back_on() -> None:
            sat.unmute_handle = None
            d = self._mutes()
            if d.pop(sat.name, None) is not None:
                self._save_mutes(d)
            sat.set_mic_mute(False)

        def arm() -> None:
            sat.unmute_handle = loop.call_later(max(0.0, until - time.time()), back_on)

        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            arm()
        else:
            loop.call_soon_threadsafe(arm)

    def _restore_mute(self, sat: Satellite) -> None:
        until = self._mutes().get(sat.name)
        if until is None:
            return
        if float(until) <= time.time():
            d = self._mutes()
            d.pop(sat.name, None)
            self._save_mutes(d)
            sat.set_mic_mute(False)                      # (the hour ran out while the hub was down)
        else:
            self._schedule_unmute(sat, float(until))

    def _pick_sat(self, satellite: str = "", room: str = "", named: bool = False):
        in_room = next((s for s in self.sats if room and s.room.lower() == room.lower()), None)
        if named:
            return in_room
        return next((s for s in self.sats if satellite and s.name == satellite), None) or in_room \
            or (self.sats[0] if len(self.sats) == 1 else None)

    def mute_cmd(self, action: str, satellite: str = "", room: str = "", named: bool = False, minutes: int = 60) -> dict:
        """"mute" = this Echo stops listening for `minutes` (then its mic comes back by itself; the mic button on the
        Echo undoes it sooner); "unmute" (said to the OTHER Echo / the app - a muted one can't hear it)."""
        sat = self._pick_sat(satellite, room, named)
        if sat is None:
            return {"ok": False, "say": f"There's no Echo in the {room}." if named else "Which Echo?"}
        where = f"the {sat.room} Echo" if sat.room else sat.name
        if not sat.connected:
            return {"ok": False, "say": f"I can't reach {where} right now."}
        if sat.mic_mute_key is None:
            return {"ok": False, "say": f"{where[0].upper() + where[1:]} can't mute its microphone."}
        d = self._mutes()
        if action == "unmute":
            if sat.unmute_handle is not None:
                sat.unmute_handle.cancel()
                sat.unmute_handle = None
            d.pop(sat.name, None)
            self._save_mutes(d)
            sat.set_mic_mute(False)
            return {"ok": True, "say": f"{where[0].upper() + where[1:]} is listening again."}
        minutes = max(1, min(int(minutes or 60), 12 * 60))
        until = time.time() + minutes * 60
        d[sat.name] = until
        self._save_mutes(d)
        self._schedule_unmute(sat, until)
        span = f"{minutes // 60} hour{'s' if minutes // 60 != 1 else ''}" if minutes % 60 == 0 else f"{minutes} minutes"
        here = satellite and sat.name == satellite
        # (said to the Echo itself: the reply has to go out BEFORE the mute - the hub mutes it a moment later)
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: loop.call_later(4.0 if here else 0.0, lambda: sat.set_mic_mute(True)))
        else:
            sat.set_mic_mute(True)
        return {"ok": True, "say": (f"Okay, I'll stop listening for {span}. Press my mic button to turn me back on "
                                    "sooner." if here else f"Okay, {where} stops listening for {span}.")}

    def wake_cmd(self, direction: str, satellite: str = "", room: str = "", named: bool = False) -> dict:
        """"less" = this Echo wakes up less easily (a TV / a phone playing a show false-wakes it), "more" = more easily.
        Its own wake_threshold in its satellites entry (the other Echo keeps its own), saved and applied now."""
        sat = self._pick_sat(satellite, room, named)
        if sat is None:
            return {"ok": False, "say": f"There's no Echo in the {room}." if named else "Which Echo?"}
        where = f"the {sat.room} Echo" if sat.room else sat.name
        try:
            cur = float(sat.conf.get("wake_threshold", self.cfg.get("voice.wake_threshold", 0.87)) or 0.87)
        except (TypeError, ValueError):
            cur = 0.87
        step = 0.03 if direction == "less" else -0.03
        new = round(max(0.80, min(0.97, cur + step)), 2)
        if new == round(cur, 2):
            return {"ok": True, "say": ("That's already as picky as I go here - at that point I'd stop hearing you. "
                                        "Say stop listening for an hour instead." if direction == "less" else
                                        "I'm already waking up as easily as I safely can here.")}
        sat.conf["wake_threshold"] = new
        try:
            for s in self.cfg.data.get("satellites", []) or []:
                if isinstance(s, dict) and s.get("name") == sat.name:
                    s["wake_threshold"] = new
            self.cfg.save()
        except Exception as exc:
            log.warning("couldn't save %s's wake strictness: %s", sat.name, exc)
        if sat.connected and sat.client is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: sat.apply_wake_thresholds(sat.client))
        log.info("%s: wake strictness %.2f -> %.2f", sat.name, cur, new)
        return {"ok": True, "say": ("Okay, I'll be pickier about waking up here." if direction == "less"
                                    else "Okay, I'll wake up more easily here.")}

    def _sync_time(self, sat: Satellite, cli) -> None:
        """Answer the Echo's time-zone request (ESPHome HA time sync) so its clock shows local time, not UTC.
        The device asks once we subscribe to HA states; we reply with the epoch + a POSIX TZ rule."""
        from aioesphomeapi import api_pb2

        tz = str(self.cfg.get("timezone", "") or "").strip() or server_tz_rule()
        try:
            conn = cli._get_connection()

            def on_get_time(_msg) -> None:
                try:
                    conn.send_message(api_pb2.GetTimeResponse(epoch_seconds=int(time.time()), timezone=tz))
                except Exception as exc:
                    log.warning("%s: time reply failed: %s", sat.name, exc)

            conn.add_message_callback(on_get_time, (api_pb2.GetTimeRequest,))
            cli.subscribe_home_assistant_states(lambda *a: None)   # nudges the device to ask for the time
            log.info("%s: time sync armed (tz %s)", sat.name, tz)
        except Exception as exc:
            log.warning("%s: couldn't arm time sync: %s", sat.name, exc)

    async def on_disconnect(self, sat: Satellite, expected: bool = False) -> None:
        log.warning("%s disconnected (%s)", sat.name, "expected" if expected else "dropped")
        sat.turn = None
        sat.connected = False
        sat.last_change = time.time()
        if self.music is not None:
            self.music.on_disconnect(sat.name)

    async def _connect(self, sat: Satellite) -> None:
        from aioesphomeapi import APIClient, ReconnectLogic

        cli = APIClient(sat.ip, 6053, password="", noise_psk=sat.psk, client_info="HouseAva")
        sat.client = cli

        async def on_connect() -> None:
            await self.on_connect(sat, cli)

        async def on_disconnect(expected: bool) -> None:
            await self.on_disconnect(sat, expected)

        logic = ReconnectLogic(client=cli, on_connect=on_connect, on_disconnect=on_disconnect, name=sat.name)
        sat.logic = logic                                # (the watchdog rebuilds it if it ever gets stuck)
        await logic.start()


# ---- command line ------------------------------------------------------------------------------
def _add(cfg: Config, a) -> int:
    sats = [s for s in (cfg.get("satellites", []) or []) if s.get("name") != a.name]
    sats.append({"name": a.name, "ip": a.ip, "room": a.room, "psk": a.psk or ""})
    cfg.set("satellites", sats)
    cfg.save()
    print(f"Added {a.name} ({a.ip}, room {a.room or '-'}). {len(sats)} satellite(s) configured.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="House Ava's Echo satellite hub")
    sub = ap.add_subparsers(dest="cmd")
    p_add = sub.add_parser("add", help="register a flashed Echo")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--ip", required=True)
    p_add.add_argument("--room", default="")
    p_add.add_argument("--psk", default="", help="the device's API encryption key (from the TECHO5 installer)")
    sub.add_parser("list", help="show the configured Echoes")
    sub.add_parser("run", help="connect to every Echo and serve voice turns")
    a = ap.parse_args(argv)
    cfg = Config()
    if not cfg.load_ok:
        # (not "no satellites configured": the file has them, it just doesn't read)
        print(f"{cfg.path} isn't valid JSON: {cfg.load_error}\nFix it (python3 -m json.tool shows where), or move it "
              "aside to start over.", file=sys.stderr)
        return 2
    if a.cmd == "add":
        return _add(cfg, a)
    if a.cmd == "list":
        for s in cfg.get("satellites", []) or []:
            print(f"{s.get('name')}: {s.get('ip')} room={s.get('room') or '-'} key={'set' if s.get('psk') else 'none'}")
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(SatelliteHub(cfg).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
