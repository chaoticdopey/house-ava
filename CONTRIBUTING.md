# Helping out

Thanks for even looking! This started as my own house project, so it's pretty loose, but here's what helps.

## Tried it? Tell me how it went

That's the most useful thing right now. Open an issue with the **Test report** template, or just post in
[Discussions](https://github.com/chaoticdopey/house-ava/discussions):
- which Echo(s) and TECHO5 version
- your server: CPU, RAM, GPU (or none), distro, Python version
- which model you used and how fast answers felt
- what worked, what didn't, and the log for anything that broke (`journalctl --user -u houseava -n 200`, with your
  keys blanked out)

## Questions and ideas

Use [Discussions](https://github.com/chaoticdopey/house-ava/discussions) for questions, ideas, "would it be possible
to…", and showing off your setup. Keep issues for actual bugs.

## Code

PRs are welcome. A few things to keep it sane:

- **Run the tests** before you send it (see [docs/TESTING.md](docs/TESTING.md)). Always with
  `HOUSEAVA_FAKE_BULBS=1`. A test should never touch real bulbs, Echoes or a real model.
- **Add a test** for what you changed. The existing ones are plain scripts that print a line per check and exit 1
  if anything failed, with no framework to learn. Copy one.
- **Keep the defaults safe for a stranger's house**: nothing on by default that records audio, swears, or needs an
  account, and no IPs, names or keys in the code.
- **Match the code around it.** Plain Python, comments that say *why*, and no big new dependencies unless they're
  optional (like `requirements-extra.txt`).
- **The fast paths matter.** Lights, time, timers, intercom and most music answer without the model, and that's
  why it feels quick. If you add a command, think about whether it belongs in the fast paths in `brain.py`.
- One thing per PR if you can.

## Stuff I'd love help with

- **Other Echoes**: the Show 5 2nd gen, Show 8 2nd gen, Spot, Dot 2. TECHO5 runs on them, and House Ava should, but
  nobody's tried.
- **A talking mouth on the screen**: the face packs already have the mouth shapes and a letter → shape map
  (see [faces/README.md](faces/README.md)), and Edge gives word timings.
- **Voice ID**: enrolling a voice works in the app, but nothing uses it during a turn yet.
- **Other languages**: it's English only (whisper `small.en`, English parsers and jokes).
- **Smaller setups**: good settings for an 8 GB GPU or no GPU at all.
- **Other smart stuff**: lights besides WiZ, plugs besides Hubspace.

## Upstream

Anything that belongs on the Echo itself (wake words, the mic, the speaker, the screen) belongs in
[TECHO5](https://github.com/HuskerMinion/techo5), not here. Please go through HuskerMinion's repo and its own
process for that.
