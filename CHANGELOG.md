# Changelog

What I added since I started it on Oct 4, from my notes. Before today it all lived in my own repo, so this is the
first public version.

## 2026-10-22

- **A step-by-step install tutorial** ([INSTALL.md](INSTALL.md)) for anyone who wants to try it, with a check after
  every step and a test checklist at the end.
- **`python -m houseava.find_bulbs`** lists the WiZ bulbs on your network and prints a config to start from.
  `--blink <ip>` flashes one so you can tell which is which.
- **The weather does the whole week now.** "Give me a seven-day forecast", "what's the weather Saturday", "this
  weekend", or tomorrow. Whisper hears "a seven-day forecast" as "a Saturday forecast", so that gets the week too.
  Every answer says "high 58 degrees and low 50 degrees".
- The weather uses your `location` when it's set. Looking a town up by name can land on the wrong one, since lots of
  towns share a name.
- "What's the weather in Chicago" is Chicago's weather (it used to answer for home), and a town that isn't on the
  map gets "I couldn't find it" instead of a guess from your IP.
- "This weekend" asked on a Sunday is today, not today plus next Saturday.
- The PIN made on the first start isn't printed in the log anymore (testers attach logs to bug reports). It's in
  the config: `grep '"pin"' ~/.houseava/houseava_config.json`.

## 2026-10-21: first public release

- Put it on GitHub. My house stuff is out of the defaults: no bulbs, no location and no music share are set, a
  random admin PIN is made on first run (and a fresh face-stream key every start), the Echo's clock follows the
  server's time zone, and swearing, adult jokes and turn recording are off.
- **Any face or picture on the screen.** Nine face packs, or point it at your own picture (photo, logo, a transparent
  PNG, a folder of pictures). Pick it in the app under Settings → Screen and the Echoes switch within a second.
- Fixed the clock running over the face on the Show 8. The text now shrinks to fit beside any shape of picture, at
  any screen size.
- With swearing off, the roasts and name-calling comebacks keep it clean too (they used to swear no matter what).
- A config file with a typo in it is never written over anymore: it's moved aside (`houseava_config.unreadable.json`)
  and the commands say what's wrong with it, instead of the next save wiping your Echoes and keys.
- An Echo added by its `.local` name on a server without mDNS gets the server's real address for her replies, face
  and songs (it used to get 127.0.0.1).
- A face picture that can't be shown (a typo'd path, an empty folder) keeps the face that's up, and gets picked up
  once the picture's there.
- Wrong PINs in the app get slowed down after 5 in a row.
- The app got an Adult jokes switch and two men's voices (Andrew and Brian), and it keeps a voice you set in the
  config that isn't in its list (saving Settings used to blank it).
- Fixed 15 things a review found in the 10-20 work, plus a one-time config fix-up for older configs.
- Turned on TECHO5's wake-clip recording on both Echoes to compare the two echo cancellers.
- Upstream: sent TECHO5 a "turn it down" command word that works with no wake word while music plays
  ([HuskerMinion/techo5#246](https://github.com/HuskerMinion/techo5/pull/246)).

## 2026-10-20

- She sees the last few lines of the conversation, so follow-ups make sense.
- "Roast Jordan": roast someone by name (nothing sexual about anyone who isn't there), plus "longer" and
  "another one".
- Factual answers don't get a smug tag line stuck on the end anymore.
- She doesn't talk over the intruder alert anymore (the alarm music is the answer), and a press of a volume button
  kills the music.
- Echo canceller back to Built-in. Fixed the stop-word setting so "stop" works over music without the wake word.

## 2026-10-19

- Mined my real misses: a 600-pair "meant vs heard" set took wrong outcomes from 232 to 82.
- A "listen again" second pass for light commands that came out garbled.
- Better timers.
- "Stop listening for an hour" (unmutes by itself, and survives a restart).
- "You keep waking up" tunes the wake word for just that Echo.
- "Is the wall switch off?" hint when a bulb doesn't answer.
- Wake word back to Alexa only (my custom "Hey Ava" wasn't reliable enough).

## 2026-10-18

- Comebacks when you call her names: about 1,000 lines plus fresh roasts.
- Once someone's talking to her, nobody else can cut in (the turn ends on a 1.2 s pause).
- "Are the lights on in here?", "how bright?", "what color?"
- Server tuning: the model stays loaded and warms up at boot.

## 2026-10-17

- Fixed the room names whisper gets wrong most. Tried whisper "hotwords" on real recordings, but it made up room
  names, so it stays off.

## 2026-10-16

- A brand-new light parser: clauses, "it / them", "all but", "in 10 minutes", and status questions.
- Live two-way calls between Echoes using TECHO5's own intercom: call, answer, hang up, do not disturb.
  Announcements wait during a call, and it falls back to walkie-talkie.
- 7 rounds of adversarial review, plus a 1,499-phrase before/after test.

## 2026-10-15

- Second Echo (a Show 8).
- The watchdog rebuilds a dead connection on the first strike (an Echo reboot went from about 3.5 minutes deaf to
  about 1).
- The face scales to each screen.
- Found the cause of a phantom command (a false wake plus whisper making up words): follow-up listening off by
  default, noise reduction on.
- Fixed phone pairing.
- Announcements, walkie-talkie intercom and "reply".
- "Connect my phone", "light number two".
- Whisper on the GPU (0.32 s per command), music pauses while you talk, per-Echo speaker settings, and the joke engine.
- Updated both Echoes' firmware over the air.

## 2026-10-13

- Rebalanced wake sensitivity.
- Mic gain, mic mixing, leveling and noise reduction controls, plus "turn your mic up".

## 2026-10-12

- Fixed her talking when nobody spoke to her: a speech gate (Silero VAD), limited follow-ups, a silent follow-up tone,
  ignoring room chatter, and no error chime when nothing was said.
- The custom wake word finally downloads to the Echo (a missing `await`).
- Phone Bluetooth.
- Routines (voice, time, sunrise/sunset and presence triggers).

## 2026-10-11

- "Living room and dining room" style commands.
- Typed chat in the app, and a conversation log.
- Intruder alert (every light red plus an alarm song, and "all clear" puts them back).
- "Arm the house" (the Echo's presence sensor, a phone push through ntfy, and an announcement).

## 2026-10-10

- First Echo flashed and talking.
- The phone app (PIN; status, settings, Echoes, voice enrollment).
- Swapped a kid-sounding voice for an adult one, added the offline Kokoro voice, and maxed the volume and EQ on the
  little Show 5.
- Fixed 3 bugs that could wreck the config.
- Optional swearing.
- Reconnects by `.local` name, plus a watchdog for stuck reconnects.
- The screen clock gets the right time zone.
- Her face and a clock on the screen, streamed from the hub.

## 2026-10-09

- The Echo hub: one always-on connection per Echo, the whole voice turn, a reply audio server, and "first Echo
  wins" when two hear you.
- Music from the NAS: room-aware, each Echo has its own queue, and the next song starts when one ends.
- Install scripts.

## 2026-10-04

- Started it. The brain runs fully local: Ollama with qwen2.5:14b, room-aware WiZ lights, a sassy personality with
  her own memory, web search, and a type-to-her terminal.
- Read through TECHO5's source and confirmed House Ava could be the Echo's ESPHome client with no Home Assistant at
  all.
