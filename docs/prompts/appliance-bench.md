# Agent prompt: appliance bench — make the VM usable like a real TV box

**Goal: you can sit in front of the VM and *use* it — see it live in a window,
hear it through the desktop, drive it with your keyboard as the remote, with
real moving content to watch.** Today the VM is only observable through
one-shot screenshots (`vm-shot`) and scripted checks (`vm-key`, `vm-e2e`); you
can't judge whether the OSD, zapping, volume, mute, or standby actually *feel*
right, because there's no sound, no live window, no interactive input, and
nothing to watch. This prompt closes all four so feature work can be validated
by use (SCOPE §7.3: "test on the real TV chain for anything visual"), in the VM
instead of only on the 3040.

This adds **no app features** and changes no viewer-facing behaviour. It makes
the existing dev VM *usable as an appliance*, and in doing so exercises the app
paths the headless checks can't (ALSA audio, the volume/mute/standby experience).

**Current state (2026-10-21, verified against the tree — start from this):**

- `vm/vmctl.py` `qemu_args(..., display="none")` already has a `"spice"` branch
  (SPICE server on port 5910 + the vdagent virtio-serial channel), and
  `cmd_run` already passes `--display` and prints the `spicy`/`virt-viewer`
  hint. But `vm-run` defaults to `--display none`, there is **no task** for the
  live window, and `vm/VAGRANT.md`'s "Running the VM" section still shows only
  `-display none`.
- **There is no audio device.** `qemu_args` emits no `-audiodev`/`-device
  ich9-intel-hda`/`hda-duplex`. `system/mpv/mpv.conf` sets `ao=alsa` with a
  `TODO: pin the exact ALSA device … once the audio path is confirmed on the
  real box`. `alpine/packages.txt` already has `alsa-utils`. The host already
  has the QEMU audio backends available (`none`, `alsa`, `pa`, `pipewire`,
  `spice`, …).
- `vmctl.py key BUTTON` is one-shot (QMP key-down, sleep, key-up, exit). There
  is **no interactive/live remote**; nothing maps your keyboard to remote
  buttons in real time. `KEY_QCODES` and `_key_event()` already exist and are
  reusable.
- The mock stick is empty unless the user stages + transcodes a real library
  into `/var/lib/rabbitears-vm/media/channels/` and runs `uv run task
  usb-build`. There is **no turnkey demo content**.
- `vm/provision.sh` already installs `virt-viewer` and `spice-client-gtk` on
  the host, so the live-display prerequisites are present.

**Prerequisite:** `docs/prompts/appliance-hardening.md` (M5 soak) is *not*
required, but if its `--soak` work has landed, don't break it — the audio and
live-display flags must stay off by default so `vm-e2e`/`vm-soak` remain
headless and deterministic.

