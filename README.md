# Hearing Device Emulator (QA MVP)

An intentionally small, **experimental** Python CLI that turns a computer with a USB
Bluetooth adapter into a stand-in for a **Cochlear sound processor** (Nucleus 7/8,
Kanso 2). It lets the Flutter / HighlanderSDK audio-routing QA effort run without a
real implant processor. The CLI installs and orchestrates Bluetooth audio receivers,
collects logs, and supports repeatable interruption tests.

## A real Cochlear processor vs. this emulator

A Cochlear processor streams directly from phones over Bluetooth **Low Energy** only:
ASHA on Android, Apple's licensed MFi hearing-device protocol on iPhone. Classic
Bluetooth (A2DP/HFP) appears only through accessories such as the Phone Clip.

| Real Cochlear path | What the phone's audio routing reports | Emulator | Fidelity |
|---|---|---|---|
| Android direct streaming (ASHA, co-developed by Cochlear and Google) | `AudioDeviceInfo.TYPE_HEARING_AID` | `--preset cochlear-asha` (Bumble on macOS/Linux, or BlueALSA on Linux) | Same protocol and route type; LE-only, "Cochlear implant" appearance (Bumble); name and HiSyncId generic |
| Two-sided set: bilateral (two implants) or bimodal (implant + hearing aid) | One grouped hearing-aid device | `cochlear-asha-pair-left` / `-right`, one adapter per side | Same grouping mechanism (shared HiSyncId) |
| iPhone direct streaming (MFi) | `AVAudioSession.Port.bluetoothLE` | none | **Not achievable**: needs Apple licensing and an authentication chip |
| Phone Clip style accessory (classic headset) | A2DP / HFP (`bluetoothA2DP`, `bluetoothHFP` on iOS) | `--preset cochlear-phone-clip` | Close |
| Nucleus 8 LE Audio / Auracast (with a later firmware update) | LE Audio | none | Possible later (Bumble includes HAP) |

What this means for QA:

- **Android:** `cochlear-asha` produces the same route type as a real processor, so
  the app's hearing-aid routing logic is exercised. Name, HiSyncId and GATT details
  are generic, not Cochlear's.
- **iPhone:** the emulator can only produce A2DP/HFP routes, which is what an iPhone
  falls back to without MFi. Testing the `bluetoothLE` code path needs a real processor.
- **Calls:** with ASHA, audio flows one way (phone → processor) and calls use the
  phone's microphone. The HFP microphone feature models a Phone Clip-style headset,
  not the processor.

### Presets

A preset describes the device being imitated. The backend that realizes it is a
separate choice. List them with the route the phone should report:

```bash
python3 -m hearing_emulator presets
```

| Preset | Imitates |
|---|---|
| `cochlear-asha` | One processor streaming from Android over ASHA (monaural) |
| `cochlear-asha-pair-left` / `cochlear-asha-pair-right` | One side of a two-sided ASHA set |
| `cochlear-phone-clip` | Classic headset accessory: A2DP + HFP microphone |
| `a2dp-speaker` | Generic output-only speaker (the iPhone fallback) |

Explicit flags override a preset: `configure --preset cochlear-asha --name CI-Left`.

## How it works

The CLI does no Bluetooth itself. It validates a configuration, then launches and
supervises the programs that do:

```text
configure → ~/.hearing-emulator/config.json   (validated settings)
start     → doctor preflight → backend.commands(config) → session.start() launches:

  BlueALSA (Linux; plugs into the OS Bluetooth stack, BlueZ)
    [daemon]     bluealsad: registers asha-sink / a2dp-sink / hfp-hf with BlueZ,
                 advertises ASHA over LE
    [audio]      bluealsa-aplay: audio from the phone → local speaker
    [microphone] python -m hearing_emulator.workers.microphone (only with --microphone on):
                 tone / wav / ALSA mic → aplay → HFP SCO playback PCM → phone

  Bumble (macOS/Linux; a complete Bluetooth host stack in Python that drives
          the USB adapter directly, bypassing the OS Bluetooth stack)
    [speaker]    bumble-speaker: A2DP sink (--mode a2dp)
    [asha]       python -m hearing_emulator.workers.bumble_asha (--mode asha), run by the
                 Bumble environment's Python: LE-only ASHA hearing device that
                 reports START/STOP/volume and can record or play the audio

session.json records each process's PID and launch time; logs: ~/.hearing-emulator/logs/
Workers with richer state (asha, microphone) also write <label>-status.json for 'status'.
```

