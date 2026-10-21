# Face packs

One folder per face. Nine ship with House Ava: `ava` (the default), `brooke`, `claire`, `harper`, `lena`, `maya` and
`reese` (women), and `elias` and `jordan` (men). Pick one in the app (Settings → Screen) or with `facecast.face` in
the config. Screenshots of all of them are in [docs/FACES-AND-VOICES.md](../docs/FACES-AND-VOICES.md).

Just want your own picture on the screen? You don't need a pack: point `facecast.frames` at the picture, or pick
"My own picture" in the app.

## Making a pack

Put the folder in `faces/` in your copy of the repo and re-run `bash deploy/install-houseava.sh` (that copies
`faces/` over to `~/houseava` and restarts House Ava). Reload the app and it's in the Face list.
Don't put it straight into `~/houseava/faces`, because an update replaces that folder. Or keep the pack anywhere and
point `facecast.frames` at it.

```
faces/<id>/
  face.json
  frames/
    idle.jpg            required: what she rests on
    blink.jpg           optional: used for the blink
    smile.jpg laugh.jpg think.jpg surprised.jpg confused.jpg serious.jpg sad.jpg point.jpg heartbeat.jpg
    mouth_ee.jpg mouth_open.jpg mouth_oh.jpg mouth_oo.jpg mouth_fv.jpg mouth_l.jpg mouth_narrow.jpg mouth_wide.jpg
```

Only `idle.jpg` is needed. The Echo screen uses `idle` and `blink` right now. The expressions and mouth shapes are
for a talking mouth that isn't built yet (want to build it? see [CONTRIBUTING.md](../CONTRIBUTING.md)). The packs
here are portrait JPEGs, 1152×1712 (Ava's idle.jpg is 784×1168), but any size works: the picture is scaled to fit next
to the clock.

`face.json`:

```json
{
  "name": "Maya",
  "gender": "female",
  "default_voice": "en-US-JennyNeural",
  "rest": "idle.jpg",
  "visemes": { "rest": "idle.jpg", "MBP": "idle.jpg", "EE": "mouth_ee.jpg", "AH": "mouth_open.jpg",
               "OH": "mouth_oh.jpg", "OO": "mouth_oo.jpg", "FV": "mouth_fv.jpg", "L": "mouth_l.jpg",
               "WQ": "mouth_narrow.jpg", "wide": "mouth_wide.jpg" },
  "letters": { "MBP": "mbp", "FV": "fv", "L": "l", "OO": "uwq", "OH": "o", "EE": "ei", "AH": "a",
               "WQ": "ctdkghszjnrx" },
  "emotions": ["smile", "laugh", "sad", "point", "surprised", "confused", "serious", "think", "blink", "heartbeat"]
}
```

- `name` is what the app's list shows.
- `default_voice` is the voice the face was made for. The app shows it next to the name, but it doesn't switch the
  voice for you. That's `voice.edge_voice`.
- `visemes` and `letters` are the plan for the talking mouth. House Ava doesn't read them yet. The idea (it's how
  my desktop assistant does it): each letter of a word maps through `letters` to a mouth shape, and each shape to a
  picture through `visemes`. Letters that aren't listed count as "other consonants" (`WQ`), spaces rest, and a loud
  `a` opens `wide`.
- `voice_locked: true` (Brooke, Claire, Harper and Reese have it) just says that face was made for its default voice
  only. Nothing enforces it.