Work from the repository root (paths are repo-relative). Read these in full
before making changes: `AGENTS.md`, `SCOPE.md` (especially §2, §3 "Key
packages", §4.3/§4.4/§4.6, §7.3, §10, §11 "Audio path"), `docs/remote.md`,
`vm/vmctl.py`, `vm/VAGRANT.md`, `vm/make-usb.sh`, `vm/e2e.py`,
`pyproject.toml` (`[tool.taskipy]`), `system/mpv/mpv.conf`, `alpine/packages.txt`,
`src/rabbitears/player.py`, and `tests/test_vmctl.py`.

---

## The paradigm (unchanged; every change must respect it)

The rules in `first-channel-vm-playback.md` ("The paradigm" and "Host-side
conventions") still apply in full. Additional rules for this task:

1. **Headless stays the default.** `vm-e2e`, `vm-soak`, and a plain `vm-run`
   must not suddenly require a host display or audio server (CI, ssh-only
   machines). Audio and the live window are opt-in flags, never the default.
2. **The VM mirrors the appliance.** The live remote must drive the *real*
   input path — the emulated `usb-kbd` (vendor `0x0627`) → `evdevd` → FIFO →
   `App.handle` — not a shortcut. That's the whole point of the `usb-kbd`
   stand-in (SCOPE §10). Do not add a VM-only config branch.
3. **No new runtime dependency, no new box-side code.** Audio is a QEMU device
   the guest sees as a normal ALSA card; the guest already ships `alsa-utils`.
   The interactive remote and demo-content generator are host-side only.
4. **Every common action is a task** with a `help` string (AGENTS.md).
5. **Honesty about the physical chain.** QEMU audio validates the app's
   ALSA/volume/mute path and lets you *hear* the result, but it does **not**
   validate the 3040's physical output (HDMI→composite vs headphone jack). Keep
   SCOPE §11's audio question open; record what the VM does and doesn't prove.

---

## Part 1: Audio — hear the appliance

### 1a. `vm/vmctl.py`: an opt-in audio device

Add `audio: str = "none"` to `qemu_args()` and the `Vm` dataclass. When
`audio != "none"`, append (before the `-device` lines, so the device can refer
to the backend):

```
-audiodev <audio>,id=audio0
-device ich9-intel-hda,id=sound0
-device hda-duplex,audiodev=audio0
```

When `"none"`, emit nothing (the current behaviour — e2e must not depend on a
host sound server). Validate the backend against QEMU's list (`pa`, `alsa`,
`pipewire`, `spice`, …) and reject unknown values with a clear message.

- **Default backend is `pa`** (PulseAudio). Debian/Ubuntu desktops run
  PulseAudio or PipeWire-with-a-Pulse-socket, so `pa` is the most portable;
  `pipewire` exists on newer hosts, and `alsa` is the fallback when neither is
  present. Document that a missing sound server makes QEMU fail to start with a
  `Failed to create ... audio backend` error — that's the failure mode to
  report, not to work around.
- Add `--audio {none,pa,alsa,pipewire,spice}` to the `run` subparser (default
  `none`), threaded through `cmd_run` → `Vm`. `spice` routes audio into the
  SPICE client instead of the host; note it as the "audio through the viewer"
  option, but keep `pa` the documented default.

### 1b. Verify audio end-to-end in the VM (the empirical part)

Resolve this in the VM, not by reading docs — the same discipline the OSD
prompt applied to mpv 0.41:

1. Boot with `--audio pa` (and the stick attached). Confirm the guest sees an
   ALSA card: `aplay -l` (or `/proc/asound/cards`) shows the HDA card. If the
   `snd_hda_intel` module isn't autoloaded, add it to `/etc/modules`
   (`setup.sh` + lbu) — but only if you actually observe it missing.
2. Play a test tone in the guest: `speaker-test -t sine -f 440` (or
   `aplay /usr/share/sounds/…` if present). You should hear it on the host.
3. Confirm mpv plays audio through `ao=alsa` with the default device: check
   `/var/log/rabbitears.log` (or run mpv by hand against a clip) and that the
   sound reaches the host.
4. Confirm the app's volume/mute path is audible: `vm-key volup`/`voldown`/`mute`
   (or `rabbitears.ctl` over SSH) changes what you hear, and `vm-key power`
   (standby) silences it. The volume/mute the app sets are mpv's `ao` volume,
   not `amixer` — so no ALSA mixer plumbing is expected, but report what you
   find.

### 1c. `system/mpv/mpv.conf`

Replace the `TODO: pin the exact ALSA device` comment with what Part 1b
established: `ao=alsa` with the default device works in the VM (the QEMU HDA
card) and on the 3040 the exact device is still to be pinned once the physical
path is chosen (SCOPE §11). If you had to pin a device in the VM, record it —
but prefer leaving `ao=alsa` unpinned if the default just works.

---

## Part 2: Live display — see the appliance

`--display spice` is already implemented; this part makes it a first-class,
documented path.

- Add a task `vm-watch` = `python vm/vmctl.py run --display spice --audio pa`
  (help: "Boot the VM with a live SPICE window + audio; then `virt-viewer
  spice://127.0.0.1:5910` and `uv run task vm-remote`").
- Verify `virt-viewer spice://127.0.0.1:5910` (or `remote-viewer`) shows the
  live framebuffer — NO SIGNAL card, then playing video — and that it updates
  in real time (not a static grab). This is the "TV screen".
- Update `vm/VAGRANT.md`'s "Running the VM" section to show the `--display`
  and `--audio` flags and the SPICE connection, instead of only `-display none`.
  Keep `-display none` documented as the headless default for `vm-e2e`.

---

## Part 3: Interactive remote — drive the appliance

New `vmctl.py keys` subcommand: turn the terminal into a live remote. It holds
the QMP connection open for the whole session and translates your keypresses
into remote buttons via `input-send-event` (the real `usb-kbd` path, Part 4 of
`remote-control.md`).

### 3a. Pure translation (unit-testable)

Add a module-level `decode_key(data: bytes) -> str | None` and a `LIVE_KEYS`
map (button name → QEMU qcode, reusing `KEY_QCODES`). `decode_key` returns a
canonical action token or `None` for anything unmapped:

| Terminal input | Action (remote button) |
|---|---|
| Arrow ↑ (`\x1b[A`) | `up` |
| Arrow ↓ (`\x1b[B`) | `down` |
| Enter / `o` | `ok` |
| Backspace / `b` | `back` |
| `+` / `=` | `volup` |
| `-` / `_` | `voldown` |
| `m` | `mute` |
| `p` | `power` |
| `q` / Ctrl-C | quit (not a button) |

Pure: takes bytes, returns a token, no I/O, no clock, no termios. Handles the
single-byte keys and the two arrow escape sequences; the Esc-prefix-vs-arrow
ambiguity is handled in the read loop (below), not here.

### 3b. The loop

- Put stdin into raw, non-echoing mode (`termios` + `tty.setraw`, guarded so
  non-TTY use still works — e.g. piped input degrades to one-shot decode) and
  restore the terminal in a `finally` on every exit path (Ctrl-C included).
- Read available bytes. Escape sequences are read as a unit: after a leading
  `\x1b`, read the following bytes with a short timeout (~50 ms) to distinguish
  a lone Esc (→ `back`) from an arrow. Feed complete sequences to `decode_key`.
- For every token except quit, send key-down, a short hold (~30 ms), key-up
  over the persistent QMP socket, reusing `_key_event()` and `_qmp_exchange()`.
  Print each action (`up`, `volup`, …) as it fires so the user sees what they
  pressed. `q`/Ctrl-C closes the socket and restores the terminal.
- Startup banner: print the key map so the mapping is discoverable.

### 3c. Caveat (record it, don't fix it)

`usb-kbd` is a boot keyboard, so consumer-page keys (likely `back`/`ac_back`,
possibly volume and `power`) may not arrive via `input-send-event` — the same
caveat already documented for `vm-key`. Channel up/down (`up`/`down`) and `ok`
(`ret`) are boot-keyboard keys and must work. For any mapped key that doesn't
arrive, report it and point at `rabbitears.ctl` over SSH (the sanctioned,
reliable path) rather than adding a second transport to this tool. Do **not**
add a VM-only config override or a different emulated device.

### 3d. Task + tests

- Task `vm-remote` = `python vm/vmctl.py keys` (help: "Drive the VM with your
  keyboard as the remote (arrows = channel, Enter = OK, +/- = volume, m = mute,
  p = power, q = quit)").
- `tests/test_vmctl.py`: `decode_key` for every row in the table above, plus
  `None` for unknown bytes; the arrow sequences parse; `LIVE_KEYS` maps to the
  right qcodes. (The raw-terminal loop is exercised by hand in the VM; keep the
  pure function under test.)

---

## Part 4: Demo content — something to watch

Add a host-side generator so the bench is turnkey without a personal media
library (AGENTS.md: no personal paths — synthetic clips are generated, not
copied from a home dir).

- New `tools/make-demo-media.sh` (bash, host-side, ffmpeg already a host dep).
  Generates into `/var/lib/rabbitears-vm/media/channels/` (the same staging dir
  `usb-build` reads), three channels (`01-`, `02-`, `03-`) × 2–3 clips each,
  every clip SCOPE §5-compliant and **with both motion and audio** so video and
  sound are both judgeable:
  - video: `testsrc2` (moving) or `smptehdbars`/`mandelbrot` (colourful), 1280×720,
    30 fps;
  - audio: a `sine` tone (vary the frequency per clip so you can hear volume
    and mute), AAC 48 kHz;
  - `-c:v libx264 -profile:v high -level:v 4.1 -pix_fmt yuv420p -movflags
    +faststart`, 30–120 s each;
  - filenames that read as episode titles (they become on-screen titles, SCOPE
    §4.5), e.g. `Color Bars - S01E01 - Opening.mp4`.
  - Idempotent: skip a clip whose output already exists and passes
    `tools/transcode.sh --check` (reuse the check; don't duplicate it).
  - End with a per-channel file list and a reminder to run `uv run task
    usb-build`.
- Task `media-demo` = `./tools/make-demo-media.sh` (help: "Generate synthetic
  demo clips (motion + audio) into the mock-stick staging dir; then
  `uv run task usb-build`").
- Verify: run it, `uv run task media-check /var/lib/rabbitears-vm/media/channels`
  passes, `uv run task usb-build`, boot with the stick, and confirm multiple
  channels zap with visible motion and audible sound.

---

## Part 5: Docs and decisions

- **`vm/VAGRANT.md`:** add an **"Appliance bench"** section at the top of the
  dev loop, the three-terminal flow:

  ```sh
  # 1. boot with a live window + audio (Ctrl+C stops it)
  uv run task vm-watch
  # 2. the "TV screen" (second terminal)
  virt-viewer spice://127.0.0.1:5910
  # 3. the "remote" (third terminal)
  uv run task vm-remote
  ```

  Then the one-time content setup (`media-demo` → `usb-build` → `vm-watch`) and
  the key map. Note the `usb-kbd` consumer-page caveat and that
  `rabbitears.ctl` over SSH covers any key that doesn't arrive.
- **`AGENTS.md`:** add `vm-watch`, `vm-remote`, `media-demo` to the VM dev-loop
  section; note the bench is the way to judge user-facing features by use.
- **`README.md`:** one line in "Quick start" pointing at the appliance bench.
- **`SCOPE.md`:** §10 decisions log, add:
  - "VM audio is an opt-in QEMU `-audiodev pa` HDA device; it exercises the
    app's ALSA/volume/mute path but not the 3040's physical output" — and a
    note in §11 that the VM audio answer does **not** close the physical-audio
    open question (that's Box A).
  - "The appliance bench: SPICE live window + `vmctl.py keys` interactive
    remote + synthetic demo media; features are judged by use, not screenshots".

---

## Completion criteria

1. `uv run task test`: all pass (only the 4 expected `test_player.IntegrationStubs` skips); the new `decode_key`/`qemu_args`-audio tests pass.
2. `uv run task vm-e2e` still passes with audio and display at their `none`
   defaults (no host sound server or display required).
3. `uv run task vm-watch` + `virt-viewer` shows live video; `vm-shot` still
   works against the same VM.
4. Audio: `aplay -l` shows the HDA card in the guest; a `speaker-test` tone is
   audible on the host; `vm-key volup`/`mute`/`power` audibly change it. Report
   the exact backend that worked on your host (`pa`/`pipewire`/`alsa`).
5. `uv run task vm-remote`: arrow keys zap, Enter toggles the overlay, `+`/`-`
   change volume, `q` quits and restores the terminal. Report which keys
   arrived through the `usb-kbd` and which (if any) needed `rabbitears.ctl`.
6. `uv run task media-demo` → `media-check` clean → `usb-build` → the VM plays
   multiple channels with motion and sound.
7. `uv run task --list` shows `vm-watch`, `vm-remote`, `media-demo` with
   accurate help.
8. No commits unless the user asks.

## Report back with

- Every file created/changed/deleted and what changed in it.
- Full `uv run task test` output and `uv run task --list`.
- The audio result: which QEMU backend worked, the `aplay -l` output, and
  whether the app's volume/mute/standby were audible.
- The live-remote result: which keys arrived via `usb-kbd` and which fell back.
- The demo-content result: channels/clips generated and `media-check` output.
- Anything in this prompt you found wrong or ambiguous and how you resolved it.
