# Install House Ava, step by step

This takes you from nothing to "Alexa, turn off the lights" working on your own Echo. Every step ends with a
**Check** so you know it worked before you move on. Plan on an hour or two, plus however long flashing the Echo takes.

If you get stuck anywhere, post in [Discussions](https://github.com/chaoticdopey/house-ava/discussions) with the step
number and what you saw. When you're done, a [test report](#step-9-send-me-a-test-report) would help me a ton.

Everything I know about every option is in [docs/SETUP.md](docs/SETUP.md). This page is the main path.

- [What you need](#what-you-need)
- [Step 1: Flash your Echo with TECHO5](#step-1-flash-your-echo-with-techo5)
- [Step 2: Set up the server](#step-2-set-up-the-server)
- [Step 3: The brain (Ollama)](#step-3-the-brain-ollama)
- [Step 4: Install House Ava](#step-4-install-house-ava)
- [Step 5: Connect your Echo](#step-5-connect-your-echo)
- [Step 6: Tell her about your house](#step-6-tell-her-about-your-house)
- [Step 7: Talk to her](#step-7-talk-to-her)
- [Step 8: Extras](#step-8-extras)
- [Step 9: Send me a test report](#step-9-send-me-a-test-report)

---

## What you need

**An Echo running TECHO5.** I've only tested the **Echo Show 5 (1st gen)** and **Echo Show 8 (1st gen)**. TECHO5 also
runs on the Show 5 2nd gen, Show 8 2nd gen, Spot (1st gen) and Dot 2nd gen, and House Ava *should* work on them, but
nobody's tried yet. If you have one, you'd be the first, and I'd love to hear how it goes.

**A Linux box that stays on**, on the same network as the Echo. A spare PC, a mini PC or a VM all work.

| | Bare minimum (I haven't tried this) | What I run |
|---|---|---|
| CPU | 4 cores | 4 cores of a Xeon E5-2699 v4 (a Proxmox VM) |
| RAM | 8 GB | 14 GB |
| GPU | none (the brain gets slow) | NVIDIA RTX A2000 12 GB |
| Disk | 40 GB free | 400 GB SSD |
| OS | Debian 13 | Debian 13 |

**Optional:** WiZ bulbs (local, no cloud), a NAS with your music on it, more Echoes.

---

## Step 1: Flash your Echo with TECHO5

Follow [TECHO5's getting started guide](https://github.com/HuskerMinion/techo5/blob/main/docs/getting-started.md).
Use HuskerMinion's own repos, not forks. No Amazon account is involved.

When it's done:
1. **Save the encryption key** the installer prints. It's also in `backups/<serial>/home-assistant.key` in your TECHO5
   folder (older installs: `backups/<serial>/api.psk`). You'll paste it in step 5.
2. **Put it on your Wi-Fi** and note its name, like `kitchen-echo.local`. A DHCP reservation in your router doesn't
   hurt either.
3. **Leave TECHO5's "Voice without Home Assistant" off,** and don't also use the Echo as a voice satellite in Home
   Assistant. House Ava does the answering.

**Check:** the Echo is on TECHO5's screen, on your Wi-Fi, and you have the key.

---

## Step 2: Set up the server

**2a. Install Debian 13.** Grab the netinst image from [debian.org](https://www.debian.org/distrib/). In the
installer:
- At **Software selection**, pick only **SSH server** and **standard system utilities** (no desktop needed).
- Leave the **root password blank**. Then your own user gets `sudo`.

On Proxmox, I'd give the VM 4 cores with **CPU type `host`**, 8–16 GB of RAM and a 64 GB disk. Pass the GPU
through if you have one.

**2b. Give it a fixed address** with a DHCP reservation in your router, so the Echo can always find it.

**2c. SSH in and install the basics:**

```bash
sudo apt update
sudo apt install git curl python3-venv fonts-dejavu-core libnss-mdns
sudo timedatectl set-timezone America/New_York     # yours: timedatectl list-timezones
```

`libnss-mdns` lets the server find `.local` names like your Echo's. The time zone is what her spoken time and the
Echo's screen clock follow.

**2d. NVIDIA GPU (skip it if you don't have one).** The driver's in Debian's `contrib` and `non-free` sections. Turn
those on: add `contrib non-free` to every `Components:` line in `/etc/apt/sources.list.d/debian.sources` (or to each
`deb` line in `/etc/apt/sources.list`). Then:

```bash
sudo apt update
sudo apt install linux-headers-amd64 dkms     # these FIRST, or the driver builds nothing
sudo apt install nvidia-driver
sudo reboot
```

**Secure Boot:** if it's on, the driver won't load. Most PCs ship with it on, and so does a Proxmox VM that uses
UEFI (OVMF) with the default "Pre-Enroll keys". Check with `mokutil --sb-state` (`sudo apt install mokutil` if it's
missing). Then either:
- **turn it off** in the firmware (a Proxmox VM: press Esc as it boots, then Device Manager → Secure Boot
  Configuration, untick Attempt Secure Boot), or
- **keep it on** and, after installing the driver, run `sudo mokutil --import /var/lib/dkms/mok.pub`. Pick a one-time
  password, reboot, and choose **Enroll MOK** on the blue screen (you need a monitor, or the Proxmox console).


**Check:**

```bash
python3 --version              # 3.11 or newer (Debian 13 has 3.13)
ping -c 2 kitchen-echo.local   # your Echo's name: it should answer
nvidia-smi                     # GPU only: shows your card ("couldn't communicate with the NVIDIA driver" = Secure Boot, see 2d)
```

---

## Step 3: The brain (Ollama)

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Then pull a model that fits your GPU:

| Your GPU | Model | Notes |
|---|---|---|
| 12 GB or more | `qwen2.5:14b` | What I use (about 9 GB). Quick and smart |
| 8 GB | `qwen2.5:7b` | Haven't tried it, should be fine |
| No GPU | `qwen2.5:3b` | Haven't tried it. Expect slower, simpler answers |

```bash
ollama pull qwen2.5:14b      # or the one you picked from the table
```

Keep the model loaded so the first answer isn't slow (for me that went from about 27 s cold to half a second):

```bash
sudo systemctl edit ollama
```

Add these two lines in the space it gives you, save, then restart it:

```ini
[Service]
Environment=OLLAMA_KEEP_ALIVE=-1
```

```bash
sudo systemctl restart ollama
```

**Check:**

```bash
ollama run qwen2.5:14b "Say hi in five words."      # your model's name if you picked another
ollama ps                                          # with a GPU, PROCESSOR should say 100% GPU
```

It should answer in a few seconds. If you picked a smaller model, you'll set it in step 6.

---

## Step 4: Install House Ava

As your normal user. The script itself doesn't use sudo:

```bash
sudo loginctl enable-linger $USER        # so she runs from boot, with nobody logged in
git clone https://github.com/chaoticdopey/house-ava ~/house-ava
cd ~/house-ava
bash deploy/install-houseava.sh
```

It copies the code to `~/houseava`, builds a Python environment in `~/houseava/.venv` (a few minutes) and sets her up
as a service. Three folders, easy to mix up:
- `~/house-ava` is your copy of the repo
- `~/houseava` is the install (replaced on every update, so don't edit in there)
- `~/.houseava` is her data: settings, memory, logs

**Check:** near the end the script says

```
No Echo configured yet, so it isn't started. Add each one, then start it:
```

and shows an `add` command. That's the one in step 5.

---

## Step 5: Connect your Echo

Paste your key from step 1 in place of `<key>`. `--room` is the room the Echo's in, and it's what "turn off the
lights" means when you say it to that Echo:

```bash
cd ~/houseava
HOUSEAVA_DIR=~/.houseava .venv/bin/python -m houseava.satellite add \
    --name "Kitchen Echo" --ip kitchen-echo.local --room kitchen --psk '<key>'
systemctl --user restart houseava
journalctl --user -u houseava -f
```

The last command shows her log live (Ctrl+C to get out). Within a minute you should see:

```
reply server on port 8790
admin: no PIN was set, so one was made: it's admin.pin in /home/you/.houseava/houseava_config.json
admin server on port 8792
facecast server on port 8793
Kitchen Echo connected (kitchen-echo.local, room kitchen)
Kitchen Echo: time sync armed (tz ...)
Kitchen Echo: speaker found (speaker)
Kitchen Echo: face streaming from 192.168.1.10:8793
speech-to-text ready (small.en)
```

That PIN is for the phone app. See it with:

```bash
grep '"pin"' ~/.houseava/houseava_config.json
```

The first start also downloads the speech-to-text model, so give it a minute.

**Check:** the Echo's screen switches to her face and a clock, like this:

<img src="docs/screenshots/real-show8.png" alt="House Ava's face and clock on an Echo Show 8" width="480">

Not happening? Look in the log for `connected`. No `connected` line means the key or the name is wrong (see
[troubleshooting](docs/SETUP.md#troubleshooting)).

---

## Step 6: Tell her about your house

**6a. Find your WiZ bulbs** (skip it if you don't have any). Turn them all on, wall switches too, then:

```bash
cd ~/houseava
.venv/bin/python -m houseava.find_bulbs
```

It lists every bulb that answers and prints a `"lights"` block for the config (you'll put it in at 6c). To see
which bulb an address is:

```bash
.venv/bin/python -m houseava.find_bulbs --blink 192.168.1.201
```

That one flashes three times and goes back to how it was.

**6b. Your location.** For the weather and sunrise/sunset routines. On Google Maps, right-click your house, and the
first line is your latitude and longitude.

**6c. Put it in the config.** Stop her first, because she writes the config out from what she loaded when she
started:

```bash
systemctl --user stop houseava
nano ~/.houseava/houseava_config.json
```

The file is already there and has your Echo in it, so **change keys in it, don't replace the whole file**. Find each
of these and set them (with your own names, addresses and numbers):

```json
  "web": {"searxng_url": "", "city": "Springfield"},
  "location": {"lat": 39.80, "lon": -89.64},
  "lights": {
    "bulbs": {"kitchen 1": "192.168.1.201", "kitchen 2": "192.168.1.202", "bedroom 1": "192.168.1.203"},
    "rooms": {"kitchen": ["kitchen 1", "kitchen 2"], "bedroom": ["bedroom 1"]},
    "aliases": {}
  },
```

- Name the bulbs `<room> <number>`, so "bedroom light two" works.
- The room names should match the `--room` you gave your Echo.
- `city` is what she calls the place ("the next 7 days in Springfield"). With `lat`/`lon` set, the weather uses
  those. Without them she looks `city` up by name, which can land on the wrong Springfield.
- Picked a smaller model in step 3? Set it under `"brain"`: `"model": "qwen2.5:7b"`.

Save (Ctrl+O, Enter, Ctrl+X in nano), make sure it still reads, and start her:

```bash
python3 -m json.tool ~/.houseava/houseava_config.json > /dev/null && echo "reads fine"
systemctl --user start houseava
```

If it doesn't say `reads fine`, there's a typo, usually a missing or extra comma. `python3 -m json.tool` shows the
line.

**Check:** "Alexa, turn off the lights" switches off the bulbs in that Echo's room.

---

## Step 7: Talk to her

Say "Alexa", wait for the chime, then try these:

| Say | What should happen |
|---|---|
| "what time is it" | answers right away (no model involved) |
| "turn off the lights" / "turn on the bedroom lights" | the lights in that room / that room |
| "make it blue" / "dim it to 30 percent" | right after a light command, the same lights |
| "what's the weather" / "give me a seven-day forecast" / "what's the weather in Chicago" | today / the week / somewhere else |
| "set a timer for 2 minutes" | it goes off in 2 minutes |
| "tell me a joke" | a joke she hasn't told before |
| "who wrote Moby Dick" | this one uses the model |
| "remember my car is in spot 14", then "where's my car" | she remembers it |

**The phone app.** On your phone (same Wi-Fi), open `http://<your server's address>:8792` and type the PIN from step 5
(`grep '"pin"' ~/.houseava/houseava_config.json`). Five wrong tries in a row and it makes you wait a bit.
- **Status** shows your Echoes.
- **Chat** lets you type to her.
- **Settings** has her personality, voice, swearing and adult jokes switches, and the **Screen** card where you pick
  her face or use your own picture.

**Check:** most of the table works, and the app shows your Echo connected.

---

## Step 8: Extras

All optional, all in [docs/SETUP.md](docs/SETUP.md):
- [Faster, better hearing on an NVIDIA GPU](docs/SETUP.md#gpu-speech-to-text) (whisper `large-v3-turbo`)
- [An offline backup voice](docs/SETUP.md#the-offline-voice-kokoro) for when the internet's down
- [Your own web search](docs/SETUP.md#web-search-searxng) (SearXNG)
- [Music from your NAS](docs/SETUP.md#music-from-your-nas)
- [Faces, voices and your own picture on the screen](docs/FACES-AND-VOICES.md)
- More Echoes: run the `satellite add` line from step 5 again for each one, with its own name and room, then
  `systemctl --user restart houseava`. With two, try "announce dinner's ready" and "intercom the kitchen".
- Routines (like Alexa's) are in the app's **Routines** tab.

**Updating later:** `cd ~/house-ava && git pull && bash deploy/install-houseava.sh`. Your settings stay.

---

## Step 9: Send me a test report

This is the part that helps the most. Open a
**[test report](https://github.com/chaoticdopey/house-ava/issues/new?template=test-report.md)** and tell me:
- your Echo model(s) and TECHO5 version
- your server: CPU, RAM, GPU (or none), Debian or something else
- the model you used, and how fast answers felt
- what worked and what didn't

Here's a checklist you can paste in and tick:

```
- [ ] Echo connects and shows her face and the clock
- [ ] "what time is it"
- [ ] lights: on / off / a room / dim / a color
- [ ] weather: today and the seven-day forecast
- [ ] a timer
- [ ] a question that needs the model
- [ ] a joke
- [ ] the phone app: log in, Chat, change a setting, pick another face
- [ ] reboot the Echo: she's back within a couple of minutes on her own
- [ ] restart the server: she comes back on her own
- [ ] (2+ Echoes) announce, and intercom to another room
- [ ] (NAS) "play some <artist>"
```

**The log** helps a lot when something didn't work:

```bash
journalctl --user -u houseava --since "1 hour ago" > houseava-log.txt
```

Look through it and **blank out anything private** first: what you said to her is in there, and so are your Echo's
name and address. (Your Echo's key and the app's PIN are in the config, not the log.) Then attach it to the report.

Thanks for trying it!
