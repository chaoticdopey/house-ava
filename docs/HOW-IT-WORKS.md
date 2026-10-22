# How House Ava works

One Python process, `python -m houseava.satellite run`, does everything. There's no Home Assistant and nothing in
the cloud doing the thinking. TECHO5 turns each Echo into an ESPHome device, and House Ava is its ESPHome API
client, the same seat Home Assistant would normally sit in.

## The pieces

| Module | What it does |
|---|---|
| `satellite.py` | The hub. One always-on connection per Echo, the voice turn from wake to reply, applying each Echo's settings when it connects, the watchdog, and the `add` / `list` / `run` commands. |
| `brain.py` | The `Router`: text in, a reply out. A long list of fast paths first (no model), then the model for everything else. |
| `stt.py`, `speechgate.py` | faster-whisper speech to text, and the gate that throws away noise and whisper's made-up lines. Silero VAD ends the turn when you stop talking. |
| `tts.py`, `offline_tts.py` | Microsoft's neural voice through edge-tts, with Kokoro as the offline fallback. Output is a 16 kHz mono WAV the Echo fetches. |
| `llm.py`, `persona.py` | A tiny Ollama client, and her personalities (sassy / warm / deadpan, plus optional swearing). |
| `lights.py`, `lightcmd.py` | WiZ bulbs over UDP, and the light-command parser (rooms, numbers, "all but", timers, "it / them", mishearings). |
| `music.py` + `vendor/ava` | The music library, a song server, and a player per Echo. |
| `livecall.py` | Live two-way calls using TECHO5's own intercom. |
| `facecast.py` | Her face and the clock, streamed to the Echo's screen. |
| `routines.py`, `security.py` | Alexa-style routines and "arm the house". |
| `admin.py` | The phone app and its JSON API. |
| `memory.py`, `convlog.py` | What she remembers, the recent conversation per room, and the log the app shows. |
| `jokes.py`, `comebacks.py`, `quips.py` | The joke banks (no repeats until a bank's used up), comebacks, and the little "done!" lines. |
| `web.py` | Search (SearXNG → Wikipedia → Google News → DuckDuckGo) and weather (Open-Meteo: today, tomorrow, a day, the weekend or the week ahead). |
| `find_bulbs.py` | `python -m houseava.find_bulbs`: lists the WiZ bulbs on your network for the config, `--blink` to tell them apart. |
| `plugs.py` | Hubspace plugs (experimental). |
| `voiceprint.py` | Voice enrollment from the app. Stored, but not used during turns yet. |

## A voice turn, start to finish

1. **Wake.** The Echo runs the wake word itself (TECHO5's `echod`): "Alexa", or a custom microWakeWord model the hub
   offers when it connects.
2. **Start.** The hub tells the Echo to send the audio over the API.
   - If two Echoes wake within 1.2 s of each other, the second one gets told it's a duplicate, so only one answers.
   - A wake turn ends after 1.2 s of quiet, so nobody else can cut in once you're talking.
3. **Audio in.** 16 kHz mono, capped at 30 s. Silero VAD tracks the speech, and the hub ends the turn just before the
   Echo's own listen limit, so you never get the error chime.
4. **Speech to text.** faster-whisper in a worker thread.
   - If the text looks like a garbled light command, it listens again with a bigger beam and the house's room
     names as hints.
   - Empty or junk text ("Thanks for watching!", "you") ends quietly, with no chime.
5. **Routing.** `Router.handle(text, room=<that Echo's room>)` tries the fast paths in order:
   thanks → routines → intercom / announce → intruder alert → all clear → arm / disarm → roasts → jokes → calls →
   Bluetooth → mute → mic gain → **lights** → **music** → time / date / timers → remember / forget → plugs → weather.
6. **The model**, if nothing matched. Ollama gets a JSON "what do they want" prompt with the last few lines of
   conversation in that room. It comes back with one of: light, brightness, color, plug, music, remember, forget,
   weather, search, insult, answer, or **ignore** (room chatter that wasn't for her).
   - Device actions get re-run through her own parser, and she speaks the *real* result, never what the model
     claimed it did.
   - `search` runs the web search and has the model answer from those results, in character.
7. **Reply.** Emoji and *stage directions* are stripped, edge-tts makes the audio, and the WAV goes up on the reply
   server (:8790). The Echo fetches `http://<hub>:8790/tts/<id>.wav` and plays it.

The fast paths are why most things feel instant: lights, time, timers and intercom never touch the model, and
music only asks it for an artist's best-known songs (cached for a month) or to learn songs' moods in the background.

## The other flows

**Music.** The NAS share is mounted read-only at `/mnt/music`. A background index reads the tags (mutagen), and the
vendored music library handles the requests: artists, songs, albums, playlists, moods, "skip this artist". Each song
gets a 12-hour token on the song server (:8791), and the Echo's media player is told to play that URL. When an Echo
goes from playing to idle, the next song starts. If you're using the Echo as a Bluetooth speaker, it backs off.

**Announcements and walkie-talkie.** Announcements go out through the Echo's announce service. For "intercom the
kitchen" she says "Go ahead" and opens the mic, trims your recording with Silero, and plays it in the other room
after "Intercom from the bedroom", in your own voice.

**Live calls.** "call the kitchen" fires the calling Echo's `intercom_call`. The audio goes Echo to Echo on port 8181
(TECHO5 1.2.15+) and never touches the hub. The hub follows the call's state so it can hold announcements, and drops
back to walkie-talkie if the Echoes aren't set up for calls.

**The face.** On connect the hub tells the Echo to stream its dashboard from the hub (`dashboard_server`), then the
Echo connects to :8793. That link is Noise-encrypted (`NNpsk0_25519_ChaChaPoly_SHA256`) with a key the hub makes
and hands over. The Echo says how big its screen is, and the hub sends a JPEG about once a second: the face on the
left, the clock and date on the right, sized so they never overlap. Picking another face in the app switches it on
the next frame.

**Routines.** They live in `routines.json`, made in the app. A 15-second clock thread handles time and sunrise/sunset
triggers (NOAA math, from your `location`). Voice triggers get checked first in the fast paths, and presence comes
from the Echo's presence sensor. By voice, the steps up to the first "wait" are her reply, and the rest runs as
announcements.

**Arm the house.** After the exit delay, any presence an Echo reports gets logged, pushed to your phone through ntfy,
and announced, with a cooldown.

**Intruder alert.** Every bulb's state is saved, then they all go full red, and your `intruder.music` blasts on the
Echo that heard it. One press of a volume button on that Echo stops the music. "All clear" puts every bulb back.

**Echo settings.** On every connect the hub pushes volume, EQ, mic gain / mixing / leveling / noise reduction, wake
sensitivity, follow-up listening, the stop word, music ducking, the echo canceller, the clock's time zone and the
face stream. A watchdog rebuilds any connection that's been stuck for more than a minute (an Echo reboot is back in
about a minute).

## Where things are kept

Everything lives in `~/.houseava` (or `$HOUSEAVA_DIR`):

| File | What |
|---|---|
| `houseava_config.json` | Settings |
| `memory.json` | What she remembers, plus the last 40 turns (all rooms together, each tagged with its room) |
| `conversations.jsonl` | The exchange log the app shows (it keeps growing; delete it whenever) |
| `routines.json` | Routines |
| `jokes_used.json` | Which jokes she's told (so they don't repeat) |
| `mic_mute.json` | "Stop listening for an hour" survives a restart |
| `music/` | The music tag index |
| `voiceprints/` | Voice enrollment from the app |
| `voices/kokoro/` | The offline voice's model files |
| `turn_audio/` | Recorded voice turns, only if you turn `stt.keep_audio` on |

## Environment variables

| Variable | What |
|---|---|
| `HOUSEAVA_DIR` | Where her data lives (the service sets `~/.houseava`) |
| `HOUSEAVA_HUBSPACE_PASSWORD` | Hubspace password |
| `HOUSEAVA_FAKE_BULBS=1` | Never send anything to real bulbs (the tests use this) |
