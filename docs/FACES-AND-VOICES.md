# Faces and voices

## On the real screens

These two came straight off the Echoes' framebuffers, so this is exactly what the screens show:

| Echo Show 8 (1st gen, 1280×800) | Echo Show 5 (1st gen, 960×480) |
|---|---|
| <img src="screenshots/real-show8.png" alt="Ava's face and the clock on an Echo Show 8"> | <img src="screenshots/real-show5.png" alt="Ava's face and the clock on an Echo Show 5"> |

The hub draws each frame at the Echo's own screen size (the Echo says how big it is when it connects), with the face
on the left and the clock and date on the right. It sends about one frame a second, and she blinks now and then.

On the first version the Show 8's clock ran over the face. I fixed that, and now the text shrinks to fit the space
next to the picture no matter what shape the picture is:

<img src="screenshots/real-show8-before-fix.png" alt="The old layout on the Show 8, with the clock running over the face" width="480">

## The nine faces

Every face as the hub draws it (rendered with the same code that streams to the Echoes):

| Face | On a Show 8 | On a Show 5 |
|---|---|---|
| **Ava** (the default) | <img src="screenshots/ava-show8.jpg" width="320"> | <img src="screenshots/ava-show5.jpg" width="240"> |
| **Brooke** | <img src="screenshots/brooke-show8.jpg" width="320"> | <img src="screenshots/brooke-show5.jpg" width="240"> |
| **Claire** | <img src="screenshots/claire-show8.jpg" width="320"> | <img src="screenshots/claire-show5.jpg" width="240"> |
| **Elias** | <img src="screenshots/elias-show8.jpg" width="320"> | <img src="screenshots/elias-show5.jpg" width="240"> |
| **Harper** | <img src="screenshots/harper-show8.jpg" width="320"> | <img src="screenshots/harper-show5.jpg" width="240"> |
| **Jordan** | <img src="screenshots/jordan-show8.jpg" width="320"> | <img src="screenshots/jordan-show5.jpg" width="240"> |
| **Lena** | <img src="screenshots/lena-show8.jpg" width="320"> | <img src="screenshots/lena-show5.jpg" width="240"> |
| **Maya** | <img src="screenshots/maya-show8.jpg" width="320"> | <img src="screenshots/maya-show5.jpg" width="240"> |
| **Reese** | <img src="screenshots/reese-show8.jpg" width="320"> | <img src="screenshots/reese-show5.jpg" width="240"> |

Each pack also has expressions and mouth shapes. Here's Ava's full set:

<img src="screenshots/ava-frames.jpg" alt="All of Ava's frames: idle, blink, smile, laugh, think, surprised, confused, serious, sad, point, heartbeat and the mouth shapes">

The other packs' sheets are in [screenshots/](screenshots/) (`<name>-frames.jpg`). Right now the Echo screen uses the
resting picture and the blink. The rest are there for a talking mouth, which would be a great thing for someone to
build (the face packs say which mouth shape goes with which letters, see [faces/README.md](../faces/README.md)).

## Picking a face

**In the app:** Settings → Screen → Face, then Save. The Echoes switch within about a second, no restart needed.

**In the config:**

```json
"facecast": {"face": "maya"}
```

## Your own picture

Anything works: a photo of you, your dog, a logo, a PNG with a see-through background. Put it on the House Ava box
and point at it.

**In the app:** Settings → Screen → Face → **My own picture**, type the path (like `/home/you/pictures/rex.png`), and
Save.

**In the config:**

```json
"facecast": {"frames": "/home/you/pictures/rex.png"}
```

`frames` can be:
- **one picture** (`.jpg`, `.jpeg`, `.png` or `.webp`). That's the whole face.
- **a folder of pictures**. It uses `idle.*` and `blink.*` if they're there, otherwise the first picture by name.
- **a face pack folder** (with `face.json` and `frames/`) kept somewhere else.

`frames` wins over `face`, so clear it to go back to a pack. Any shape works, because the picture gets scaled to fit
its share of the screen (up to 45% of the width) and the clock shrinks to fit beside it. Transparent PNGs go on the
dark background, so you don't get black edges. Here's a plain smiley PNG:

| Show 8 | Show 5 |
|---|---|
| <img src="screenshots/custom-picture-show8.jpg" width="400"> | <img src="screenshots/custom-picture-show5.jpg" width="300"> |

Want blinking? Add a `blink.jpg` next to an `idle.jpg` in a folder and point `frames` at the folder.

To add a whole new pack that shows up in the app's list, see [faces/README.md](../faces/README.md).

## Voices

Her voice is picked separately from her face (`voice.edge_voice`, or Settings → Voice in the app). It's Microsoft's
neural voices through [edge-tts](https://github.com/rany2/edge-tts). They're free, sound great, and need the internet.

The app has these in its list:

| Voice | Sounds like |
|---|---|
| `en-US-AvaNeural` | Adult woman, US. The default, and what Ava, Brooke, Claire, Harper and Reese were made for |
| `en-US-AriaNeural` | Woman, US |
| `en-US-JennyNeural` | Woman, US. Goes with Maya |
| `en-US-MichelleNeural` | Woman, US |
| `en-US-EmmaNeural` | Woman, US |
| `en-GB-SoniaNeural` | Woman, British. Goes with Lena |
| `en-US-AndrewNeural` | Man, US. Goes with Elias |
| `en-US-BrianNeural` | Man, US. Goes with Jordan |
| `en-US-AnaNeural` | A kid's voice |

Any other Edge voice works too: put it in the config (run `~/houseava/.venv/bin/edge-tts --list-voices` to see
them all) and the app adds it to its list.
`voice.rate`, `voice.pitch` and `voice.volume` tweak it (`"+10%"`, `"-2Hz"`…).

**Offline backup (Kokoro).** If Microsoft can't be reached, she switches to [Kokoro-82M](https://github.com/thewh1teagle/kokoro-onnx)
on your own box for a minute at a time (setup in [SETUP.md](SETUP.md#the-offline-voice-kokoro)). Pick it with
`voice.offline_voice`:

| Voice | |
|---|---|
| `af_heart` | Heart, US woman (the best one, and the default) |
| `af_bella` | Bella, US woman |
| `af_nicole` | Nicole, US woman, soft |
| `af_sarah` | Sarah, US woman |
| `bf_emma` | Emma, British woman |
| `am_michael` | Michael, US man |
| `bm_george` | George, British man |

## Personality

Separate from the face and voice too:
- `persona`: **sassy** (the default), **warm** (like a friendly neighbor) or **deadpan** (dry, short, secretly funny).
- `personality.cussing`: she swears for real, in answers, in the little "lights off" lines, roasts and comebacks.
  With it off, all of that stays clean.
- `personality.adult`: unlocks the dirty, adult and morbid joke banks.
- `name`: what she calls herself.