## What actually works in this starter

| Capability | BlueALSA on Ubuntu | Bumble on macOS/Linux |
|---|---|---|
| Automated dependency installation | Source build v5.0.0 + D-Bus policy | Isolated Python venv + pip + Realtek firmware |
| A2DP sink | Configured | `bumble-speaker` app |
| ASHA sink, side and HiSyncId | Configured | **Implemented**, tested on a simulated link; awaiting phone validation |
| ASHA runtime feedback (START/STOP, audio type, volume, lost frames, recording) | Logs only | Yes (`status`, `connections`, `--record-audio on`) |
| Dual ASHA + A2DP | Configured, hardware validation needed | **Not implemented** |
| HFP profile registration | Configured (experimental) | **Not implemented** |
| Bluetooth microphone audio injected to phone | **Implemented HFP SCO PCM writer, awaiting device validation** | **Not implemented** |
| Process start/stop/status and logs | Yes | Yes |
| Receiver restart scenario | Yes | Yes |
| Explicit disconnect/connect by phone address | Linux BlueZ only | **Not implemented** |
| iOS MFi hearing-device emulation | **Not supported** | **Not supported** |

**Important:** Process RUNNING status does not mean the phone is connected or the
requested audio route is active. Ubuntu VMware requires a passed-through physical
Bluetooth HCI USB controller. Bumble also requires a real controller to talk to a
physical phone, and macOS USB access may require manual driver/permission setup.

## Choosing a Bluetooth adapter

The emulator is not tied to one adapter model: any adapter that meets the
requirements below should work. The emulator needs a USB Bluetooth adapter of its
own. A Mac's built-in Bluetooth belongs to macOS, and a VM only sees an adapter that
is passed through over USB. On bare-metal Linux, BlueALSA can also use a built-in
controller that meets the same requirements.

### Requirements

| Requirement | Needed for | Why |
|---|---|---|
| USB adapter exposing standard Bluetooth HCI (USB class `E0`) | Bumble; BlueALSA inside a VM | Bumble drives the controller directly over USB. `bumble-usb-probe --hci-only` lists only such adapters |
| **Bluetooth 5.0 or newer** (4.2 is the floor) | ASHA | Android's ASHA spec requires LE Data Length Extension with payloads of at least 167 bytes (Bluetooth 4.2+) and strongly recommends the LE 2M PHY (Bluetooth 5.0) |
| LE peripheral role and LE Secure Connections | ASHA | The emulator is the advertising, connectable device; Android encrypts ASHA links |
| Dual mode (Classic BR/EDR + LE) | `cochlear-phone-clip`, `a2dp-speaker`, `cross-platform` | Classic A2DP/HFP profiles. Pure ASHA is LE-only |
| Not claimed by macOS | Bumble on macOS | Bumble needs exclusive USB access. macOS leaves Realtek- and Nordic-based adapters alone; Broadcom-based ones may be taken by macOS's own Bluetooth stack |
| Controller firmware available to the host | Realtek-based adapters | Realtek chips load firmware at power-up. `install --backend bumble` downloads the images for every Realtek chip Bumble supports; Linux loads them from `linux-firmware` |
| One adapter per emulated side | Two-sided ASHA sets | Each side is a separate LE device with its own address |
| Bluetooth 5.2+ with LE isochronous channels | LE Audio / HAP (future Nucleus 8 work) | Not needed today |

**Avoid** Bluetooth 4.0/4.1 adapters, such as CSR8510 A10 (`0A12:0001`, often
counterfeit) or Broadcom BCM20702 dongles. They lack LE Data Length Extension, so they
cannot meet ASHA's packet-size requirement.

### Example adapters

These are examples, not an exhaustive or endorsed list. None is validated with this
project until the first hardware check passes on it.

