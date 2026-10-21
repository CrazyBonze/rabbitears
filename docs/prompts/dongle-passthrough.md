# Task: pass the real Adomax dongle through to the VM

You are taking over a partially-finished feature. Read this file first, then
`AGENTS.md`, then the files listed under "Key files". The user wants to drive
the Rabbit Ears VM with the **physical Adomax 2.4GHz remote** (dongle
`0627:697d`) instead of `uv run task vm-key`. The dongle is plugged into the
host and the VM boots with it passed through — but the guest never sees it.
We have a complete diagnosis; the remaining problem is **one**: the host
device node is not accessible to the user, and the user has (so far) declined
a udev rule, believing there is another way. Find it.

## Goal

The guest sees `0627:697d` on its xHCI bus, the app's evdev daemon
(`rabbitears-remote` service) finds it by vendor scan, `grab()`s it, and
presses on the physical remote show up in `/var/log/rabbitears.log` as
`remote: <button> ...`. Bonus: this finally exercises the real **Back**
button (KEY_BACK), which the emulated `usb-kbd` cannot deliver (see
`docs/appliance-bench-report.md`, Finding #1).

## What is already done (do not redo)

- `vm/vmctl.py`: `run --remote-usb VID:PID` passes the dongle through with
  `-device usb-host,vendorid=0x..,productid=0x..,bus=xhci.0,id=remote0` and
  **replaces** the emulated `usb-kbd` (which also reports vendor 0x0627).
  `qemu_args()`, the `Vm` dataclass, and `cmd_run()` all thread `remote_usb`.
- `pyproject.toml`: task `vm-run-usb-vnc-remote`
  (= `run --usb /var/lib/rabbitears-vm/usb.img --display vnc --remote-usb 0627:697d`).
- `vm/vmctl.py`: `run --display vnc` (VNC on 127.0.0.1:5901) — the working
  live-view path (SPICE shows a multi-head view that hides the video; VNC
  mirrors the same surface `screendump` captures).
- `tests/test_vmctl.py` updated; full suite green (349 tests, 4 expected skips).
- Viewer: Remmina profile `~/.local/share/remmina/rabbitears-vm.remmina`
  (`[remmina]` group, `protocol=VNC`, `server=127.0.0.1:5901`,
  `scaling_mode=2` = fit-to-window).

## Current live state (at time of writing)

- VM **running**: QEMU pid via `pgrep -f qemu-system-x86_64`, booted with
  `vm-run-usb-vnc-remote`. VNC on 127.0.0.1:5901, stick attached, CH 01
  playing. QMP at `/tmp/rabbitears-qmp.sock`.
- The boot-time `usb-host` device (`id=remote0`) is **deferred**: QEMU 8.2's
  `usb-host` does not fail when it cannot open/claim the device — it
  registers a libusb hotplug and waits. `device_add` returns success;
  QEMU keeps running; the guest never sees the device. (A stray
  `id=remotetest` device added during diagnosis was removed.)
- Host: dongle at `Bus 003 Device 016`, node `/dev/bus/usb/003/016`,
  **`crw-rw-r-- root:root`**. Host `usbhid` is still bound to both
  interfaces (`/sys/bus/usb/devices/3-2:1.*/driver -> usbhid`).
  `fuser /dev/bus/usb/003/016` → no process holds it.

## The diagnosis (verified, with evidence)

1. `michael` (uid 1000, groups incl. `plugdev`, `sudo`) **cannot open**
   `/dev/bus/usb/003/016`: `os.open(..., O_RDWR)` → `EACCES`. Node is
   `root:root 0660`.
2. Therefore QEMU (runs as `michael`, via `sg kvm -c ...`) cannot claim the
   dongle, so the guest never enumerates it. Everything else in the chain
   works: the QEMU flag list is correct (verified in the process cmdline),
   the guest xHCI works (the emulated stick `46f4:0001` enumerates fine),
   and the app's daemon is running and waiting.
3. QEMU's deferred-open behaviour is why nothing errors: expect **no**
   failure signal from QEMU until the node becomes accessible.

## Constraints (from AGENTS.md — read it)

- **No ad-hoc root commands without the user's explicit OK.** No new
  sudoers rules, no setuid bits. The user is in the `sudo` group and will
  run a small number of explicit `sudo` commands if you ask.
- The user **declined a standing udev rule** for now ("I feel like there is
  another way"). Do not re-litigate; find the alternative, or present the
  trade-offs crisply and let them choose.
- Repo targets change only via the deploy pipeline; this task is host-side
  VM tooling only. Don't touch `src/rabbitears/` — the app already does the
  right thing (vendor scan + grab) and works on the real 3040.
- `vm/` scripts are host-side; bash is fine; Python via `uv run`.
- One QEMU may hold the disk; use `uv run task vm-stop` / the vmctl tasks.
  The `sg kvm -c "..."` stopgap applies in old shells (kvm group).
- Never commit/push unless asked.

## Candidate solutions (evaluate; pick or propose the best)

1. **One-shot permission fix (no persistent rule):**
   `sudo chmod 660 /dev/bus/usb/003/016 && sudo chgrp plugdev /dev/bus/usb/003/016`
   — or just `sudo chmod 666` for the test. Non-persistent (resets on
   replug/reboot), zero config files. Then trigger QEMU's deferred attach:
   the libusb hotplug fires on device (re)add, so replug the dongle, or
   `sudo udevadm trigger` may not be enough — a VM restart with the node
   already permissive is the deterministic path.
2. **Why is the node root:root in the first place?** Inspect
   `udevadm info -a /dev/bus/usb/003/016` (MODE/GROUP lines and the rules
   that set them). Distro defaults usually give USB nodes `plugdev` group
   access; something on this host may be overriding. If a *broken/foreign*
   rule is the cause, fixing/removing it is more in spirit than adding a new
   rule. (Read-only inspection needs no approval.)
3. **libvirt hostdev:** the repo already has a libvirt domain
   (`rabbitears-vm`, built by `vm/provision.sh`). libvirtd runs as root and
   claims host devices itself — `<hostdev mode='subsystem' type='usb'
   source='vendor id=0x0627' product id=0x697d'/>` in the domain XML needs
   no udev rule and no permission fiddling. Cost: it bends the
   "provision.sh builds / vmctl.py runs" decision (2026-10-18) and the
   domain would also need the stick image + VNC graphics to match vmctl's
   setup. Weigh this against the repo's one-run-path design before
   proposing it; if you go this way, update SCOPE.md §10 and AGENTS.md.
4. **Fallback (no passthrough):** host-keyboard → `vmctl.py key` mapper
   script (e.g. an `xdotool`/evdev watcher that maps arrows/enter/volume to
   buttons while the VNC window is focused). Less authentic: no real Back
   button, no RF, no physical hold-to-repeat. Only if 1–3 are rejected.

## Verification (definition of done)

1. `ssh -i vm/keys/rabbitears-vm -p 2222 root@127.0.0.1 'lsusb'` shows
   `0627:697d` (SSH is read-only inspection — allowed).
2. A new `/dev/input/eventN` appears in the guest; the daemon log
   (`/var/log/rabbitears-remote.log`) or app log shows the device grabbed.
3. Pressing **channel up** on the physical remote logs
   `remote: channel_up -> CH 0N ...` in `/var/log/rabbitears.log` and the
   VNC window shows the green `CH NN` flash.
4. **Back** dismisses the info overlay (the button that never worked via
   the emulated remote).
5. `uv run task test` still green; `uv run task vm-verify` → `verify: OK`.
6. Update `docs/vm-handoff.md` with the real-remote procedure (including
   how to get the dongle back from the VM: `uv run task vm-stop` releases
   it).

## Key files

- `vm/vmctl.py` — `qemu_args()` (usb-host emission), `Vm` dataclass,
  `cmd_run()`, `KEY_QCODES` (the emulated-remote key map, for reference).
- `pyproject.toml` — `[tool.taskipy.tasks]` (vm-run-usb-vnc-remote, etc.).
- `src/rabbitears/evdevd.py` — the daemon's vendor scan + grab (read-only
  reference; shows exactly what the guest expects).
- `src/rabbitears/remote.py` — `BUTTONS` evdev-code map (KEY_BACK=158 etc.).
- `docs/remote.md` — the verified remote dossier (USB ID, button map,
  composite device: keyboard + system-control interfaces).
- `docs/vm-handoff.md` — how the user drives the VM today.
- `docs/appliance-bench-report.md` — bench results + Finding #1 (back key).
- `AGENTS.md` — the rules you must follow.

## Report back with

- Which solution you chose and why (one paragraph).
- The exact commands the user had to run (if any).
- Verification evidence: guest `lsusb` line, one physical-remote log line
  per of {channel up, back, power}, and a note that the host got the dongle
  back after `vm-stop`.
- Any repo files changed (and whether SCOPE.md §10 needed a decision entry).
