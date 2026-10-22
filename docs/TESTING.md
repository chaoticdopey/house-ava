# Testing

## The test suites

Seventeen standalone scripts in `tests/`. Each one prints a line per check (the two big light ones print a passed
count, plus any misses) and exits 1 if anything failed. No
real Echo, bulb or model is needed: they use fake Echoes, fake bulbs and fake brains, and a temp data folder.

```bash
cd ~/houseava        # the install (that's where the venv is)
export HOUSEAVA_FAKE_BULBS=1 PYTHONIOENCODING=utf-8
for t in tests/run_tests.py tests/test_*.py; do .venv/bin/python "$t" > /dev/null || echo "FAILED: $t"; done
```

`HOUSEAVA_FAKE_BULBS=1` makes sure nothing ever gets sent to a real bulb. Every suite also fakes the bulbs on its
own, but set it anyway. Stop the live hub first (`systemctl --user stop houseava`) or run the tests on another box,
because `test_music` wants the ports the hub is using.

All 17 pass on a clean install (Debian 13, Python 3.13, the versions `requirements.txt` pulls today):

| Suite | What it checks | Checks |
|---|---|---|
| `run_tests.py` | The router's fast paths with fake bulbs, plugs and brain | 16 |
| `test_satellite.py` | A whole voice turn against a fake Echo, start to reply; a config file with a typo never getting written over; the reply address for a `.local` name; the Echo clock's time zone | 29 |
| `test_quiet.py` | Whisper's junk lines, false wakes, follow-ups, ignoring room chatter, ending the turn on a pause | 137 |
| `test_lights_every_way.py` | About 39,000 generated light commands, each graded against an answer key built separately from the parser (it samples, so the count moves a bit; `-v` prints every miss) | ~38,900 |
| `test_lights_attack.py` | 1,157 sneaky sentences that look like light commands but aren't, or the other way round | 1,157 |
| `test_light_numbers.py` | "bedroom light one", "light number two", "one bedroom light" | 14 |
| `test_music.py` | A made-up MP3/FLAC library, the song server, the players and the hub with fake Echoes | 110 |
| `test_intercom.py` | Announcements, walkie-talkie intercom and "reply" | 57 |
| `test_livecall.py` | Live calls, with fakes that replay TECHO5's real sensor values | 226 |
| `test_bluetooth.py`, `test_bt_connect.py` | Phone pairing and reconnecting | 88 + 15 |
| `test_routines.py` | Routines, the sunrise/sunset math, and the app's routines API | 86 |
| `test_roast.py` | Roasts, recent conversation, the intruder alert, the volume button, clean mode with swearing off | 92 |
| `test_facecast.py` | Which picture gets used, transparent PNGs, the clock never running over the face at 5 screen sizes, the app's face picker, keeping the face when a new picture can't be shown, the wrong-PIN backoff | 63 |
| `test_watchdog.py` | Rebuilding a stuck Echo connection | 9 |
| `test_weather.py` | What a weather question asks for (today / tomorrow / a weekday / the weekend / the week, and "a Saturday forecast" being whisper's "a seven-day forecast"), which town is named ("in Chicago", but not "in here" or a room), every answer against a faked week, "this weekend" asked on a Saturday or Sunday, where "home" is, a town that isn't on the map | 46 |
| `test_find_bulbs.py` | The bulb finder hearing a (fake) WiZ bulb, ignoring junk, never switching anything, and `--blink` | 6 |

A couple of suites print a traceback in the middle on purpose (a fake brain that fails), and they still pass.

## Reviews

- 7 rounds of adversarial code review on the light parser and live calls, with each round's fixes reviewed again.
- A before/after test of 1,499 everyday phrases run through the old and new light parsers, comparing what each
  one did.
- I mined my own real misses into a 600-pair "what I meant vs what whisper heard" set. That took wrong outcomes from
  232 down to 82.
- The newer tests (like the face ones) get mutation-checked: break the code on purpose, make sure the test catches it.

## On the real Echoes

Everything below was on my Echo Show 5 and Show 8 (1st gens, TECHO5 1.2.6 → 1.4.0 along the way):
- Full voice turns on both, every day. Lights, music, timers, weather, questions, jokes, announcements and
  walkie-talkie intercom.
- The face and clock checked against screenshots pulled off each Echo's own screen (that's how I caught the Show 8's
  clock running over the face).
- TECHO5 updates done over the air, with how long each Echo was deaf timed (about a minute now, down from 3.5).
- Real mic recordings used for tuning:
  - Whisper's confidence cutoff was first set on clean TTS audio, and it ate real speech, so now it's only a
    safety net.
  - Whisper "hotwords" (room names as hints) tested on 66 real turns. It made up room names, so it stays off.
  - The "listen again" second pass replayed on 129 real turns. Exactly 4 changed, all for the better.
  - The 1.2 s end-of-turn replayed on real turns where two people were talking.
- The brain on the real model: 17/17 real requests answered right, and 11/14 overheard lines correctly ignored.
- Speed on my box: whisper on the GPU about 0.32 s, model answers 2.2–2.5 s, Microsoft's voice about 0.65 s,
  Kokoro about 3 s.
- Music from the real library: the file the Echo fetches is byte-for-byte the NAS copy, seeking works, and a bad
  link gets a 404.

## Upstream: a "turn it down" word for TECHO5

While working on this I trained a "turn it down" command word for TECHO5. It turns the volume down with no wake word
while music's playing, and it's up as a PR on TECHO5
([HuskerMinion/techo5#246](https://github.com/HuskerMinion/techo5/pull/246)).
- Offline at a 0.90 threshold: 97% caught on clean speech, 97% at +10 dB over music, 85% at +5 dB, and no false
  fires on music.
- On my Show 5 it worked 3 out of 3–4 tries, from a few different distances, with music playing.

## Not tested yet (help wanted!)

- Any Echo besides the Show 5 and Show 8 1st gen.
- A full live call between the two Echoes (the code and the fakes pass, but I haven't made one for real yet).
- Phone Bluetooth again since the last pairing fix.
- "Arm the house" firing from someone actually walking in (only unit tests and one test push so far).
- Hubspace plugs, never run against a real one.
- Voice ID: enrolling works in the app, but nothing uses it during turns yet.
- Ubuntu, other distros, smaller GPUs, no GPU at all.
- Which echo canceller (Built-in or WebRTC) is better for the wake word. I'm collecting wake clips on both Echoes
  to compare, and it's still going.

If you test any of this, please open a test report (there's an issue template for it) or post in
[Discussions](https://github.com/chaoticdopey/house-ava/discussions).