| Adapter | Chip | Bluetooth | USB ID | Notes |
|---|---|---|---|---|
| ASUS USB-BT500 | Realtek RTL8761BUV | 5.0 | `0B05:190E` | Dual mode; firmware downloaded by `install` |
| TP-Link UB500 | Realtek RTL8761BUV | 5.0 | `2357:0604` | Dual mode; firmware downloaded by `install` |
| Unbranded "Bluetooth 5.0 USB" adapters with an RTL8761B | Realtek RTL8761B/BUV | 5.0 | often `0BDA:8771` | Works if the USB ID is in Bumble's Realtek list (`doctor` reports it) |
| Nordic nRF52840 Dongle with Zephyr's `hci_usb` firmware | Nordic nRF52840 | 5.x, LE only | Set by the firmware | Listed as known-working by Bumble; ASHA only (no A2DP/HFP); flash once, no host firmware |

### Checking an adapter

After `install --backend bumble`, with the adapter plugged in:

```bash
~/.hearing-emulator/venv-bumble/bin/bumble-usb-probe --hci-only   # Bluetooth adapters and their transport names
~/.hearing-emulator/venv-bumble/bin/bumble-controller-info usb:0  # look for LE_DATA_PACKET_LENGTH_EXTENSION and LE_2M_PHY under "LE Features"
python3 -m hearing_emulator configure --bumble-transport usb:0    # or usb:VID:PID to pick a specific adapter
python3 -m hearing_emulator doctor
```

`usb:0` is the first Bluetooth adapter found. When several are plugged in (for
example, one per side), use the `usb:VID:PID` or `usb:VID:PID#N` names that the probe
prints.

## Getting started (Ubuntu)

Requires Python 3.11 or newer and Ubuntu with `sudo` access.

```bash
cd hearing-device-emulator
python3 -m hearing_emulator install --backend bluealsa
python3 -m hearing_emulator configure --backend bluealsa --preset cochlear-asha
python3 -m hearing_emulator plan
python3 -m hearing_emulator doctor
```

The installer asks before changing system software with `apt`, `make install` and
`sudo install`. It:

1. Checks for BlueALSA v5.0.0 with ASHA and skips the build if it is already there.
   Add `--rebuild` to deliberately recompile and reinstall it.
2. Installs `/etc/dbus-1/system.d/hearing-emulator-bluealsa.conf`. By default only
   root may own the `org.bluealsa` D-Bus name, and only the `audio` group may talk to
   it. This drop-in lets your user run `bluealsad` and `bluealsa-aplay` from `start`
   without root.

No adapter is required for installation, configuration, planning or tests. To also
install Bumble on Ubuntu, use `install --backend bumble`, or `--backend all` for both.

### Free the Bluetooth audio profiles (PipeWire / PulseAudio)

The Ubuntu desktop's own audio server registers A2DP and HFP endpoints with BlueZ.
With two stacks registered, the phone may connect to the desktop instead of the
emulator. `doctor` reports this as "competing Bluetooth audio stack" and `start`
refuses to run until it is resolved. Check your version with `wireplumber --version`.

WirePlumber 0.5 (Ubuntu 24.10 and later): create
`~/.config/wireplumber/wireplumber.conf.d/80-disable-bluez.conf` containing

```text
wireplumber.profiles = {
  main = {
    monitor.bluez = disabled
  }
}
```

WirePlumber 0.4 (Ubuntu 22.10 – 24.04): create
`~/.config/wireplumber/bluetooth.lua.d/80-disable-bluez.lua` containing

```text
bluez_monitor.enabled = false
```

Then run `systemctl --user restart wireplumber`. Delete the file to restore desktop
Bluetooth audio. With PulseAudio (Ubuntu 22.04), run
`pactl unload-module module-bluetooth-discover` (lasts until the next login).

Also stop any BlueALSA system service, e.g. `sudo systemctl stop bluealsa`.

## Getting started (Apple Silicon macOS)

Bumble requires Python 3.11+. The macOS `/usr/bin/python3` is too old, so install one
from python.org or Homebrew first. Use the same extracted directory on macOS:

```bash
python3 -m hearing_emulator install            # defaults to Bumble outside Linux
python3 -m hearing_emulator configure --backend bumble --preset cochlear-asha \
  --bumble-transport usb:0 --name CI-Left
python3 -m hearing_emulator plan
python3 -m hearing_emulator doctor
```

