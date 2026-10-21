# Driving the VM like the real appliance

The VM is a stand-in for the Wyse 3040: same app, same OpenRC services, same
mdev USB mount, an emulated USB stick (`/var/lib/rabbitears-vm/usb.img`) and
an emulated remote (QEMU `usb-kbd`, vendor `0x0627` — the Adomax dongle's ID).
Everything below is a taskipy task run from the repo root. No SSH needed for
normal use.

## State right now

- VM **running with a live VNC display** (127.0.0.1:5901), stick **plugged in**,
  and the **real Adomax dongle passed through** (`--remote-usb`), tuned to
  CH 01 · Holiday Classics.
- Media: 3 channels / 14 files (see `docs/appliance-bench-report.md`).

## Seeing the screen

**Live window (preferred):** boot with the VNC display and connect the viewer:

```
uv run task vm-run-usb-vnc        # boots with the stick + VNC on 127.0.0.1:5901
uv run task vm-watch              # opens Remmina on the live screen (returns immediately)
```

`vm-watch` = Remmina via the repo's committed `vm/remmina/rabbitears-vm.remmina`
profile; any VNC client works too (`vinagre vnc://127.0.0.1:5901`).

VNC mirrors QEMU's primary display surface — the same one `vm-shot`
captures — so it reliably shows mpv's video. (The older `vm-run-usb-spice`
still works, but with the VM's two GPUs a SPICE client can get a multi-head
view that shows the tty login prompt instead of the video.)

**One-shot screenshot:** `uv run task vm-shot` → `vm/screenshot.png`
(works with any boot, including headless).

The serial console (`uv run task vm-serial`) is text only.

## The remote

| You want to… | Run |
|---|---|
| Channel up / down | `uv run task vm-key up` / `vm-key down` |
| Info overlay (toggle) | `uv run task vm-key ok` |
| Volume up / down | `uv run task vm-key volup` / `voldown` |
| Mute (toggle) | `uv run task vm-key mute` |
| Standby / wake | `uv run task vm-key power` |
| Hold a key (auto-repeat) | `uv run task vm-key up --hold 3` |
| **Back** | **doesn't work via `vm-key`** (QEMU `usb-kbd` drops AC Back — see bench report, Finding #1). Use `ok` to dismiss the overlay. The **real dongle** below delivers Back. |

Expected behaviour: green `CH NN` flash top-right for ~2.5 s on every arrival
(initial tune, zap, wake); overlay shows channel/name/title/remaining;
standby is a pure black screen; volume is log-only (no sound card in the VM).

## The real remote (physical Adomax dongle)

Drive the VM with the actual 2.4GHz remote instead of `vm-key`. The dongle
(`0627:697d`) is passed straight through (`--remote-usb`), so the app's real
vendor scan + `grab()` and the physical RF path run — including **Back**
(`KEY_BACK`), which the emulated `usb-kbd` can't deliver (bench report Finding
#1).

**Prerequisite (one-time):** the host udev rule that puts the dongle node in
the `plugdev` group. It's installed by `vm/provision.sh` (already done); a
`uv run task vm-build` re-run also (re)installs it. Without it the node is
`root:root 0664` and QEMU silently defers the `usb-host` device — the guest
never sees the remote.

```
# 1. plug the dongle into the host
uv run task vm-run-usb-vnc-remote   # stick + VNC (127.0.0.1:5901) + dongle
# 2. point the physical remote at it and press buttons
```

Channel up/down, OK, Back, volume, mute and power all arrive through the real
evdev path and show up as `remote: <button> …` in the app log.

**While the VM is up it owns the dongle** — the host can't use it (that's the
point; the host keyboard is a separate device). `uv run task vm-stop` releases
the dongle back to the host.

## The appliance itself

| You want to… | Run |
|---|---|
| Screenshot the screen | `uv run task vm-shot` → `vm/screenshot.png` |
| Pull the USB plug | `uv run task usb-unplug` → NO SIGNAL test pattern |
| Re-insert the plug | `uv run task usb-plug` → re-index + retune |
| Press the ACPI power button | `uv run task vm-powerbutton` → nothing happens (by design) |
| Serial console | `uv run task vm-serial` |
| App log (read-only) | `ssh -i vm/keys/rabbitears-vm -p 2222 root@127.0.0.1 'tail -f /var/log/rabbitears.log'` |
| Stop the VM | `uv run task vm-stop` |
| Reboot the VM (stick stays in) | `uv run task vm-stop` then start it again (below) |

## Starting the VM (when it's off)

```
uv run task vm-run-usb          # headless; screenshots only
uv run task vm-run-usb-vnc      # + live VNC display (vinagre vnc://127.0.0.1:5901)
```

Caveat: this needs the `kvm` group, which only takes effect in **new**
terminals. In an old shell use the stopgap:

```
sg kvm -c "uv run task vm-run-usb"
```

(it runs in the foreground; `Ctrl-C` stops the VM. For background use
`nohup sg kvm -c "uv run task vm-run-usb" > /tmp/serial.log 2>&1 &`.)

All the *drive* commands (`vm-key`, `vm-shot`, `usb-*`, `vm-powerbutton`)
talk to the QMP socket and work from any terminal while the VM is up — no
group or sudo needed.

## Changing the media

1. Cut/transcode into the staging dir: `/var/lib/rabbitears-vm/media/channels/`
   (the reusable bench sources live in `/home/michael/Videos/rabbitears-bench/`).
2. `uv run task usb-build` (add `--reset-state` to wipe on-drive state/index).
3. `uv run task usb-unplug && uv run task usb-plug` — or reboot the VM.

## What the VM does *not* validate

VAAPI/hardware decode (virtio-gpu is software), audio output, RF remote
pairing, and the physical power button. Those are 3040-only checks (SCOPE §5,
`docs/hardware.md`).
