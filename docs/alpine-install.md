# Alpine Install

Two targets share the *software* procedure but differ in the *base* install:

- **VM (dev)** — a standard Alpine install in the KVM VM. Zero-touch: one command.
- **Box A/B (prod)** — a *diskless* Alpine install on the Wyse 3040's eMMC, state
  persisted via `lbu`. This is the real appliance (milestone M1).

Once the base OS is up, both get the identical Rabbit Ears stack from
`alpine/setup.sh`. Only the base install differs (standard vs diskless).

---

## 1. VM — zero-touch install (dev)

**One-time per clone:** install passwordless sudo for the VM scripts:

```sh
./vm/setup-sudoers.sh    # asks for your sudo password once
```

One command provisions the KVM VM, drives an unattended `setup-alpine` over the
serial console, and leaves the disk at rest (domain shut off):

```sh
./vm/provision.sh        # self-elevates under the NOPASSWD rule
```

What it does (idempotent, safe to re-run):

1. Installs host packages (qemu, libvirt, `python3-paramiko`, `python3-serial`).
2. Downloads + SHA256-verifies the pinned Alpine ISO (`vm/isos/`, git-ignored).
3. Generates the VM SSH keypair (`vm/keys/`, git-ignored) and syncs the
   answerfile's `ROOTSSHKEY` to it — so a fresh clone always matches.
4. Creates the 8 GB disk and defines the domain (direct kernel boot + serial pty).
5. Logs into the live system over serial, writes `alpine/answerfile`, and runs
   `setup-alpine -f` unattended: empty root password, auto-confirmed disk erase
   (`ERASE_DISKS`), and the SSH key baked in for passwordless root.
6. Switches the domain to boot from disk and verifies SSH with the key.

> **Speed:** the VM's e1000+NAT path to the public CDN is slow (~50 KB/s), while
> the host pulls the CDN at several MB/s. Start the local caching proxy first so
> the install (and every later `apk add`) is served over the local network:
>
> ```sh
> sudo ./vm/provision.sh proxy
> ```
>
> The answerfile points the VM's apk repo at the proxy (`192.168.122.1:8081`).
> With the proxy, `apk update` is ~1 s and package installs are sub-second.

### Subcommands

```sh
./vm/provision.sh                    # create + zero-touch install (default)
./vm/provision.sh status             # VM + ISO + disk status
./vm/provision.sh proxy              # start the local apk caching proxy
./vm/provision.sh stop               # shut the domain down (idempotent)
./vm/provision.sh console            # dump the serial console (debugging)
./vm/provision.sh console-cmd "<c>"  # run a command on the console, read result
./vm/provision.sh diskboot           # boot the installed system (from disk)
./vm/provision.sh liveboot           # boot the live ISO (to inspect the disk)
./vm/provision.sh eject              # detach the ISO (boot from disk)
./vm/provision.sh destroy            # destroy the VM + its disk
```

### SSH in

```sh
sg libvirt -c 'virsh domifaddr rabbitears-vm'    # get the IP
ssh -i vm/keys/rabbitears-vm root@<vm-ip>        # key auth, no password
```

### Rebuild from scratch

```sh
./vm/provision.sh destroy && ./vm/provision.sh
# or: uv run task vm-rebuild
```

---

## 2. Box — diskless install (prod, M1)

Diskless = root runs in RAM, populated at boot from an `apkovl` (overlay) on a
small eMMC partition. This is the fiddly, do-once part; the steps below are the
shape of it — validate each on Box A and record the exact commands that worked.

1. **Boot Box A from the Alpine ISO** (USB). You get a RAM root as `root`.
2. **Partition the eMMC** — one small ext4 partition for boot + overlay:
   ```sh
   # e.g. on /dev/mmcblk0 (confirm the device first!)
   parted /dev/mmcblk0 mkpart primary ext4 1MiB 1GiB
   mkfs.ext4 /dev/mmcblk0p1
   mount /dev/mmcblk0p1 /mnt
   ```
