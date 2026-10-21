# Rabbit Ears — dev VM

A KVM VM that mirrors the Wyse 3040 (4 vCPU, 2 GB RAM, 8 GB disk) for
developing and validating the Rabbit Ears stack before touching a real box.

**What it validates:** the DRM/KMS output path (`mpv --vo=gpu`), the app,
OpenRC wiring, and media handling. **What it does NOT validate:** hardware
decode (VAAPI/i965). virtio-gpu has no VAAPI driver, so hwdec is checked only
on the real 3040 (M1). The VM decodes in software; non-compliant media
"works" here but would fail on the 3040.

---

## The loop

```sh
uv run task setup-sudoers   # once per clone (passwordless sudo for provision.sh)
uv run task vm-build        # build the VM; leaves the disk at rest
uv run task vm-run &        # boot in QEMU (4 vCPU, 2 GB, SSH on 2222)
uv run task deploy-vm       # deploy repo, reboot, verify
uv run task vm-shot         # screenshot to vm/screenshot.png
```

Or prove the whole pipeline in one go:

```sh
uv run task vm-e2e              # boot + stick, deploy, check playback/schedule/unplug/replug/crash
uv run task vm-e2e --rebuild    # same, starting from a freshly built VM
```

Under the hood:

| Step | Script |
|---|---|
| `vm-build` | `vm/provision.sh` → `vm/install.py` (libvirt domain, unattended `setup-alpine`) |
| `vm-run` | `vm/vmctl.py run` (raw QEMU, virtio-gpu, user-net, QMP) |
| `deploy-vm` | `alpine/deploy.sh --vm --reboot` (tar over ssh → `setup.sh` → `verify.sh`) |
| `vm-shot` | `vm/vmctl.py screenshot` (QMP `screendump`) |
| `vm-stop` | `vm/vmctl.py stop` + `vm/provision.sh stop` (both verify the QEMU died) |
| `vm-e2e` | `vm/e2e.py` (see below) |

---

## When `vm-build` fails

`install.py` streams setup-alpine's progress (`setup-alpine: Keymap (+0s)`, …)
and writes **every serial byte** to `/var/lib/rabbitears-vm/install-serial.log`
(`uv run task vm-install-log` shows the tail). It fails fast, with the reason
and the last console lines, when:

| Symptom | Cause / fix |
|---|---|
| `the guest is waiting for interactive input: '…'` | An answerfile option is unset or invalid, so setup-alpine asked a question. Set it in `alpine/answerfile`; `tests/test_install.py` lists the required ones. |
| `setup-alpine is stuck in its 'X' section` | The section overran its budget. `APK Mirror`/`User`/`Disk & Install` usually means the apk repos or proxy (`/var/log/rabbitears-apk-proxy.log`). |
| `the system disk has not grown …` | `setup-disk` never started writing. |
| `apk repo … does not answer` (from provision.sh) | The proxy is down or the answerfile's repo URLs are wrong. `uv run task vm-proxy`. |
| `the VM disk … is in use by: …` | Another QEMU holds the disk. `uv run task vm-stop`. |

The failed live VM is destroyed so it can't hold the disk.
`uv run task vm-build --keep-on-failure` keeps it running instead, for
`virsh -c qemu:///system console rabbitears-vm`.

---

## End-to-end check (`vm-e2e`)

`vm/e2e.py` runs the pipeline exactly as a developer would, then checks the
result from the outside. Each step prints `PASS`/`FAIL` with evidence:

| Check | Pass condition |
|---|---|
| preflight | stick image + disk exist, no QEMU holds the disk |
| rebuild (`--rebuild`) | `provision.sh destroy` + create succeed |
| boot, ssh | `vmctl.py run --usb …` up, SSH answers on :2222 |
| deploy | `deploy.sh --vm --reboot` ends with `verify: OK` |
| service, mount | `rabbitears` started; `/dev/sd*` exFAT on `/media/rabbitears` (mdev) |
| playing, schedule | mpv plays a stick file; file + `time-pos` match `(now − epoch) mod loop` within ±3 s, computed on the host from `index.json` |
| motion | two screendumps 3 s apart differ and aren't black |
| remote power-off/on | `e2e-remote.py power` → NO SIGNAL, then `power` → playing again |
| remote zap, autofire, volume | `e2e-remote.py` drives the button FIFO: `channel-up` re-tunes, a 1.5 s `hold` autofires, `vol-up` raises mpv volume |
| osd flash | a zap puts green `CH NN` in the top-right (green pixels above a pre-zap baseline); it's gone ~3 s later |
| osd overlay | `ok` → `ctl status` `overlay: true` + a screenshot; `back` → `false`; `ok` twice toggles on/off |
| osd overlay + zap | with the overlay up, a zap keeps `overlay: true` and re-tunes |
| osd overlay + standby | with the overlay up, `power` → `standby: true, overlay: false`; wake stays off |
| osd remaining | `ctl status` `remaining` ≈ file duration − `time-pos`, within a few seconds |
| unplug | QMP unplug → lazy unmount, mpv on the NO SIGNAL card, log line |
| replug | QMP plug → remount, playing again at the schedule position |
| crash | `pkill -9 mpv` → respawned and playing |
| verify | `verify.sh --vm` still OK after the test actions |
| shutdown | vmctl exits, no QEMU left holding the disk |

