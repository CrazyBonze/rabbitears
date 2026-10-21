# Rabbit Ears — Appliance Bench Report

Date: 2026-10-21
Target: KVM VM (`rabbitears-vm`), booted with the emulated USB stick (`vm-run-usb`)
and the emulated `usb-kbd` remote. All interaction via taskipy tasks
(`vm-key`, `usb-unplug`/`usb-plug`, `vm-powerbutton`, `vm-shot`); all
verification via screenshots and the app log. No `rabbitears.ctl`, no
`/opt` inspection, no app changes.

## Media

14 files, 3 channels, 2–5 min each, cut from `/media/media/Filler`
(read-only) into `/home/michael/Videos/rabbitears-bench/` and transcoded with
`uv run task transcode` (all pass `media-check`):

| Channel | Files | Notes |
|---|---|---|
| `01-holiday-classics` | 4 (Charlie Brown, Yogi, Pooh, Shrek the Halls) | `channel.toml`: `name = "Holiday Classics"`, `shuffle = true` |
| `02-cartoon-toons` | 6 (Tom & Jerry ×2, Curious George ×2, Trolls, Olive) | `channel notes.txt` sibling file (must be ignored) |
| `03-holiday-adventures` | 4 (Santa's Apprentice, Grandma, Murray, Little Spirit) | |

The bench's "duplicate-name file" item was dropped as redundant (per user);
it is not representable on one filesystem and the index is keyed by filename.

## Checks

| Check | Result | Evidence |
|---|---|---|
| Boot → playing a channel | PASS | `bench-01-boot-playing.png`; log: `CH 01 · Holiday Classics — Yogi's First Christmas … +2m01s (position 842s / loop 1020s)` (842 = 3×240 + 122 ✓) |
| Channel up → CH 02 | PASS | `bench-02`/`bench-03`; log: `remote: channel_up -> CH 02 · Cartoon Toons`, `position 242s / loop 1501s` (242s into Curious George S01E01 ✓) |
| Channel up → CH 03 + flash | PASS | `bench-04-zap-ch03-flash.png` shows `CH 03` flash top-right in green VCR font; log: `channel_up -> CH 03 · Holiday Adventures` |
| Channel down ×2 wraps to CH 01 | PASS | `bench-05`; log: `channel_down -> CH 02`, `channel_down -> CH 01` |
| OK → info overlay | PASS | `bench-06-info-overlay.png`: CH, name, title, `2m 43s remaining`; log: `info -> overlay shown` |
| Back → dismiss overlay | **FAIL (VM only)** | `vm-key back` (qcode `ac_back`) produced **zero** app events in 8+ presses. QEMU `usb-kbd` does not deliver consumer-page AC Back to the guest. Overlay can only be dismissed via OK (toggle). See Findings #1. |
| Volume up/down | PASS | log: `volume_up -> 55 (cap 80)`, `volume_down -> 50 (cap 80)` (5s steps, capped) |
| Mute | PASS | log: `mute on` / `mute off` |
| Power → standby (black) | PASS | `bench-08-standby-black.png` is fully black; log: `power standby` |
| Power → wake, retunes | PASS | `bench-09-wake-playing.png`; log: `power on` |
| USB unplug → NO SIGNAL | PASS | `bench-10-no-signal.png` (test pattern); log: `media removed — NO SIGNAL`, `waiting for media at /media/rabbitears` |
| USB replug → retune | PASS | `bench-11-replug-retuned.png`; log: re-index + `CH 01 … Shrek the Halls +1m13s (position 314s / loop 1020s)` (314 = 240 + 74 ✓) |
| `.txt` sibling ignored | PASS | log: `skipping channel notes.txt: not .mp4 — transcode with tools/transcode.sh`; channel plays its 6 mp4s |
| `channel.toml` name override | PASS | log/OSD show `CH 01 · Holiday Classics` |
| `channel.toml` shuffle | **PARTIAL** | Parsed and stored, but never applied — playlist is always filename-sorted. See Findings #3. |
| ACPI power button → no shutdown | PASS | `bench-12-powerbutton-still-on.png` (still playing); `acpid: PWRF/00000080` no-op hook fired; box still up (uptime, SSH) |
| Reboot → state persists | PASS | state.json before **and** after: `{"channel": 1, "volume": 50}`; boot log: retuned to `CH 01 · Holiday Classics — Shrek the Halls +2m44s (position 404s / loop 1020s)` (404 = 240 + 164 ✓); `bench-13-reboot-retuned.png` |
| Drift check | PASS | `uv run task vm-verify` → `verify: OK` |

## Screenshots

In `docs/screenshots/`: `bench-01-boot-playing.png`, `bench-02-zap-ch02-flash.png`,
`bench-03-ch02-playing.png`, `bench-04-zap-ch03-flash.png`,
`bench-05-wrap-to-ch01.png`, `bench-06-info-overlay.png`,
`bench-07-back-key-noop-overlay-still-up.png`, `bench-08-standby-black.png`,
`bench-09-wake-playing.png`, `bench-10-no-signal.png`,
`bench-11-replug-retuned.png`, `bench-12-powerbutton-still-on.png`,
`bench-13-reboot-retuned.png`.

## Findings

1. **`vm-key back` is dead in the VM (emulation gap, not an app bug).**
   QMP `input-send-event` with qcode `ac_back` never reaches the guest's
   evdev layer (0 events in 8+ presses, while `volumeup`/`volumedown`/
   `audiomute`/`power` — also consumer/system-page — all arrive). QEMU's
   `usb-kbd` scancode→HID table evidently has no entry for AC Back
   (scancode 0xe012), so the key is dropped. The real Adomax dongle sends
   KEY_BACK (158) directly over evdev, so the box itself is fine. Options:
   (a) accept it — OK toggles the overlay, so nothing is unreachable;
   (b) have `vmctl.py key back` write a `back 1\nback 0` line to the button
   FIFO over SSH (a test action, like `e2e-remote.py`); (c) patch/replace the
   emulated keyboard.
2. **Long overlay titles overflow the screen edge.**
   `A Charlie Brown Christmas - S01E01 - The Tree` at the overlay's 36 px
   runs off the right edge (trailing `-` clipped; see `bench-06`).
   mpv `show-text` does not wrap. Titles ≳30 chars are affected; consider
   truncating/wrapping the title line in `osd.py`.
3. **`shuffle = true` is parsed but never applied.**
   `channels.py` reads it into `Channel.shuffle`, but `App`/`schedule.py`
   always play filename-sorted order (SCOPE §4.2 promises seeded shuffle).
   Unimplemented feature, not a crash — the box plays deterministically.
4. **VM app log is append-only across reboots** (VM is a standard install,
   `/var/log` is on disk; the box is RAM-root and won't do this). Old boots'
   entries accumulate, which made mid-file forensics confusing during the
   bench. Cosmetic for the VM; consider truncating at service start.
5. **ALSA noise in the VM log** (`Unknown PCM default`, `cannot find card
   '0'`): QEMU has no sound card attached; expected, video-only validation.

## state.json (reboot test)

```
before: { "channel": 1, "volume": 50 }
after:  { "channel": 1, "volume": 50 }
```

## Remaining risks

- **VAAPI/hardware decode untested** — the VM uses virtio-gpu (software
  decode); SCOPE §5 compliance is verified by `media-check`, not by the
  3040's i965 driver.
- **Audio untested** — no sound card in the VM; volume/mute verified via
  log lines only.
- **Back button** (Finding #1) is the one remote function that cannot be
  exercised in the VM at all.
- **Real-remote behaviour** (RF pairing, hold-to-repeat on the physical
  dongle, hot-plug of the dongle) is only approximated by `usb-kbd`.
- The bench ran one unplug/replug cycle; the M5 "pull-the-plug ×20"
  endurance test has not been done here.