Use `--preset a2dp-speaker` instead for the iPhone fallback route (Bumble's A2DP speaker).
See [Choosing a Bluetooth adapter](#choosing-a-bluetooth-adapter) for what to plug in
and how to pick its transport name.

### First hardware check (ASHA on Bumble)

This proves the adapter, Bumble and an ASHA-capable Android phone work together
(Android 10+; Pixel and recent Samsung phones support ASHA):

1. Plug the adapter into the Mac, then `doctor`: all checks should pass.
2. `configure --backend bumble --preset cochlear-asha --record-audio on`, then `start`.
3. `logs --component asha --follow` should show `ADVERTISING: 'CI-Left' ...`.
4. On the phone: **Settings → Connected devices → Pair new device**. The emulator is
   listed as a hearing aid. Pair it; the log shows `CONNECTED` and `bonded`.
5. Play media on the phone: the log shows `START MEDIA audio, volume ... dB`, then
   progress lines with seconds received and frames lost.
6. Stop playback (`STOP; ...`). With `--record-audio on` the stream is saved under
   `~/.hearing-emulator/recordings/`; listen to it to confirm the content.
7. Check the app under test: Android should report `TYPE_HEARING_AID` for this device.

Record which adapter model passed (or failed) so the example list above can grow.

Realtek-based adapters run on firmware that Bumble uploads at power-on. `install`
downloads the images for every Realtek chip Bumble supports (about 0.7 MB), and
`doctor` checks them when a Realtek adapter is plugged in. Bumble 0.0.236's own
`bumble-rtk-fw-download` command fails on an import error, so the installer runs its
downloader directly.

The adapter must be **connected to macOS**, not attached to the VMware VM, when
running Bumble on the host. If `bumble-usb-probe --hci-only` cannot see the adapter,
follow Bumble's USB transport documentation. The CLI deliberately does not change
macOS security settings or automatically unbind system drivers.

**Do not use Bumble's USB backend and BlueZ/BlueALSA against the same USB
adapter at the same time.** Linux may also need manual controller-ownership setup
before Bumble can claim an adapter from the kernel Bluetooth driver.

## Running the emulator

Show the commands to be launched without hardware:

```bash
python3 -m hearing_emulator configure --interactive
python3 -m hearing_emulator plan
```

`plan` also prints the audio route the phone should report, so QA can compare it with
what the app observes.

After plugging in and passing through your adapter:

```bash
python3 -m hearing_emulator doctor
python3 -m hearing_emulator start
python3 -m hearing_emulator status
python3 -m hearing_emulator connections
python3 -m hearing_emulator logs --tail 50
python3 -m hearing_emulator logs --follow
python3 -m hearing_emulator stop
```

`start` refuses failed checks unless `--force` is specified, which is **only** for
debugging. Bumble's browser status UI is at `http://127.0.0.1:7654` by default.

## Pairing

**ASHA on Bumble.** The emulator advertises over LE only (no classic profiles, like a
real processor) and needs no `bluetoothctl`: pair from Android **Settings → Connected
devices → Pair new device**. Bonds persist in `~/.hearing-emulator/bumble-asha-keys.json`,
so the phone reconnects after `stop`/`start`. The address and HiSyncId are derived from
name, side and sync ID: change any of them and the phone sees a new device (forget the
old one on the phone).

**ASHA on BlueALSA (`cochlear-asha`, `--mode asha`).** `bluealsad` advertises the ASHA service
over LE by itself. A real processor is LE-only, so keep classic discovery off:
otherwise the phone may pair a classic profile instead of the hearing-aid route. Pair
from Android **Settings → Connected devices → Pair new device**, where hearing aids
are listed. Keep a `bluetoothctl` agent open for the bonding prompt:

```text
bluetoothctl
power on
agent NoInputNoOutput
default-agent
pairable on
```

**Classic (A2DP / HFP / cross-platform).** Also make the adapter discoverable:

```text
system-alias Hearing-Emulator
discoverable on
```

The iPhone pairs A2DP/HFP in its regular Bluetooth settings, not in the MFi hearing
devices menu.

## ASHA runtime feedback (Bumble)

The ASHA worker reports what the phone actually asked for, which is what routing
tests need to assert:

```bash
python3 -m hearing_emulator status        # ASHA state: streaming | START MEDIA audio, volume -20 dB, ...
python3 -m hearing_emulator connections   # current state and bonded phones
python3 -m hearing_emulator logs --component asha --follow
```

| State | Meaning |
|---|---|
| `advertising` | Discoverable/reconnectable; no phone connected |
| `connected` | A phone is connected (and bonded, once paired); no stream |
| `streaming` | Phone sent START: audio type (`MEDIA`, `PHONE_CALL`, `RINGTONE`), volume in dB, other side's state; progress every 2 s with seconds received and frames lost |
| `stopped` | Worker exited, e.g. adapter unplugged (`Bluetooth transport closed`) |

"Frames lost" counts gaps in ASHA's per-frame sequence counter, i.e. audio the phone
sent that never arrived. `--record-audio on` saves each START…STOP stream as a 16 kHz
mono WAV in `~/.hearing-emulator/recordings/`; with `--playback on` and `ffplay`
installed (`brew install ffmpeg`), audio also plays on the computer.

## Configuration reference

### ASHA identity

```bash
# One processor, right ear
python3 -m hearing_emulator configure --preset cochlear-asha --asha-side right

# Two-sided set: each side is its own LE device, so run one per adapter/host.
python3 -m hearing_emulator configure --preset cochlear-asha-pair-left --name CI-Left
#   → prints a generated sync ID; on the second host:
python3 -m hearing_emulator configure --preset cochlear-asha-pair-right --name CI-Right \
  --asha-sync-id <ID printed above>
```

`--asha-binaural on` marks the device as one half of a pair (BlueALSA `--asha-side=left+`).
`--asha-sync-id` is the 64-bit HiSyncId that Android uses to group both sides as one
device. An ID is generated if a two-sided preset has none.

### Profile configurations

```bash
# Android ASHA receiver
python3 -m hearing_emulator configure --backend bluealsa --mode asha --microphone off

# iOS output-only Bluetooth speaker
python3 -m hearing_emulator configure --backend bluealsa --mode a2dp --microphone off

# Both profiles on same BlueALSA Linux receiver (experimental hardware-dependent)
python3 -m hearing_emulator configure --backend bluealsa --mode cross-platform --microphone off

# HFP headset with a deterministic 440 Hz test microphone signal
python3 -m hearing_emulator configure --backend bluealsa --mode a2dp \
  --microphone on --mic-source tone --mic-frequency 440 --mic-gain 0.2

# HFP headset with silence (tests microphone presence without actual sound)
python3 -m hearing_emulator configure --backend bluealsa --mode a2dp \
  --microphone on --mic-source silence

# HFP headset with an Ubuntu physical microphone (ALSA capture)
python3 -m hearing_emulator configure --backend bluealsa --mode a2dp \
  --microphone on --mic-source alsa --mic-alsa-device plughw:1,0

# HFP headset with a prerecorded WAV (must be mono, PCM 16-bit, 8 kHz by default)
python3 -m hearing_emulator configure --backend bluealsa --mode a2dp \
  --microphone on --mic-source wav --mic-wave-path /home/ubuntu/test-voice.wav

# Optional: explicitly target the paired phone, or request 16 kHz PCM for mSBC
python3 -m hearing_emulator configure --mic-peer AA:BB:CC:DD:EE:FF --mic-sample-rate 16000

# Bumble on macOS: ASHA hearing device, or A2DP speaker (no HFP microphone)
python3 -m hearing_emulator configure --backend bumble --mode asha --record-audio on
python3 -m hearing_emulator configure --backend bumble --mode a2dp --microphone off
```

## Microphone (HFP)

Bluetooth HFP is bidirectional. When microphone is ON, the BlueALSA backend
registers `hfp-hf` and runs a supervised Python worker that sends PCM into its
**SCO playback endpoint**. This is the emulated headset microphone in the phone's
HFP audio route. The worker supports tone, silence, looping WAV, and ALSA
physical capture, with a retry loop after the link disappears.

The microphone worker does **not** inject audio into ASHA or A2DP: ASHA and A2DP
remain output-only. When both A2DP and HFP are enabled the phone may choose HFP,
which changes the audio codec and iOS route. Keep microphone OFF when testing
output-only device disappearance, and for a clean A2DP regression set
`--microphone off` and restart the emulator. Bumble is still A2DP-only, since
Bumble's `bumble-speaker` app has no SCO microphone audio pipeline integrated here.

### Microphone diagnostics and offline test

No dongle is needed to generate and inspect the test signal:

```bash
python3 -m hearing_emulator mic-sample --output ./mic-test.wav --seconds 3
python3 -m unittest discover -s tests -v
```

The optional test WAV `examples/test-tone-8khz.wav` validates file input without a
hardware microphone. For a USB microphone, check `arecord -l` first and substitute
its ALSA capture PCM.

Once BlueALSA and the USB dongle are working and the phone selects an HFP
headset audio route, observe the worker:

```bash
python3 -m hearing_emulator status
python3 -m hearing_emulator logs --component microphone --follow
bluealsa-aplay -L  # look for PROFILE=sco with playback direction
```

The worker reports WAITING until it sees an HFP SCO PCM and FEEDING_PCM when
writing to the `aplay` process. **This is not confirmation that the phone received
or selected the emulated microphone.** Validate remotely by recording a call
with the iPhone's microphone route set to the HFP headset and checking that the
injected test signal is audible on the **remote peer**. For physical input in
VMware, an actual USB microphone may need USB passthrough as well.

The default sample rate is 8000 Hz (CVSD); 16000 Hz is an option for mSBC
experiments, but actual codec selection is controlled by the phone/HFP gateway.
WAV input must already match the selected format; the tool deliberately
rejects files needing resampling. You can convert files with a separate audio
tool if needed. The physical Bluetooth controller still must support SCO/eSCO
transport on Ubuntu.

## Fault and reconnection scenarios

```bash
# Linux BlueZ only; use the actual connected phone's Bluetooth address
python3 -m hearing_emulator scenario disconnect --address AA:BB:CC:DD:EE:FF
python3 -m hearing_emulator scenario connect --address AA:BB:CC:DD:EE:FF

# Both backends: turns emulator off, waits 5 seconds, then restarts
python3 -m hearing_emulator scenario restart --delay 5
```

The phone operating system decides whether a disconnected link reconnects.
"Restart" validates receiver service recovery, **not automatic handset reconnect**.

## Project layout

The package has three layers. The CLI process runs the top-level modules and
`backends/`. The backends start processes, some of which are this project's own
`workers/`.

```text
hearing_emulator/
  cli.py                 # CLI: configure, presets, install, doctor, plan, start, stop, logs, connections, scenario
  config.py              # Config schema, validation, JSON storage, backend capability table
  presets.py             # Named device presets and the phone route each profile produces
  ops.py                 # Observable safe subprocess helpers
  session.py             # Managed child processes, PID + launch-time checks, text logs
  backends/              # Imported by the CLI: install, doctor, and the commands to launch
    bluealsa.py          # Ubuntu build, D-Bus policy, profiles, conflict and controller diagnostics
    bumble.py            # Venv installer, Realtek firmware, A2DP speaker / ASHA commands, USB diagnostics
  workers/               # Separate processes launched by a backend (python -m hearing_emulator.workers.<name>)
    microphone.py        # BlueALSA: HFP SCO microphone PCM worker + offline WAV test
    bumble_asha.py       # Bumble: ASHA hearing-device worker (runs in the Bumble venv, imports only Bumble + stdlib)
tests/
  test_config.py         # Config, presets, command builders, host-check parsers
  test_microphone.py     # Offline HFP worker/PCM config tests
  test_session.py        # Process start/status/stop and PID-reuse protection
  test_bumble_asha.py    # ASHA flow against a simulated phone (needs Bumble)
examples/
  test-tone-8khz.wav     # 8 kHz mono test file for --mic-source wav
```

Run offline tests:

```bash
python3 -m unittest discover -s tests -v
# Same suite inside the Bumble environment also runs the ASHA tests (skipped above):
~/.hearing-emulator/venv-bumble/bin/python -m unittest discover -s tests -v
```

The ASHA tests run the real worker against a Bumble central on a simulated radio
link, acting like Android: read properties, START, 20 ms G.722 frames over the LE
credit-based channel, volume, STOP, disconnect.

### Adding a mode, backend or preset

- **Preset:** add an entry to `PRESETS` in `presets.py`.
- **Backend:** add a module with `installed()`, `install()`, `commands()` and
  `doctor()`, register it in `cli.BACKENDS`, and declare what it supports in
  `config.BACKEND_MODES` / `config.BACKEND_MICROPHONE`.
- **Mode:** add it to `config.MODES` and `Config.profiles`, then to each backend's
  `commands()` and, if it introduces a new profile, to `presets.PROFILE_ROUTES`.
- **Worker:** when a backend needs a long-running process of this project's own,
  add a module to `workers/` with a `main()` and launch it from the backend's
  `commands()` with `-m hearing_emulator.workers.<name>`. If it should show more than
  RUNNING/STOPPED, write `<label>-status.json` (`pid`, `state`, `detail`) and add it to
  `cli.WORKER_STATUS`.

## Important limitations & next milestones

- **Hardware unvalidated.** Run the first hardware check with each adapter model, on Ubuntu and macOS separately.
- **ASHA audio unvalidated.** Check LE peripheral advertising and LE L2CAP CoC on the real adapter.
- **iOS MFi unavailable.** A2DP does not reproduce iOS `bluetoothLE` hearing device routes.
- **Bumble ASHA awaiting phone validation.** Tested on a simulated link, not yet with
  a real Android phone and adapter (see "First hardware check"). One side per machine:
  a two-sided set needs two computers, each with its own adapter.
- **Bumble upstream issues worked around.** Its G.722 decoder raises `OverflowError` on
  valid frames (missing 16-bit saturation), `AshaService` reports volume as an unsigned
  byte, and `bumble-rtk-fw-download` fails to import `tools.rtk_util`. The emulator
  works around all three; worth reporting upstream.
- **ASHA Device Information Service.** Android's ASHA spec asks the hearing device to
  implement the Device Information Service (manufacturer and model names); the Bumble
  emulator doesn't expose it yet.
- **LE Audio (Nucleus 8).** Bumble also ships the Hearing Access Profile
  (`bumble.profiles.hap`); it needs a Bluetooth 5.2+ adapter with isochronous channels.
- **Windows.** Bumble is cross-platform; on Windows the adapter likely needs a WinUSB
  driver. Not yet tried.
- **Microphone pipeline (BlueALSA only).** Generated and physical PCM → HFP SCO is coded and offline-tested, but end-to-end phone mic capture and SCO hardware support remain unverified. Bumble HFP SCO needs a separate implementation.
- **Better runtime feedback.** Add D-Bus PCM state monitoring for BlueALSA and event hooks for Bumble (connected/streaming/disconnected), rather than relying just on logs.
- **Reconnection tests.** Add timed orchestration and assert fallback/resume using phone app diagnostics.
- **Pairing/security.** Make BlueZ agent lifecycle explicit; avoid implicit host-wide Bluetooth changes.
- **Release hardening.** Pin the Bumble version, add packaging, support Mac controller permissions safely, add end-to-end hardware tests.

## Glossary

- **ASHA**: Audio Streaming for Hearing Aids. Google's LE protocol for hearing devices
  on Android 10+, one-way audio (G.722, 16 kHz) over LE L2CAP connection-oriented channels.
- **HiSyncId**: 8-byte ASHA identifier; both sides of a pair share it so Android
  treats them as one device.
- **MFi hearing device**: Apple's licensed LE hearing-aid protocol. iOS exposes it as the
  `bluetoothLE` audio route.
- **A2DP**: classic Bluetooth stereo audio, phone → receiver.
- **HFP / SCO**: classic Bluetooth hands-free profile; SCO is its two-way voice link.
- **LE Audio / HAP**: the newer LE audio standard and its Hearing Access Profile
  (Nucleus 8 after a firmware update).
- **BlueZ**: the Linux Bluetooth stack. **BlueALSA**: a BlueZ audio backend exposing
  Bluetooth audio as ALSA devices. **Bumble**: Google's Python Bluetooth stack that
  drives a USB controller directly over **HCI** (the host–controller interface).

## Project source references

- https://github.com/arkq/bluez-alsa/wiki/Installation-from-source
- https://github.com/arkq/bluez-alsa/blob/v5.0.0/doc/bluealsad.8.rst
- https://github.com/arkq/bluez-alsa/blob/v5.0.0/doc/bluealsa-aplay.1.rst
- https://github.com/arkq/bluez-alsa/blob/v5.0.0/doc/bluealsa-plugins.7.rst
- https://google.github.io/bumble/apps_and_tools/speaker.html
- https://github.com/google/bumble/blob/main/docs/mkdocs/src/transports/usb.md
- https://github.com/google/bumble/blob/main/bumble/profiles/asha.py
- https://source.android.com/docs/core/connect/bluetooth/asha
- https://pronews.cochlear.com/connect-apple-and-android-streaming/