Output (serial log, deploy log, screenshots, `report.json`) goes to
`/tmp/rabbitears-e2e/`. The VM is always shut down at the end, even after a
failure (`--keep-running` leaves it up). Other flags: `--usb IMG`,
`--skip-crash`, `--run-dir DIR`. Prerequisites: `vm-build` and `usb-build`.

---

## Prerequisites (host)

- Debian/Ubuntu with `/dev/kvm` (you are in the `kvm`/`libvirt` group — `vm-build` adds you; already-open terminals need a re-login, or use `sg kvm -c "uv run task vm-run"` as a stopgap).
- **uv** (the project pins Python 3.14, matching the VM/appliance; `vmctl.py` is stdlib-only).
- `qemu-system-x86_64` and libvirt (installed by `provision.sh` on first run).

Confirm KVM is usable:

```sh
egrep -c '(vmx|svm)' /proc/cpuinfo   # > 0
ls -l /dev/kvm                        # exists and you can read/write it
```

---

## Running the VM

```sh
uv run task vm-run
# or directly:
uv run vm/vmctl.py run
```

This boots the VM and **streams the serial console to stdout**. The disk
defaults to `/var/lib/libvirt/images/rabbitears-vm.qcow2` (the libvirt image);
no `--disk` argument needed. The exact QEMU invocation:

```
qemu-system-x86_64 \
  -m 2G -smp 4 \
  -cpu max,kvm=on -enable-kvm \
  -drive file=/var/lib/libvirt/images/rabbitears-vm.qcow2,if=virtio,format=qcow2 \
  -device virtio-gpu-pci \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
  -device virtio-net-pci,netdev=net0 \
  -serial tcp:127.0.0.1:4321,server,nowait \
  -display none \
  -qmp unix:/tmp/rabbitears-qmp.sock,server,nowait
```

- **Ctrl+C** (or SIGTERM/SIGHUP, e.g. closing the terminal) shuts the VM down
  cleanly (SIGTERM → SIGKILL). QEMU also gets `PR_SET_PDEATHSIG`, so even a
  SIGKILLed vmctl takes its QEMU with it.
- **Preflight:** refuses to start if any QEMU already has the disk open or the
  libvirt domain is running (run `uv run task vm-stop` first), or if the disk
  isn't readable/writable (check your `kvm` group membership).

### Ports

| Host port | Purpose |
|---|---|
| `127.0.0.1:4321` | Serial console (TCP). |
| `127.0.0.1:2222` | SSH, host-forwarded to the VM's `:22`. |
| `/tmp/rabbitears-qmp.sock` | QMP monitor (Unix socket) for headless `screendump`. |

---

## Attaching to a running VM's serial console

```sh
uv run task vm-serial
# or: uv run vm/vmctl.py serial
```

---

## Taking a screenshot

```sh
uv run task vm-shot
# → screenshot saved: vm/screenshot.png  (1280x800)
uv run task vm-shot --out /tmp/frame.png   # extra args go to vmctl.py screenshot
```

Uses QMP `screendump` on `/tmp/rabbitears-qmp.sock`, which every `vm-run` opens.
It works headless and needs only the stdlib. It exits non-zero and leaves any
existing file untouched if the VM isn't running or the grab fails.

---

## Stopping the VM

```sh
uv run task vm-stop    # stops vmctl's QEMU and the libvirt domain, verifies both died
uv run task vm-status  # lists anything still holding the disk
```

Or Ctrl+C in the `vm-run` terminal to stop the QEMU process.

> **ACPI shutdown is ignored once the app is deployed.** The power-button hooks
> (`system/acpid/`, SCOPE §3) make both `KEY_POWER` and the ACPI power button a
> logged no-op, so `virsh shutdown` (which sends an ACPI power-off) no longer
> shuts the guest down. `uv run task vm-stop` therefore falls back to
> `virsh destroy` after `provision.sh stop`'s ~60 s graceful-shutdown timeout.
> That's expected.

---

## Mock USB drive

The VM's "media drive" is an **emulated USB mass-storage device**, not a 9p
share or host directory. A 9p share would skip the label lookup, the mdev
hotplug rule, the exFAT driver, and the pull-the-plug behaviour — exactly the
parts most likely to break on the real 3040. The box and the VM mount the
drive through the *same* mdev rule (no fstab entry, no 9p).

`vm/make-usb.sh` builds a partitioned exFAT image labelled `RABBITEARS` from
the transcoded content in the staging dir. It needs `uv run task setup-sudoers`
to have been run once (it self-elevates for `losetup`/`mount`).

### Stage content (SCOPE §5-compliant)

Stage transcoded media at `/var/lib/rabbitears-vm/media/channels/<NN-slug>`
(mirrors the USB layout). `vm-build` creates the user-owned staging dir:

