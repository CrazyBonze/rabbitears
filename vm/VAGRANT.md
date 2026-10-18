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

Under the hood:

| Step | Script |
|---|---|
| `vm-build` | `vm/provision.sh` → `vm/install.py` (libvirt domain, unattended `setup-alpine`) |
| `vm-run` | `vm/vmctl.py run` (raw QEMU, virtio-gpu, user-net, QMP) |
| `deploy-vm` | `alpine/deploy.sh --vm --reboot` (tar over ssh → `setup.sh` → `verify.sh`) |
| `vm-shot` | `vm/vmctl.py screenshot` (QMP `screendump`) |

---

## Prerequisites (host)

- Debian/Ubuntu with `/dev/kvm` (you are in the `kvm`/`libvirt` group).
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

- **Ctrl+C** shuts the VM down cleanly (SIGTERM → SIGKILL).
- **Preflight:** refuses to start if the libvirt domain is running
  (run `uv run task vm-stop` first) or if the disk isn't readable/writable
  (check your `kvm` group membership).

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
# → saves vm/screenshot.png
```

---

## Stopping the VM

```sh
uv run task vm-stop    # shuts the libvirt domain down (idempotent)
```

Or Ctrl+C in the `vm-run` terminal to stop the QEMU process.

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
