"""Config: one JSON file with sane defaults so a missing/partial file never crashes her.

Dev runs keep it next to the project; on a server it lives in ~/.houseava/houseava_config.json.
The Hubspace password is NOT stored here — it comes from the HOUSEAVA_HUBSPACE_PASSWORD
environment variable (or a secret file), so a copy of the config never leaks it.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()

DEFAULTS: dict[str, Any] = {
    # her character
    "name": "Ava",                 # the house units are "Ava" too; rename freely (e.g. "Juno")
    "persona": "sassy",            # key into persona.PERSONAS
    # the Echoes ask the hub for the time zone on connect (ESPHome time sync); without an answer they
    # sit on UTC and the screen clock is wrong. "" = the server's own zone (set it with timedatectl), or a POSIX
    # TZ rule: Pacific PST8PDT,M3.2.0,M11.1.0 · Mountain MST7MDT,M3.2.0,M11.1.0 · Central CST6CDT,M3.2.0,M11.1.0 ·
    # Eastern EST5EDT,M3.2.0,M11.1.0
    "timezone": "",
    # the brain: a local Ollama model. The default is Ollama on the same box at 127.0.0.1:11434; point url at
    # another machine if your GPU lives there.
    "brain": {
        "url": "http://127.0.0.1:11434",
        "model": "qwen2.5:14b",
        "temperature": 0.6,        # a little loose — she has a personality
        "timeout": 45,
    },
    # web search: your own SearXNG (docker, localhost:8888 — JSON format turned on). web.py falls back to
    # Wikipedia + Google News + DuckDuckGo if it's unreachable. Blank = skip SearXNG.
    "web": {"searxng_url": "http://127.0.0.1:8888", "city": ""},
    # WiZ bulbs {name: ip} and which bulbs are in which room. Name them "<room> <n>" so "bedroom light two"
    # works, e.g. "bulbs": {"bedroom 1": "192.168.1.201", "bedroom 2": "192.168.1.202"},
    #         "rooms": {"bedroom": ["bedroom 1", "bedroom 2"]}
    # aliases = other names for a room, {"master bedroom": "bedroom", "den": "living room"}
    "lights": {"bulbs": {}, "rooms": {}, "aliases": {}},
    # Hubspace (cloud) plugs {friendly name: hubspace device name}. Password via env, not here.
    "plugs": {
        "enabled": False,
        "username": "",            # your Hubspace account email
        "devices": {},             # friendly -> the name shown in the Hubspace app, e.g. {"porch": "Porch Light"}
    },
    # the Echo satellites. Each knows its room so "turn the light off" is room-local.
    # psk = the device's noise key (base64); fill when a unit is flashed.
    "satellites": [
        # {"name": "Bedroom Echo", "ip": "bedroom-echo.local", "psk": "<base64 key>", "room": "bedroom"},
    ],
    # her voice. edge_voice = the SAME neural voice as desktop Ava (AvaNeural, an adult woman) — NOT AnaNeural,
    # which is Microsoft's CHILD voice. volume = edge-tts loudness ("+0%".."+100%"). offline_fallback: use the
    # local Kokoro voice (offline_voice, "af_heart" = Heart, US female) when Microsoft can't be reached, so she
    # never goes silent in an outage. echo_volume (0.0-1.0, null = don't touch) is the Echo's hardware volume set
    # on every connect; 1.0 = max (the Show 5's little speaker is quiet, so keep it maxed). bass/treble (-6..+6)
    # are set on connect too — a loudness boost that helps the small speaker sound fuller.
    # follow_up = seconds the Echo keeps listening after a reply so you can talk again without the wake
    # word (echod does it on-device); 0 = off. bass/treble/echo_volume are set on every connect too.
    # stop_word = how hard she listens FOR an interruption while she's talking (echod's barge-in). 0 turns
    # it off so the mic is closed while she speaks (she won't hear herself or seem to keep listening).
    # Not answering the room (an Echo woke and talked when nobody spoke to her) - set on every connect:
    # wake_threshold = how sure the Echo must be it heard the wake word (0.5-0.99; echod's default 0.85 false-woke
    # on conversation/TV; higher = fewer false wakes, a bit more "say it again"). follow_ups = follow-ups one wake
    # word opens (echod's 0 = NO LIMIT, which chained a reply after every reply). follow_up_tone = the sound a
    # follow-up opens with ("None" = silent like Alexa's follow-up mode; the old chime after every reply sounded
    # like she'd woken by herself). max_listen = the longest one turn records. ignore_chatter = drop whisper's
    # noise phrases and let the brain ignore speech that isn't for her (people talking, the TV) - quietly.
    "voice": {"edge_voice": "en-US-AvaNeural", "rate": "+0%", "pitch": "+0Hz", "volume": "+0%",
              "offline_fallback": True, "offline_voice": "af_heart",
              "echo_volume": 1.0, "bass": 4, "treble": 4, "follow_up": 0, "stop_word": 0.6,
              "wake_threshold": 0.87, "follow_ups": 1, "follow_up_tone": "None", "max_listen": 10,
              "ignore_chatter": True, "duck_on_near_miss": True,
              # music_on_turn = what a playing song does while she listens: "Pause" (stops, picks up after - so the
              # Echo can hear him stop talking) or "Duck" (plays on at music_duck_db, -40..-3). echo_canceller =
              # "WebRTC" (stronger, for the wake word over music) or "Built-in". speaker_eq = the factory tuning
              # (True) - off = no built-in bass lift, and bass/treble then do nothing.
              # echo_canceller: "Built-in" - echod's own measurements: WebRTC removes more music but its suppressor
              # clamps a voice talking over it, and the wake word was markedly harder to catch (10-20: switched back).
              "music_on_turn": "Pause", "echo_canceller": "Built-in",
              # bass / treble / stop_word / duck_on_near_miss / music_on_turn / music_duck_db / echo_canceller /
              # speaker_eq can also be set PER ECHO in its satellites entry
              # (e.g. {"name": "Kitchen Echo", ..., "bass": 0, "treble": 0}) - that wins over these.
              # the microphone: mic_gain = analog gain in dB (0-59; the factory setting is 20 - 30 hears further, for
              # the front room), mic_mixing = "All microphones" / "Center mic", mic_leveling = auto-lift quiet speech,
              # mic_denoise = noise reduction. All applied live from the app ("Alexa, turn your mic up" too).
              "mic_gain": 30, "mic_mixing": "All microphones", "mic_leveling": True, "mic_denoise": True},
    # the little HTTP server the Echo fetches reply audio from (on this box)
    # public_host = this box's IP as the Echoes see it ("" = worked out per Echo); set it if the box has two networks
    "http": {"host": "0.0.0.0", "port": 8790, "public_host": ""},
    # custom wake word: a microWakeWord .tflite + its manifest. On connect the hub OFFERS it to each Echo
    # (served from the reply server at /wake/<id>.tflite, verified by sha256) and activates it. keep_alexa
    # leaves "Alexa" working too. model/manifest "" = wakeword/hey_ava_V3.* next to the package (not shipped -
    # bring or train your own microWakeWord model and set enabled true).
    # threshold = how sure the Echo must be it heard "Hey Ava" (echod uses each model's own raw scale and ignores the
    # model's cutoff - this one's trainer calibrated it at 0.43; 0.6 = a bit stricter). Alexa uses voice.wake_threshold.
    "wake": {"enabled": False, "id": "hey_ava", "name": "Hey Ava", "model": "", "manifest": "", "keep_alexa": True,
             "threshold": 0.5},
    # speech-to-text on this box
    # min_logprob / max_no_speech: whisper's own confidence, a SAFETY NET only. Real far-field speech from the Echo mic
    # scores avg_logprob -0.36..-0.97 (a -0.35 cutoff silently ate "How are you?" on 10-15), TV babble ~ -0.46 - so
    # logprob can't tell them apart; -2.0 only drops true garbage. (see stt.py)
    # keep_audio = save each voice turn (WAV + what whisper heard) in <data_dir>/turn_audio, for tuning. Off by default.
    "stt": {"model": "small.en", "device": "auto", "compute_type": "int8", "min_logprob": -2.0, "max_no_speech": 0.6,
            "keep_audio": False},
    # music from the NAS (houseava/music.py) on the Echo that heard you. root = the read-only mount of
    # your NAS music share (deploy/setup-music-share.sh); data_dir "" = <data_dir>/music (the tag index,
    # playlists, each Echo's Now Playing); seed_file = a copy of desktop Ava's music_index.json, its desktop_root
    # paths moved onto root, to start the index from (once); index = keep the tag index fresh in the background.
    "music": {"enabled": False, "root": "/mnt/music", "desktop_root": "", "data_dir": "",
              "http_port": 8791, "seed_file": "", "index": True},
    # admin web server (settings + voice enrollment from the phone app). pin "" = one is made on the first run
    # and saved here (the log says what it is).
    "admin": {"port": 8792, "pin": ""},
    # Ava's face on the Echo screen — a dashcast stream server (facecast.py). The hub points each Echo at
    # it (dashboard mode "streamed") on connect. key = the dashcast secret we share with the device;
    # "" = a random one each start (the hub hands it to the Echo on every connect).
    # face = one of the packs in faces/ (ava, maya, lena, elias, jordan, brooke, claire, harper, reese);
    # frames = any picture, a folder of pictures or a pack folder instead, which wins over face.
    "facecast": {"enabled": True, "port": 8793, "key": "", "face": "ava", "frames": ""},
    # people: speaker identification on the Echoes
    "people": {"enrolled": [], "identify": True, "sensitivity": 1.0, "unknown_voice": "allow"},
    # where you are, for sunrise / sunset routines (decimal degrees, e.g. 40.71 / -74.01). Blank = those
    # triggers just don't fire.
    "location": {"lat": None, "lon": None},
    # personality: adult = the dirty / adult / morbid joke banks; cussing = she swears for real (persona.py injects
    # it + quips.py gives sweary light/plug lines). Both off by default - turn them on if that's your house.
    "personality": {"adult": False, "cussing": False},
    # "arm the house": after exit_delay seconds, any presence an Echo reports is logged, pushed to your
    # phone (ntfy.sh topic — subscribe to it in the ntfy app; blank = no push), and announced on the Echo.
    "security": {"enabled": True, "exit_delay": 40, "ntfy_topic": "", "announce": True, "alert_cooldown": 25},
    # "intruder alert" / "code red" / "panic mode": every light bright red + this song blasting. A specific
    # song ("Raining Blood") or a genre ("death metal") — both work against the library.
    "intruder": {"music": "death metal"},
    # the room the typed console speaks as (blank = room-agnostic). Used by the admin chat endpoint.
    "console_room": "",
}


def data_dir() -> Path:
    env = os.environ.get("HOUSEAVA_DIR")
    base = Path(env) if env else Path.home() / ".houseava"
    # dev convenience: if the project has a local config, use the project dir instead
    proj = Path(__file__).resolve().parent.parent
    if (proj / "houseava_config.json").exists() and not env:
        base = proj
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Config:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else data_dir() / "houseava_config.json"
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load_ok = True
        self.load_error = ""          # why the file didn't read, when load_ok is False
        self.load()

    def load(self) -> None:
        with _LOCK:
            if self.path.exists():
                try:
                    user = json.loads(self.path.read_text(encoding="utf-8")) or {}
                    if not isinstance(user, dict):
                        raise ValueError("config is not an object")
                    self.data = _deep_merge(DEFAULTS, user)
                    self.load_ok = True
                    v = self.data.get("voice") or {}
                    if not self.data.get("_migrated_1020"):
                        # every save() writes ALL the defaults of that day: a config saved before 10-20 holds
                        # stop_word 0 (it was clamped to 0.5 = on; 0 now means OFF) and the WebRTC canceller
                        if v.get("stop_word") in (0, 0.0):
                            v["stop_word"] = DEFAULTS["voice"]["stop_word"]
                        if v.get("echo_canceller") == "WebRTC":
                            v["echo_canceller"] = DEFAULTS["voice"]["echo_canceller"]
                        self.data["_migrated_1020"] = True   # (saved with the next save; a stop_word 0 set later = off)
                except Exception as exc:
                    print(f"[config] couldn't read {self.path}: {exc}; using defaults", file=sys.stderr)
                    self.data = copy.deepcopy(DEFAULTS)
                    self.load_ok = False
                    self.load_error = str(exc)
            else:
                self.data = copy.deepcopy(DEFAULTS)
                self.data["_migrated_1020"] = True    # (a brand-new config never needs the 10-20 fix-up above)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, dotted: str, value: Any) -> None:
        node = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self) -> bool:
        """Write the config out; False if it couldn't be. A file that didn't read (a typo in a hand edit) is never
        written over: it's moved aside to <name>.unreadable.json first, so the Echoes, keys and bulbs in it are
        still there to fix."""
        with _LOCK:
            if not self.load_ok and self.path.exists():
                keep = self.path.with_name(self.path.stem + ".unreadable.json")
                n = 1
                while keep.exists():
                    n += 1
                    keep = self.path.with_name(f"{self.path.stem}.unreadable-{n}.json")
                try:
                    os.replace(self.path, keep)
                except OSError as exc:
                    print(f"[config] {self.path} didn't read and couldn't be moved aside ({exc}); not writing over it",
                          file=sys.stderr)
                    return False
                print(f"[config] {self.path} didn't read ({self.load_error}); it's kept as {keep.name}, and a new one "
                      "is written from the defaults", file=sys.stderr)
                self.load_ok = True
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
                return True
            except Exception as exc:
                print(f"[config] couldn't write {self.path}: {exc}", file=sys.stderr)
                return False

    def hubspace_password(self) -> str:
        pw = os.environ.get("HOUSEAVA_HUBSPACE_PASSWORD", "")
        if pw:
            return pw
        f = data_dir() / "hubspace_password.txt"
        try:
            return f.read_text(encoding="utf-8").strip() if f.exists() else ""
        except Exception:
            return ""