```sh
uv run task vm-build         # no-op on an installed VM; ensures the staging dir
mkdir -p /var/lib/rabbitears-vm/media/channels/<NN-slug>
uv run task transcode "<SOURCE_DIR>" /var/lib/rabbitears-vm/media/channels/<NN-slug>
uv run task media-check /var/lib/rabbitears-vm/media/channels/<NN-slug>
```

### Build, boot, and hot-plug the stick

```sh
uv run task usb-build        # -> ./vm/make-usb.sh  (builds /var/lib/rabbitears-vm/usb.img)
uv run task vm-run-usb       # -> vmctl.py run --usb /var/lib/rabbitears-vm/usb.img
uv run task usb-unplug       # -> vmctl.py usb unplug   (yank the drive)
uv run task usb-plug         # -> vmctl.py usb plug     (re-insert it)
```

`usb-build` is idempotent: a second run with no content changes copies nothing
and does not reformat. Pass `--reformat` to rebuild a corrupt image, or
`--reset-state` to drop the on-drive `/.rabbitears/` (index + state).

---

## On-screen display

The channel flash and the info overlay are text over the live video (mpv
`show-text` + ASS override tags, SCOPE §4.5/§10). In the VM:

```sh
uv run task vm-key ok     # show the info overlay (channel, title, time remaining)
uv run task vm-key back   # dismiss it
uv run task vm-shot       # the overlay is centred; a flash is top-right right after a zap
```

The flash (`CH NN`, green, top-right) fires on every tune and clears itself
after ~2.5 s, so `vm-shot` right after a `vm-key up`/`down` catches it. The
overlay is centred and stays until `back`. Legibility over composite on the CRT
is a real-3040 check (SCOPE §7.3); the VM screenshot is the pixel evidence.

---

## Remote (emulated)

The VM has no physical remote; the emulated remote is a QEMU `usb-kbd`
(vendor `0x0627`, the Adomax dongle's ID), attached by default whenever the VM
boots. The app's real vendor scan (`evdevd.py`) finds it with no VM branch.
The guest check is `cat /sys/class/input/event*/device/id/vendor` — every
`0x0627` entry is the remote.

```sh
uv run task vm-key up              # press a button via QMP input-send-event
uv run task vm-key down --hold 2.0 # hold the button for 2 s (autofire)
uv run task remote-unplug          # -> vmctl.py remote unplug  (yank the dongle)
uv run task remote-plug            # -> vmctl.py remote plug    (re-insert it)
uv run task vm-powerbutton         # ACPI power button (tests the §3 no-op)
```

Button → qcode map (`vmctl.py key`):

| Button | qcode | evdev code (expected) |
|---|---|---|
| `up` / `down` | `up` / `down` | 103 / 108 |
| `ok` | `ret` | 28 |
| `back` | `ac_back` | 158 |
| `volup` / `voldown` | `volumeup` / `volumedown` | 115 / 114 |
| `mute` | `audiomute` | 113 |
| `power` | `power` | 116 |
| `left` | `left` | 105 (ignored key; negative test) |

The `usb-kbd` is a boot keyboard, so consumer-page keys (likely `back`/
`ac_back`, possibly the volume keys and `power`) may not arrive via
`input-send-event`. Check the app log (`/var/log/rabbitears.log`) to confirm
which rows arrive; anything that doesn't is covered by `rabbitears.ctl` over
SSH and is re-checked on the real remote at M3 hardware sign-off.

**Driving the app over SSH** (the sanctioned test action):

```sh
ssh -i vm/keys/rabbitears-vm -p 2222 root@127.0.0.1 \
    'PYTHONPATH=/opt python3 -m rabbitears.ctl status'      # read-only JSON
ssh -i vm/keys/rabbitears-vm -p 2222 root@127.0.0.1 \
    'PYTHONPATH=/opt python3 -m rabbitears.ctl channel_up' # -> ok
```

The control socket is `/run/rabbitears/control.sock` (`srw-------`, root only);
it accepts only a `Command` value (`channel_up`, `mute`, `power`, …) or
`status`. There is no "load this path", no shell, no network listener.

---

## Validation checklist (inside the VM)

Once the VM is up (`ssh -i vm/keys/rabbitears-vm -p 2222 root@127.0.0.1`):

1. **Service is running:**
   ```sh
   rc-service rabbitears status    # → started
   ```
2. **App log exists:**
   ```sh
   ls -l /var/log/rabbitears.log
   ```
3. **A 720p file plays (software decode):**
   ```sh
   mpv --vo=gpu /path/to/sample-720p.mp4
   ```
   Watch for a visibly playing picture. Note: this is **software decode** —
   the VM has no VAAPI. Hardware decode is validated only on the 3040 (M1).

---

## Related docs

- `docs/alpine-install.md` — the full box/VM install procedure.
- `SCOPE.md` §2 (hardware), §4.3 (playback), §5 (media rules) — the source of
  truth for what "works" means.
