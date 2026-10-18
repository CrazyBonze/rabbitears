# Rabbit Ears — virtio-gpu validation VM

A throwaway QEMU VM whose single job is to **prove the video path works
before we commit to it on the real Wyse 3040.** The 3040 has no discrete GPU;
it renders through the integrated Cherry Trail (Gen8) i965 driver over VAAPI.
We want to be sure that an `mpv` output driven by a **virtio-gpu** display
device (the closest thing to the 3040's path we can stand up on a dev box)
actually decodes and shows a 720p H.264 file — *before* we wire it into the
production box and discover it doesn't.

This is a **validation** VM, not the install VM. The unattended Alpine install
(live ISO, direct kernel boot, libvirt domain) lives in `vm/provision.sh` +
`vm/install.py` — see `docs/alpine-install.md`. This VM boots an *already
installed* disk image with a fixed, minimal device set.

---

## What it validates

- `virtio-gpu-pci` as the display device (the dev-box stand-in for the 3040's
  integrated GPU).
- Hardware decode over VAAPI (`i965` driver) with `mpv --vo=gpu`.
- A 720p H.264 / AAC file plays end-to-end (the media rule from SCOPE §5).

If this VM can't show a 720p file smoothly, the 3040 likely can't either —
fix the video path here, cheaply, before touching a box.

---

## Prerequisites (host)

- `qemu-system-x86_64` with **KVM** enabled (`/dev/kvm` accessible; you are in
  the `kvm`/`libvirt` group).
- An installed Rabbit Ears Alpine **disk image** (qcow2). Build one with the
  normal install flow (`vm/provision.sh` + `vm/install.py`, or any Alpine
  install) and point `--disk` at the resulting image.
- Python 3.14 (stdlib only — `vmctl.py` needs no extra packages).

Confirm KVM is usable before anything else:

```sh
egrep -c '(vmx|svm)' /proc/cpuinfo   # > 0
ls -l /dev/kvm                        # exists and you can read/write it
```

---

## Creating the disk image

`vmctl.py` boots whatever qcow2 you hand it. If you don't have one yet:

```sh
# 8 GB image, matching the prod box's disk size
qemu-img create -f qcow2 rabbitears-vm.qcow2 8G
```

Then install Alpine into it (see `docs/alpine-install.md` for the full
procedure, and `vm/provision.sh` for the automated libvirt-based install). The
VM only needs a working Alpine with `mpv`, `ffmpeg`, and the i965 VAAPI driver
installed (`apk add mpv ffmpeg mesa-vaapi-drivers` — confirm the driver with
`vainfo` inside the VM).

---

## Running the VM

```sh
python3 vm/vmctl.py run --disk /path/to/rabbitears-vm.qcow2
```

This boots the VM and **streams the serial console to stdout**. The exact
qemu invocation (fixed for reproducibility) is:

```
qemu-system-x86_64 \
  -m 2G -smp 2 \
  -cpu max,kvm=on -enable-kvm \
  -drive file=DISK,if=virtio,format=qcow2 \
  -device virtio-gpu-pci \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
  -device virtio-net-pci,netdev=net0 \
  -serial tcp:127.0.0.1:4321,server,nowait \
  -display none \
  -qmp unix:/tmp/rabbitears-qmp.sock,server,nowait
```

- **Ctrl+C** shuts the VM down cleanly: `SIGTERM` first, escalating to
  `SIGKILL` if it doesn't exit within a few seconds, then the process exits 0.
  You never have to hunt for a stray qemu.
- The QMP socket at `/tmp/rabbitears-qmp.sock` is opened automatically (see
  [Taking a screenshot](#taking-a-screenshot-development-loop)); disable it
  with `--monitor ""`.

### Ports

| Host port | Purpose |
|---|---|
| `127.0.0.1:4321` | Serial console (TCP). `run` streams it; `serial` attaches to it. |
| `127.0.0.1:2222` | SSH, host-forwarded to the VM's `:22`. |
| `/tmp/rabbitears-qmp.sock` | QMP monitor (Unix socket) for headless `screendump`. |

---

## Attaching to a running VM's serial console

If the VM is already up (e.g. you started it in one terminal), attach from a
second terminal:

```sh
python3 vm/vmctl.py serial            # defaults to 127.0.0.1:4321
python3 vm/vmctl.py serial --host 127.0.0.1 --port 4321
```

It forwards serial bytes to stdout until the connection closes (the VM went
down). Log in over the console as `root`.

---

## Taking a screenshot (development loop)

Every `vmctl.py run` automatically opens a QMP monitor socket at
`/tmp/rabbitears-qmp.sock`. Once the VM has booted, grab the current
framebuffer from a second terminal:

```sh
python3 vm/vmctl.py screenshot
# -> saves vm/screenshot.png and prints: screenshot saved: vm/screenshot.png (1280x800)
```

Override the defaults when needed:

```sh
python3 vm/vmctl.py screenshot --monitor /tmp/other.sock --out /tmp/frame.png
```

The grab is synchronous and reliable (QMP `screendump` via the QEMU monitor
socket). It works with `--display none` (headless) and `--display spice`.

---

## Live display with SPICE

Boot with `--display spice` to open a SPICE server on port 5910 and watch the
VM output live:

```sh
python3 vm/vmctl.py run --disk /path/to/rabbitears-vm.qcow2 --display spice

# From another terminal (or after connecting over SSH):
spicy --host=127.0.0.1 --port=5910
# or
virt-viewer spice://127.0.0.1:5910
```

The SPICE server is on localhost only and requires no password
(`disable-ticketing=on`). Combine with `--monitor /tmp/qmp.sock` to get both
a live view and the QMP screendump capability.

---

## Validation checklist (inside the VM)

Once the VM is up (serial console or `ssh -p 2222 root@127.0.0.1`):

1. **VAAPI driver present:**
   ```sh
   vainfo | grep -i i965     # expect the i965 driver listed
   ```
2. **A 720p file plays with hardware decode:**
   ```sh
   mpv --vo=gpu --hwdec=vaapi /path/to/sample-720p.mp4
   ```
   Watch for `hwdec: ... vaapi` in mpv's startup log and a visibly playing
   picture (attach a display, or confirm no decode errors + steady playback).
3. **No fallback to software decode.** If mpv logs a hwdec failure and falls
   back to `sw`, the video path is *not* validated — that's the thing we're
   trying to catch.

If all three pass, the virtio-gpu + VAAPI path is good to carry forward to the
3040 (M1).

---

## Cleanup

Ctrl+C in the `run` terminal (or kill the qemu process). The disk image is a
plain file — delete it when you're done:

```sh
rm rabbitears-vm.qcow2
```

---

## Related docs

- `docs/alpine-install.md` — the full box/VM install procedure (diskless prod
  install + the libvirt install VM this one complements).
- `docs/hardware.md` — 3040 notes: BIOS settings, adapters, the video chain
  (why the display path is the risky part we're validating here).
- `docs/remote.md` — remote button map and USB IDs (for the later M3 milestone
  once the video path is proven).
- `SCOPE.md` §2 (hardware), §4.3 (playback), §5 (media rules) — the source of
  truth for what "works" means.