3. **Install the kernel + initramfs** to `/mnt/boot` (from the ISO's `/lib/modules`
   and a generated initramfs that knows how to load the apkovl).
4. **Set up the apkovl** with `lbu`:
   ```sh
   # point lbu at the overlay location on /mnt, then:
   lbu include /opt/rabbitears
   lbu commit
   ```
5. **Install a bootloader** (extlinux/syslinux) on the partition to boot the
   kernel with the apkovl, then reboot into diskless mode.
6. **Verify:** `lbu status` shows the overlay; the box boots to a RAM root with
   `/etc` and packages from the overlay.

> Reference: the official Alpine "diskless" guide. The exact partition sizes,
> initramfs flags, and lbu config are version-sensitive — capture the working
> values here once Box A is up so Box B is a copy, not a re-derivation.

### The LBU checkpoint

Once the base OS + Rabbit Ears stack are in place, cut a checkpoint so a rebuild
is a restore, not a re-derivation:

```sh
lbu package          # → /root/lbu-<timestamp>.tar.gz (config + package list)
```

Store that tarball in the repo. A fresh box is then: base install + `lbu restore`.

### What persists where (box)

| Data | Location | How |
|---|---|---|
| OS config, packages, services | eMMC `apkovl` | `lbu commit` |
| App code (`/opt/rabbitears`) | eMMC `apkovl` | `lbu include /opt/rabbitears` + `lbu commit` |
| Fonts, `mpv.conf` | eMMC `apkovl` | under `/etc` or `lbu include` |
| Media library | USB drive | plain files |
| Runtime state (last channel, volume) | USB `/.rabbitears/state.json` | written by the app |
| Schedule index (durations) | USB `/.rabbitears/index.json` | generated by the indexer |

**Rule:** the app never writes to `/` at runtime. State goes to the USB drive;
if the drive is absent, state is kept in memory only.

---

## 3. First-boot checklist (both targets)

- [ ] `apk system update` is clean
- [ ] `rc-service sshd status` → running (dev access)
- [ ] `chrony` synced (correct time — the schedule depends on it)
- [ ] `alpine/deploy.sh … --reboot` ran clean (packages, `system/` config, app code, `rc-update`, `lbu include`, manifest)
- [ ] **Reboot** (not just `rc-service start`) — the service must come up through the real OpenRC boot sequence
- [ ] `rc-service rabbitears status` → `started`
- [ ] `/run/rabbitears/` exists (created by the init script's `start_pre()`)
- [ ] `/var/log/rabbitears.log` exists
- [ ] `supervise-daemon` is running with the supervised process (check respawn: kill the child, it comes back)

### The `rabbitears` service must boot (M1 gate)

The whole M1 "base box" hinges on the `rabbitears` OpenRC service coming up at
boot under `supervise-daemon` (respawn on crash, SCOPE §4.7). Verify it with a
**real reboot**, because `rc-service start` in a live session does not exercise
the boot path:

```sh
# 1. deploy the stack (tar over ssh → setup.sh → verify.sh)
uv run task deploy-vm             # VM
# or: alpine/deploy.sh --host H --commit --reboot   # box
# 2. after it's back up:
rc-service rabbitears status      # → * status: started
ls -ld /run/rabbitears            # → exists (start_pre ran)
ls -l /var/log/rabbitears.log     # → exists
pgrep -af supervise-daemon        # → the supervisor + supervised process
```

> **Shebang gotcha (cost us a debugging session):** the init script's shebang
> **must** be `#!/sbin/openrc-run`, not `#!/bin/sh`. `rc-service` hands off to
> the shebang interpreter; `openrc-run` is what sources the OpenRC framework
> (`openrc-run.sh`) and wires up the `supervisor=supervise-daemon` hook,
> `start_pre()`, and the `start()`/`stop()` dispatch. With `#!/bin/sh` the
> script runs as a plain shell — top-level code executes but `start()` is never
> called and the service never starts (neither via `rc-service` nor at boot).
> Every stock service (`sshd`, `chronyd`, …) uses `#!/sbin/openrc-run`; match it.
