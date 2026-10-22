# Setting up House Ava

**Trying it for the first time? Start with [INSTALL.md](../INSTALL.md)**, the step-by-step tutorial with a check after
each step. This page is the reference: every option, in the same order.

This is everything I did to get it running, in order. I did it on Debian 13. Ubuntu should be about the same, but I
haven't tried it. Expect an hour or two, not counting flashing the Echo.

- [1. Flash your Echo with TECHO5](#1-flash-your-echo-with-techo5)
- [2. The server](#2-the-server)
- [3. The brain (Ollama)](#3-the-brain-ollama)
- [4. Install House Ava](#4-install-house-ava)
- [5. Add your Echoes](#5-add-your-echoes)
- [6. The config file](#6-the-config-file)
- [7. Optional stuff](#7-optional-stuff): GPU speech to text, offline voice, web search, music, plugs, phone pushes,
  custom wake word
- [8. First run: is it working?](#8-first-run-is-it-working)
- [9. Ports](#9-ports)
- [10. Updating and removing](#10-updating-and-removing)
- [Troubleshooting](#troubleshooting)

---

## 1. Flash your Echo with TECHO5

Follow [TECHO5's own guide](https://github.com/HuskerMinion/techo5/blob/main/docs/getting-started.md). Use
HuskerMinion's repos, not forks. On my two Shows it went: unlock with amonet, then LineageOS, then TECHO5's installer
over a real USB data cable. There's no Amazon login anywhere in that.

When it's done:
- **Keep the encryption key** the installer prints. It's also saved in `backups/<serial>/home-assistant.key` in your
  TECHO5 folder (older installs: `backups/<serial>/api.psk`). That base64 string is the `psk` House Ava needs.
- **Get it on Wi-Fi** and note its name, like `kitchen-echo.local`, or give it a DHCP reservation.
- **Leave TECHO5's "Voice without Home Assistant" off.** That's TECHO5 answering by itself. House Ava wants to be
  the one answering.
- **Don't also run it as a voice satellite in Home Assistant.** House Ava connects the same way Home Assistant does,
  and two voice assistants fighting over one Echo won't go well.
- For live calls between Echoes and phone Bluetooth you want TECHO5 **1.2.15 or newer**. I'm on 1.4.0.
- **Live calls also need the same "house word"** typed on every Echo's TECHO5 setup page (no API can set it). Without
  it, "call the kitchen" falls back to the walkie-talkie intercom.

---

## 2. The server

Any always-on 64-bit Linux box or VM on the same network as your Echoes. Give it a static IP.

```bash
sudo apt install git curl python3-venv fonts-dejavu-core libnss-mdns
```

`libnss-mdns` lets the server look up `.local` names like `kitchen-echo.local`. No `sudo`? Install it as root
(`apt install sudo`, then `adduser <you> sudo`) and log in again.

House Ava needs **Python 3.11 or newer**. Debian 13 comes with 3.13.

Set the server's time zone. Her spoken time and the Echo's screen clock both follow it:

```bash
sudo timedatectl set-timezone America/New_York     # yours
```

**NVIDIA GPU (optional but worth it).** The driver's in Debian's `contrib` and `non-free` sections, which a stock
install doesn't turn on: add `contrib non-free` to every `Components:` line in `/etc/apt/sources.list.d/debian.sources`
(or to each `deb` line in `/etc/apt/sources.list`). Then install the kernel headers and DKMS **first**, or the driver
silently builds nothing:

```bash
sudo apt update
sudo apt install linux-headers-amd64 dkms     # the metapackage keeps the headers in step with kernel updates
sudo apt install nvidia-driver
sudo reboot                                   # the old nouveau driver has to go before the NVIDIA one loads
nvidia-smi                                    # should show your card
```

**Secure Boot:** if it's on, the driver won't load. Most PCs ship with it on, and so does a Proxmox VM that uses
UEFI (OVMF) with the default "Pre-Enroll keys". Check with `mokutil --sb-state` (`sudo apt install mokutil` if it's
missing). Then either:
- **turn it off** in the firmware (a Proxmox VM: press Esc as it boots, then Device Manager → Secure Boot
  Configuration, untick Attempt Secure Boot), or
- **keep it on** and, after installing the driver, run `sudo mokutil --import /var/lib/dkms/mok.pub`. Pick a one-time
  password, reboot, and choose **Enroll MOK** on the blue screen (you need a monitor, or the Proxmox console).


I pass an RTX A2000 12 GB through to a Proxmox VM (vfio-pci), and it works like it's bare metal.

---

## 3. The brain (Ollama)

Install [Ollama](https://ollama.com/download) (its Linux install is a `curl … | sh` line), then pull a model:

```bash
ollama pull qwen2.5:14b      # what I use: about 9 GB of VRAM
# smaller card? try qwen2.5:7b or llama3.1:8b and set brain.model to match
```

- Leave Ollama on `127.0.0.1:11434` (the default). Its API has no password, so don't open it to your network.
- Keep the model loaded so the first answer isn't slow. Mine went from about 27 s cold to half a second:

  ```bash
  sudo systemctl edit ollama
  # add:
  # [Service]
  # Environment=OLLAMA_KEEP_ALIVE=-1
  sudo systemctl restart ollama
  ```

- If Ollama lives on another machine, point `brain.url` at it.

---

## 4. Install House Ava

As the user House Ava will run as (**no sudo** for the script itself):

```bash
sudo loginctl enable-linger $USER        # so it runs from boot with nobody logged in
git clone https://github.com/chaoticdopey/house-ava ~/house-ava
cd ~/house-ava
bash deploy/install-houseava.sh
```

Heads up on the names: `~/house-ava` is your copy of the repo, `~/houseava` is the install the script makes (it gets
replaced on every update, so don't edit in there), and `~/.houseava` is her data.

The install script:
1. copies the code to `~/houseava`
2. makes a venv at `~/houseava/.venv` and installs `requirements.txt` into it
3. writes `~/.config/systemd/user/houseava.service` and enables it

Your settings, memory and everything else she keeps live in `~/.houseava`. The script never touches that folder,
so re-running it to update is safe.

Until you add an Echo it just exits (systemd retries it every 30 seconds), which is harmless.

---

## 5. Add your Echoes

From the command line:

```bash
cd ~/houseava
HOUSEAVA_DIR=~/.houseava .venv/bin/python -m houseava.satellite add \
    --name "Kitchen Echo" --ip kitchen-echo.local --room kitchen --psk '<the key from step 1>'
HOUSEAVA_DIR=~/.houseava .venv/bin/python -m houseava.satellite list
systemctl --user restart houseava
```

- `--ip` can be the `.local` name. That's what I use, so a reboot or a DHCP change doesn't matter.
- `--room` is what "turn off the lights" means on that Echo, and what "call the kitchen" looks for.
- You can also add them later in the app (Satellites tab). A new Echo connects after the hub restarts.

---

## 6. The config file

It's `~/.houseava/houseava_config.json`. The settings and their defaults are in
[`houseava/config.py`](../houseava/config.py) with a comment, and you only need to put in what you change. Any save
(the `add` command, the app, "turn your mic up") writes the whole thing out from what the hub loaded when it started.
So edit it with the hub stopped, then start it again:

```bash
systemctl --user stop houseava
nano ~/.houseava/houseava_config.json
python3 -m json.tool ~/.houseava/houseava_config.json > /dev/null && echo "reads fine"
systemctl --user start houseava
```

A file that doesn't read (a missing comma) is never written over: the hub stops and says what's wrong, and if something
saves anyway, the broken one is kept as `houseava_config.unreadable.json`.

Here's a starting point. **Merge these keys into the file that's already there** (it has your Echo from step 5 and,
once the hub has run, your PIN, so don't replace the whole thing):

```json
{
  "name": "Ava",
  "persona": "sassy",
  "location": {"lat": 40.71, "lon": -74.01},
  "brain": {"url": "http://127.0.0.1:11434", "model": "qwen2.5:14b"},
  "web": {"searxng_url": "", "city": "New York"},
  "http": {"public_host": "192.168.1.10"},
  "lights": {
    "bulbs": {"kitchen 1": "192.168.1.201", "kitchen 2": "192.168.1.202", "bedroom 1": "192.168.1.203"},
    "rooms": {"kitchen": ["kitchen 1", "kitchen 2"], "bedroom": ["bedroom 1"]},
    "aliases": {"master bedroom": "bedroom"}
  },
  "personality": {"adult": false, "cussing": false}
}
```

The important bits:

| Key | What it is |
|---|---|
| `timezone` | The Echo's screen clock. Leave it blank and it uses the server's own time zone, so just set that (`sudo timedatectl set-timezone America/New_York`) and both her spoken time and the screen are right. Or give a POSIX TZ rule: Eastern `EST5EDT,M3.2.0,M11.1.0`, Central `CST6CDT,M3.2.0,M11.1.0`, Mountain `MST7MDT,M3.2.0,M11.1.0`, Pacific `PST8PDT,M3.2.0,M11.1.0`. |
| `location.lat` / `lon` | Where you are: the weather and sunrise/sunset routines use it. On Google Maps, right-click your house and the first line is these two numbers. Leave them out and the sun routines don't fire. |
| `web.city` | What she calls your town ("the next 7 days in Springfield"). With no `location`, the weather looks this name up instead (a town name can match a few places, so `location` is better). Neither set = she guesses from your IP (ip-api.com). "What's the weather in Chicago" always looks the named town up. |
| `http.public_host` | The server's IP as the Echoes see it. The Echo fetches her replies, songs, face and wake-word files from this. It's worked out by itself; set it if your box has more than one network, or if the log says `face streaming from` an address the Echo can't reach. |
| `lights.bulbs` | WiZ bulbs, `{"name": "ip"}`. Name them `<room> <number>` so "bedroom light two" works. `cd ~/houseava && .venv/bin/python -m houseava.find_bulbs` lists the ones on your network (and `--blink <ip>` flashes one so you can tell which is which). |
| `lights.rooms` | Which bulbs are in which room. |
| `lights.aliases` | Other names people use for a room: `{"master bedroom": "bedroom", "den": "living room"}`. |
| `persona` | `sassy`, `warm` or `deadpan`. |
| `personality.cussing` / `adult` | Swearing, and the dirty / adult / morbid joke banks. Both off by default. Also in the app. |
| `voice.edge_voice` | Her voice. Any Microsoft neural voice works (see [Faces and voices](FACES-AND-VOICES.md)). |
| `facecast.face` | Which face pack is on the screen (`ava`, `maya`, …). Or `facecast.frames` = a path to your own picture. Also in the app. |
| `intruder.music` | The song or genre "intruder alert" blasts. |
| `security.ntfy_topic` | An [ntfy](https://ntfy.sh) topic for "arm the house" pushes. Make it long and random, because anyone who knows it can read it. |

**Per-Echo settings.** Each entry in `satellites` can override `bass`, `treble`, `stop_word`, `duck_on_near_miss`,
`music_on_turn`, `music_duck_db`, `echo_canceller`, `speaker_eq` and `wake_threshold` for just that Echo. My Show 5 is
tiny and quiet, so it gets more bass and treble than the Show 8.

**The hub sets the Echo's settings on every connect** (volume, EQ, mic gain, wake sensitivity, follow-up listening,
echo canceller, clock time zone, face stream). Change them in the config or the app, not on the Echo, or the hub will
just put them back next time.

---

## 7. Optional stuff

### GPU speech to text

The default is whisper `small.en` on the CPU, which is about 1.4 s per command on an old i5. On a GPU,
`large-v3-turbo` is about 0.3 s and hears a lot better.

```bash
~/houseava/.venv/bin/pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
```

Then tell the service where those libraries are. Use a drop-in, because re-running the install script rewrites
`houseava.service` itself:

```bash
systemctl --user edit houseava
```

and put this in (fix `python3.13` if your Python's different):

```ini
[Service]
Environment=LD_LIBRARY_PATH=%h/houseava/.venv/lib/python3.13/site-packages/nvidia/cublas/lib:%h/houseava/.venv/lib/python3.13/site-packages/nvidia/cudnn/lib
```

Then set this in the config:

```json
"stt": {"model": "large-v3-turbo", "device": "cuda", "compute_type": "int8_float16"}
```

```bash
systemctl --user restart houseava
```

If the GPU model won't load at all, it falls back to `small.en` on the CPU by itself and says so in the log. If the
CUDA libraries can't be found, it can load fine and then fail on every command instead, so watch the log the first
time you talk to her.

### The offline voice (Kokoro)

Her normal voice is Microsoft's (edge-tts), which needs the internet. Kokoro is the backup: when Microsoft can't be
reached she switches to it for a minute at a time.

```bash
~/houseava/.venv/bin/pip install -r ~/houseava/requirements-extra.txt
mkdir -p ~/.houseava/voices/kokoro
```

Put `kokoro-v1.0.fp16.onnx` (about 170 MB) and `voices-v1.0.bin` (about 27 MB) from the
[kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases) into `~/.houseava/voices/kokoro/`.

### Web search (SearXNG)

Without it she still searches (Wikipedia, then Google News, then DuckDuckGo), but your own
[SearXNG](https://docs.searxng.org) is better and keeps it on your network. Run it in Docker on the same box, turn on
the JSON format in its `settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

and set `"web": {"searxng_url": "http://127.0.0.1:8888"}` (or whatever port you gave it). That's the default, but
the starter config above blanks it, so put it back.

### Music from your NAS

```bash
sudo MUSIC_SHARE=//<nas>/<share>/Music HOUSEAVA_USER=$USER bash deploy/setup-music-share.sh
```

That asks for the NAS login (saved root-only in `/etc/houseava/music.cred`), mounts the share **read-only** at
`/mnt/music`, and turns on linger. Then set:

```json
"music": {"enabled": true, "root": "/mnt/music"}
```

and restart. The first tag scan of a big library takes a while (hours for mine), but asking for an artist or song
works while it runs. MP3 and FLAC only.

### Hubspace plugs (experimental)

I've got the code for it, but it's never run against a real plug, so expect to fix stuff.
`~/houseava/.venv/bin/pip install aiohubspace`, set `plugs.enabled`, `plugs.username` and `plugs.devices`
(`{"porch": "Porch Light"}` = the name you say → the name in the Hubspace app). The password goes in the
`HOUSEAVA_HUBSPACE_PASSWORD` environment variable or `~/.houseava/hubspace_password.txt`, never in the config.

### Phone pushes for "arm the house"

Install the [ntfy](https://ntfy.sh) app, subscribe to a long random topic name and put it in `security.ntfy_topic`.

### A custom wake word

Out of the box it's TECHO5's "Alexa". If you train your own [microWakeWord](https://github.com/kahrendt/microWakeWord)
model (TECHO5 has a guide: `docs/training-wake-word.md` in its repo), drop the `.tflite` and its `.json` manifest
somewhere and set:

```json
"wake": {"enabled": true, "id": "hey_ava", "name": "Hey Ava", "model": "/path/to/model.tflite",
         "manifest": "/path/to/model.json", "keep_alexa": true}
```

The hub hands it to every Echo when it connects. I'm not shipping my "Hey Ava" model. I went back to Alexa because it
was more reliable.

---

## 8. First run: is it working?

```bash
journalctl --user -u houseava -f
```

You want to see, more or less in this order:

```
reply server on port 8790
admin: no PIN was set, so one was made: it's admin.pin in /home/you/.houseava/houseava_config.json    ← first run only
admin server on port 8792
face renderer: 2 frame(s) from /home/you/houseava/faces/ava
facecast server on port 8793
Kitchen Echo connected (kitchen-echo.local, room kitchen)
Kitchen Echo: time sync armed (tz ...)
Kitchen Echo: speaker found (...)
Kitchen Echo: face streaming from ...
speech-to-text ready (small.en)
```

Then try it out loud:
- "Alexa, what time is it" (instant, no model)
- "Alexa, turn off the lights"
- "Alexa, what's the weather"
- "Alexa, who wrote Moby Dick" (this one uses the model)

The Echo's screen should switch to her face and the clock. On your phone, open `http://<server>:8792` and use the PIN
(`grep '"pin"' ~/.houseava/houseava_config.json`). **Status** shows the Echoes, and **Chat** lets you type to her.

No Echo yet? You can type to her from the terminal. This switches your **real** lights:

```bash
cd ~/houseava && HOUSEAVA_DIR=~/.houseava .venv/bin/python -m houseava.cli --room kitchen
```

---

## 9. Ports

**Coming in to the server** (from the Echoes and your phone), all TCP:

| Port | What |
|---|---|
| 8790 | Her reply audio and wake-word files (the Echo fetches them) |
| 8791 | Songs, if music's on |
| 8792 | The app and its API (PIN, plain HTTP) |
| 8793 | The face stream (the Echo connects in, encrypted) |

**Going out from the server:** TCP 6053 to each Echo (ESPHome API, encrypted with the psk), UDP 38899 to the WiZ
bulbs, and 443 to the internet for her voice, search, weather and pushes (plus 80 for ip-api.com, only when
neither `location` nor `web.city` is set).

Keep Ollama (11434) and SearXNG on localhost, and **don't port-forward 8792 to the internet.**

---

## 10. Updating and removing

**Update:** `cd ~/house-ava && git pull && bash deploy/install-houseava.sh`. Your settings in `~/.houseava` and a
`systemctl --user edit` drop-in stay as they are.

**Remove:**

```bash
systemctl --user disable --now houseava
rm -rf ~/houseava ~/.config/systemd/user/houseava.service ~/.config/systemd/user/houseava.service.d
systemctl --user daemon-reload
rm -rf ~/.houseava        # her settings, memory, routines and logs, only if you're sure
```

If you set up the music share, also delete the House Ava line from `/etc/fstab` (the script marks it with a
comment) and `/etc/houseava/music.cred`.

---

## Troubleshooting

- **The Echo never connects.** Check the `psk` (copy the whole thing, including any `=` at the end) and that the
  server can resolve the name (`ping kitchen-echo.local`). If it can't, use the Echo's IP. If you use Home Assistant,
  make sure it isn't also running that Echo as a voice satellite.
- **She hears you but there's no reply out of the speaker.** The Echo can't reach the server on 8790. Look at the
  address in the log's `face streaming from` line: if it's not one the Echo can reach, set `http.public_host` to the
  server's IP. Then check the firewall.
- **Her voice stopped working (HTTP 403 in the log).** edge-tts is too old. `~/houseava/.venv/bin/pip install -U edge-tts`
  (7.3.1 or newer).
- **No face, just the normal TECHO5 screen.** Look for `facecast` in the log. Pillow and noiseprotocol come with
  `requirements.txt`, so "renderer didn't load" (Pillow) or "gone (No module named 'noise')" means the install didn't
  finish.
- **Answers are slow the first time.** The model's loading. See `OLLAMA_KEEP_ALIVE` in step 3.
- **"I don't have any lights in the bedroom".** You named a room that isn't in `lights.rooms`. (And if an Echo's
  own room isn't in `lights.rooms`, "turn off the lights" said there switches every bulb in the house.)
- **Whisper on the GPU fell back to the CPU, or every command fails.** The `LD_LIBRARY_PATH` drop-in is missing or
  points at the wrong Python version folder.
- **Still stuck?** Open a [Discussion](https://github.com/chaoticdopey/house-ava/discussions) with the log
  (`journalctl --user -u houseava -n 200`). Blank out your keys first.
