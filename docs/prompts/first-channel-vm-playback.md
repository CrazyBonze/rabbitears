# Agent prompt: first channel end-to-end. Mock USB stick → VM playback → screenshot

**Goal: give the VM an emulated USB stick that behaves like the real drive: partitioned, exFAT, labelled `RABBITEARS`, holding SCOPE §5-compliant media, hot-pluggable. Mount it with the same mdev hotplug rule the Wyse 3040 will use. Implement the app entry point. Prove from a freshly built VM, through a real reboot, that the box plays the correct file at the correct schedule position. Then prove it survives the drive being pulled and re-inserted.**

**Prerequisite:** `docs/prompts/vm-foundation-fixes.md` must be complete. This prompt relies on what it delivered:
- `provision.sh` builds the VM and leaves the disk at rest; `vmctl.py run` is the only runtime (4 vCPU, default disk, QMP socket)
- `alpine/deploy.sh --vm [--reboot]`, ending with `alpine/verify.sh` (drift check against `/etc/rabbitears/manifest`)
- `setup.sh`:
  - installs the app atomically and checks lbu coverage
  - installs files through `install_file` (writes only on change, counts `CHANGED`)
  - writes the manifest
- taskipy tasks in `pyproject.toml`: `test`, `vm-build`, `vm-rebuild`, `vm-run`, `vm-stop`, `vm-shot`, `deploy-vm`, `vm-verify`, `transcode`, `media-check`, …
- the init script logs to `/var/log/rabbitears.log` with unlimited respawn
- `Player.get_property` / `is_idle` / `is_running` / `show_image`, plus PDEATHSIG
- `config.testpattern_path`
- `tools/transcode.sh` (with `--check`)

If any of these is missing, stop and report. Don't re-implement them here.

Work from the repository root (paths below are repo-relative). Read these in full before making any changes: `AGENTS.md`, `SCOPE.md`, `vm/setup-sudoers.sh`, `docs/alpine-install.md`, `vm/VAGRANT.md`, `pyproject.toml` (`[tool.taskipy]`), `vm/provision.sh`, `vm/vmctl.py`, `alpine/deploy.sh`, `alpine/verify.sh`, `alpine/setup.sh`, `system/openrc/rabbitears`, `tools/transcode.sh`, and everything in `src/rabbitears/`.

---

## The paradigm (unchanged; every change must respect it)

1. **The repo is the only source of truth.** A target is base install + `uv run task deploy-vm` (or `deploy-box`). No hand edits on a target. SSH is for read-only inspection and deliberate test actions (killing a process, querying mpv), never for changing config. `verify.sh` must stay clean.
2. **Everything is scripted and idempotent.** Re-runs converge. They don't duplicate config.
3. **Only the base install differs between VM and box.** `setup.sh` has **no** VM-vs-box detection. The VM and the box mount media through the *same* mechanism.
4. **The VM mimics the appliance.** That is why the "USB drive" is an emulated USB mass-storage device, not a 9p share or host directory. A 9p share would skip the label lookup, the mdev hotplug rule, the exFAT driver and the pull-the-plug behaviour. Those are the parts most likely to break on real hardware. (The VM's 8 GB limit applies only to its system disk, not to a second device.)
5. **Validation is a real reboot**, never `rc-service restart`.
6. **Every file the box needs at boot is under `/etc` or an `lbu include` path.** `setup.sh`'s lbu self-check must stay clean.

### Host-side conventions (same as the foundation prompt)

- **Never prefix `vm/provision.sh` or `vm/make-usb.sh` with `sudo`.** Both self-elevate. `vm/setup-sudoers.sh` (run once per clone by the user) makes that passwordless. If either prompts for a password, stop and ask the user to run `./vm/setup-sudoers.sh`.
- **Host Python runs through `uv`**, matching the VM/appliance's Python 3.14. Bare `python3` appears in this prompt **only for commands run inside the VM/box**, where Alpine's system `/usr/bin/python3` runs the app and there is no uv.
- **Use the taskipy tasks** (`uv run task --list`) for every common action.
  - Each new host-side entry point gets a task with a `help` string.
  - Tasks that change a target go through `deploy.sh` → `setup.sh`; no task runs ad-hoc commands on a target.
  - taskipy appends extra args to the main `cmd` only, not to `pre_`/`post_` hooks.
- For any other command that needs root, stop and ask the user to run it. Don't edit `/etc/sudoers.d/` yourself.
- **No personal paths.** The media source is whatever directory the user names. Never hardcode it, a home directory, or a username in any script, test or doc. The commands and examples below use `<SOURCE_DIR>` and `<NN-slug>` as placeholders.

---

## Part 1: The mock USB stick

### 1a. Content (SCOPE §5-compliant, staged at a stable host path)

The content stays the user's choice, expressed as plain commands, not baked into a script. Stage it at `/var/lib/rabbitears-vm/media/`, which mirrors the USB layout (SCOPE §6). Not `/tmp`: that's wiped on reboot, and the transcode is slow.

`/var/lib/rabbitears-vm` is root-owned, so the user can't create the staging dir without sudo. Have `vm/provision.sh` do it:
- `create` ensures `${VM_BASE}/media/channels` exists, owned by `$REAL_USER`. Do this idempotently, **before** the "already installed" early-skip, so a no-op re-run still creates it.
- `destroy` must **keep** `${VM_BASE}/media` and `${VM_BASE}/usb.img`. They're content, not VM state, and the transcode is expensive.

**Before starting, ask the user** (one question batch) for:
- the source media directory (`<SOURCE_DIR>`)
- the channel folder name (`<NN-slug>`, SCOPE §4.1 format, e.g. `01-cartoons`)

Don't search their filesystem for media. Document these commands in `vm/VAGRANT.md` under a new **"Mock USB drive"** section, using the placeholders. Then run them with the user's values:

```sh
uv run task vm-build         # no-op on an installed VM; ensures the user-owned staging dir
mkdir -p /var/lib/rabbitears-vm/media/channels/<NN-slug>
uv run task transcode "<SOURCE_DIR>" /var/lib/rabbitears-vm/media/channels/<NN-slug>
uv run task media-check /var/lib/rabbitears-vm/media/channels/<NN-slug>
```

Transcode every source file, keeping original basenames (they become on-screen titles), with no hand-filtering. Count the source files first and report that number (`N` below). Source media is often VP9 or 1080p; transcoding is what makes it playable on a 3040.

The transcode may be long: run it in the background and continue with Parts 2–5 meanwhile. It's resumable, so re-run it if interrupted. If any source file fails to transcode, don't hide it: list it in your report. A channel with fewer than `N` files is acceptable if the failures are explained. Never modify the source directory.

### 1b. `vm/make-usb.sh`: build the stick image (bash, host-side, self-elevating like `provision.sh`)

```
./vm/make-usb.sh [--src DIR] [--out IMG] [--size SIZE] [--reformat] [--reset-state]
```

The script needs root for `losetup` and `mount`. Use the same pattern as `provision.sh`: `if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi`, plus `REAL_USER="${SUDO_USER:-root}"`. It's never invoked with a `sudo` prefix.

`vm/setup-sudoers.sh` already lists `vm/make-usb.sh` in `SCRIPTS`, so its NOPASSWD rule exists as soon as the user has run that script; the rule takes effect once the file is created. Check with `sudo -n -l`. If `make-usb.sh` isn't listed, ask the user to run `./vm/setup-sudoers.sh` and wait for confirmation. **Don't write the rule yourself.**

Defaults: `--src /var/lib/rabbitears-vm/media`, `--out /var/lib/rabbitears-vm/usb.img`, `--size 16G`.

1. Refuse to run if the image is open by a running QEMU (check with `fuser` or by matching the path in running QEMU command lines).
2. **Create once.** If the image is missing:
   - make a sparse file (`truncate`)
   - write an **MBR partition table with one partition** spanning the disk (`sfdisk`, type `7`)
   - attach with `losetup -P`
   - run `mkfs.exfat -L RABBITEARS` on partition 1

   Real sticks ship partitioned, so the guest must see `sda1`, not a whole-disk filesystem.
3. **Never silently reformat.** If the image exists, check that partition 1 is exFAT labelled `RABBITEARS`. If not, error out unless `--reformat` is given.
4. **Sync content.** Mount partition 1 and run `rsync -rt --delete --modify-window=2 "$SRC/channels/" "$MNT/channels/"` (exFAT has no owners/permissions). Preserve the on-drive `/.rabbitears/` (index + state written by the box) unless `--reset-state` is given.
5. Always clean up (sync, umount, `losetup -d`) via a `trap`, even on failure.
6. `chown root:kvm` and `chmod 0660` the image, matching the system disk, so the user can open it through `vmctl.py`.
7. Print a summary: label, filesystem, channels with file counts, used / total.

A second run with no content changes must copy nothing and must not reformat.

---

## Part 2: `vm/vmctl.py`: attach the stick, and pull and re-insert it

### 2a. `--usb IMG`

Add `usb_image: Path | str | None = None` to `qemu_args()` and the `Vm` dataclass, and `--usb IMG` to `run`. When set, append:

```
-device qemu-xhci,id=xhci
-blockdev driver=file,filename=<IMG>,node-name=usbfile
-blockdev driver=raw,file=usbfile,node-name=usbstick
-device usb-storage,bus=xhci.0,drive=usbstick,id=usbstick0,removable=on
```

Use `-blockdev`, not `-drive`: a backend created with `-drive` is auto-deleted when its device is unplugged, which would make re-plugging impossible. Default is no stick, so the VM boots to NO SIGNAL exactly like a box with no drive.

### 2b. `vmctl.py usb {unplug,plug} [--monitor SOCK]`

The VM equivalent of yanking and re-inserting the drive.
- `unplug`: QMP `device_del` with `id=usbstick0`, then wait (≤10s) for the `DEVICE_DELETED` event.
- `plug`: QMP `device_add` with `driver=usb-storage, bus=xhci.0, drive=usbstick, id=usbstick0, removable=on`.
- Clear error messages if the VM wasn't started with `--usb`, or if it's already in the requested state.

**Fix `_qmp_exchange` first.** QMP sends async `{"event": ...}` messages on the same socket, and the current code treats the next line as the reply, so an event (e.g. `DEVICE_DELETED`) is misparsed. Skip event messages while waiting for `return`/`error`, and keep the events available to the caller that needs to wait for `DEVICE_DELETED`.

### 2c. Tests (`tests/test_vmctl.py`)

- `test_qemu_args_usb_image`: all four argument pairs, the correct path, `-blockdev` (not `-drive`) for the stick.
- `test_qemu_args_no_usb_by_default`: no `qemu-xhci` or `usb-storage`.
- `test_qmp_exchange_skips_interleaved_event`.
- `test_usb_unplug_sends_device_del_and_waits_for_event`, `test_usb_plug_sends_device_add`: fake QMP server over a real `AF_UNIX` socket, as in the existing screendump tests.

---

## Part 3: `system/mdev/rabbitears-usb`, the hotplug mount used by both VM and box

The box has no fstab entry for the drive. mdev mounts it when it appears and lazily unmounts it when it disappears. The VM uses this identical rule, because its stick is a real USB mass-storage device.

### 3a. The handler script

Installed by `setup.sh` to **`/etc/rabbitears/mdev-usb`** (0755). It lives under `/etc` so lbu persists it; `/lib/mdev/` would vanish on the diskless box. POSIX sh, busybox only, fast, and it never blocks mdev.

- Inputs: `$MDEV` (e.g. `sda1`, or `sda` for an unpartitioned stick) and `$ACTION`. Find out what busybox mdev sets `ACTION` to during coldplug (`mdev -s`). Treat an empty `ACTION` as `add`, and record the finding in a comment.
- **add:**
  1. If `/media/rabbitears` is already a mountpoint, exit 0.
  2. Read `LABEL` and `TYPE` from `blkid /dev/$MDEV`. If the label isn't `RABBITEARS`, exit 0. Any other USB device is ignored.
  3. `mkdir -p /media/rabbitears` (RAM root: the dir doesn't survive reboot, so never assume it exists).
  4. `mount -t "$TYPE" -o rw,noatime /dev/$MDEV /media/rabbitears`. Pass `-t` explicitly: busybox `mount` only auto-detects filesystems already in `/proc/filesystems`, and `exfat` isn't loaded until something asks for it.
- **remove:** if `/proc/mounts` shows `/dev/$MDEV` on `/media/rabbitears`, run `umount -l`. Lazy is required: mpv holds the playing file open, so a plain `umount` fails with EBUSY and leaves a dead mount behind.
- Log every action with `logger -t rabbitears-usb`.

### 3b. The mdev rule

Alpine's `/etc/mdev.conf` already has `sd[a-z].* root:disk 0660 */lib/mdev/persistent-storage`, and mdev stops at the first matching rule. Add a managed block at the **top** of `/etc/mdev.conf`:

```
# BEGIN rabbitears (managed by alpine/setup.sh — do not edit)
-sd[a-z][0-9]*	root:disk 0660 */etc/rabbitears/mdev-usb
# END rabbitears
```

- The leading `-` tells mdev to keep matching, so `persistent-storage` still runs. Verify on the VM that the `/dev/disk/by-*` symlinks still appear.
- `[0-9]*` matches both `sda` and `sda1`.

### 3c. Coldplug fallback

The stick may already be enumerated before the rule fires at boot. In the init script's `start_pre`, run the handler for every `/dev/sd*` node:

```sh
MDEV=<name> ACTION=add /etc/rabbitears/mdev-usb
```

That's one mount implementation, used by both hotplug and boot.

---

## Part 4: `alpine/setup.sh`: install the mount mechanism (same on every target)

- Remove the `fstab.snippet` TODO. **No target detection, no fstab entry, no 9p.**
- Install `system/mdev/rabbitears-usb` → `/etc/rabbitears/mdev-usb` with `install_file`, so it lands in the manifest and `verify.sh` covers it automatically.
- Add a helper `replace_managed_block FILE NAME SRC_FILE {top|bottom}`. It deletes any existing `# BEGIN NAME` … `# END NAME` block and inserts the current content fresh. Changes to the repo therefore reach the target on every deploy (a `grep -q || append` would freeze the first version forever). Write the file atomically, and only when the result differs, incrementing `CHANGED`. Use it for the `/etc/mdev.conf` block, with the block content in `system/mdev/mdev.conf.snippet`.
- **Extend `alpine/verify.sh`.** Managed blocks aren't whole files, so the manifest can't cover them. Add a check that extracts the `rabbitears` block from the target's `/etc/mdev.conf` and compares it with `system/mdev/mdev.conf.snippet`:
  - Report `DRIFT` if the block is missing, duplicated or edited.
  - Report `STALE` if the repo snippet changed since the last deploy. Record the snippet's hash in the manifest to tell these apart.
  - Make the check generic (a list of file + block name + snippet), so future managed blocks reuse it.
- Make sure `mdev` is in the `sysinit` runlevel (idempotent `rc-update add mdev sysinit`).
- After installing the rule, run the coldplug loop once, so a first deploy mounts an already-attached stick without a reboot. The reboot in validation is still the real test.

---

## Part 5: The app entry point

SCOPE §6 lists `__main__.py` as "entry point, main loop". Keep `__main__.py` a thin entry point and put the logic in **`src/rabbitears/app.py`** (add it to SCOPE §6) so it's unit-testable. Replace the placeholder `main()` in `src/rabbitears/__init__.py` (`pyproject.toml`'s `rabbitears = "rabbitears:main"` points at it) with one that calls `app.main()`. No CLI flags: everything comes from `load_config()`, identical on box and VM.

### Pure helpers (unit-tested)

- `media_present(mount_point) -> bool`: `os.path.ismount(mount_point)` **and** `channels/` is a directory. Existence alone isn't enough: on the box `/media/rabbitears` can be an empty directory with nothing mounted.
- `select_channel(channels, saved_number) -> Channel | None`: the saved channel if it still exists, else the lowest; `None` if there are none (SCOPE §4.8).
- `tune_target(files, epoch, now) -> (Path, offset)`: `compute_position(epoch, sum(durations), now)` → `find_file(files, position)`.
- `channel_label(ch) -> str`: e.g. `"CH 03 · Magic School Bus"` for `03-magic-school-bus`.

### `App`: injected player, clock, config, `media_present`. A `step()` method is called every 100 ms.

**Startup:**
1. Start the player.
2. Set volume to `min(state.volume, config.volume_cap)`.
3. Immediately `show_image(config.testpattern_path)`, so the screen shows NO SIGNAL rather than a console while waiting or indexing.

Load state from `mount_point/.rabbitears/state.json` once media is present.

**State NO_SIGNAL.** Every 5s, if `media_present`:
1. `scan_channels`, then `select_channel`.
2. Log `[rabbitears] indexing CH 03 · Magic School Bus (<n> files)...`.
3. Run `get_durations(channel.path, mount_point/".rabbitears"/"index.json")`.
4. Log `[rabbitears] CH 01: <n> playable, <m> skipped, loop <H>h<MM>m<SS>s (indexed in <t>s)`.
5. If there are playable files, tune → PLAYING.

Log "waiting for media" once per entry into the state, not every 5s.

**Tune:**
1. `tune_target` at `clock()`, then `player.load(path, offset)`.
2. Save state (channel, volume).
3. Log `[rabbitears] CH 03 · Magic School Bus — <filename> +<M>m<SS>s (position <p>s / loop <L>s)`.

**State PLAYING.** Each step:
- **Drive pulled.** If `not media_present` → `show_image(testpattern)`, log, NO_SIGNAL. Detection must happen within about 1s of the lazy unmount.
- **End of file.** If `player.is_idle()`, **recompute from the clock and load**. Never advance a counter (SCOPE §4.2).
- **Unplayable file** (SCOPE §4.7). If mpv goes idle within 2s of loading a file at an offset that isn't near that file's end, treat the file as unplayable. Log it, add it to a per-session skip set, drop it from the playlist (the loop length changes; log that), and retune. A bad file must never cause a reload storm.

**Crashes.** If `not player.is_running()`, or any `PlayerError` occurs: log and exit with status 1. supervise-daemon respawns the app, and the new instance re-tunes (SCOPE §4.7).

**Shutdown.** `SIGTERM`/`SIGINT` set a flag. The loop exits, saves state if media is present, runs `player.stop()`, and exits 0.

**Logging:** stdlib `logging` to stderr with a `[rabbitears]` prefix (captured into `/var/log/rabbitears.log` by the init script).

### Tests (`tests/test_app.py`)

Use a fake Player that records calls and returns scripted `is_idle`/`is_running`, a fake clock, and an injected `media_present`. Use a temp dir with empty `.mp4` files plus a pre-written `.rabbitears/index.json`, so `get_durations` reads from cache and no ffprobe is needed. Cover:
- boot with no media → test pattern shown, no `load`
- media appears → correct file and offset for a chosen clock value
- EOF after an arbitrary clock jump → target recomputed from the clock, not the next file in sequence
- saved channel present / missing → saved / lowest
- drive pulled while playing → test pattern, NO_SIGNAL; re-inserted → retunes at the *current* position
- unplayable file → skipped once, no reload loop
- mpv dies → exit status 1
- SIGTERM → state saved, `stop()` called, exit 0
- each pure helper

---

## Part 6: `system/openrc/rabbitears`: run the real app

Replace the placeholder with `command="/usr/bin/python3"` and `command_args="-m rabbitears"` (`PYTHONPATH=/opt` already comes from `supervise_daemon_args`). Add the Part 3c coldplug loop to `start_pre`, keeping the `/run/rabbitears` creation. Leave the logging, respawn and ordering from the foundation prompt intact.

---

## Part 7: taskipy tasks

Add to `[tool.taskipy.tasks]`, each with a `help` string:

| Task | Command |
|---|---|
| `usb-build` | `./vm/make-usb.sh`; args appended (`--reformat`, `--reset-state`, …) |
| `vm-run-usb` | `python vm/vmctl.py run --usb /var/lib/rabbitears-vm/usb.img` |
| `usb-unplug` | `python vm/vmctl.py usb unplug` |
| `usb-plug` | `python vm/vmctl.py usb plug` |

- The fixed `/var/lib/rabbitears-vm` path is `provision.sh`'s `VM_BASE`, the same on every clone, so it's not a personal path.
- Don't add a task that edits the stick's content in place. Content changes go through `<staging dir>` → `usb-build`.

---

## Part 8: Docs

- **`AGENTS.md`:** add the new tasks to the VM dev loop section. Add one line: the mock drive is an emulated USB stick (`usb-build`, `vm-run-usb`, `usb-unplug`/`usb-plug`), mounted by the same mdev rule as the box.
- **`vm/VAGRANT.md`:** the "Mock USB drive" section covers:
  - the 1a commands
  - `uv run task usb-build`
  - `uv run task vm-run-usb`
  - `uv run task usb-unplug` / `usb-plug`
  - the underlying script or `vmctl.py` command for each, shown once
  - that `make-usb.sh` needs `uv run task setup-sudoers` to have been run once
  - why a USB device and not 9p (paradigm rule 4)
- **`SCOPE.md`:**
  - §4.8: "mdev mounts the drive by label `RABBITEARS` on hotplug and at boot".
  - §6 repo layout: replace `system/fstab.snippet` with `system/mdev/` (`rabbitears-usb`, `mdev.conf.snippet`); add `src/rabbitears/app.py`, `vm/make-usb.sh`, `tools/transcode.sh` (if not already listed).
  - §6 installed paths: add `/etc/rabbitears/mdev-usb`.
  - §11: resolve the exFAT-vs-ext4 open question as exFAT, matching `packages.txt`.
  - §10 decisions log, add:
    - "mdev hotplug mount by label, no fstab entry"
    - "VM media is an emulated USB stick, not 9p"

---

## Part 9: End-to-end validation (from a fresh build)

Run every step. Report evidence (command output, log excerpts, image dimensions), not assertions.

1. **Unit tests:** `uv run task test`, all passing.
2. **Content + image:**
   - Transcode done; `uv run task media-check …` exits 0.
   - `uv run task usb-build` prints the summary.
   - A second `usb-build` copies nothing.
3. **Fresh VM:**
   ```sh
   uv run task vm-rebuild      # media/ and usb.img survive destroy
   uv run task vm-run-usb &
   uv run task deploy-vm       # ends with verify: OK
   ```
   No manual step inside the VM, ever.
4. **Mount came from mdev.**
   - `/proc/mounts` shows `/dev/sda1 /media/rabbitears exfat`.
   - `logger` output in `/var/log/messages` shows `rabbitears-usb` mounted it.
   - `ls /media/rabbitears/channels/<NN-slug> | wc -l` matches the transcoded count.
   - `/dev/disk/by-*` symlinks still exist.
5. **First boot indexes.** The log shows the indexing line and the summary line. `index.json` now exists on the stick.
6. **Correct schedule position.**
   - Inside the VM, query mpv over `/run/rabbitears/mpv.sock` for `path` and `time-pos` (a `python3 -c` one-liner run over SSH; this is the VM's system Python, not uv).
   - Independently, on the host, compute the expected file and offset: read `index.json` over SSH and apply the SCOPE §4.2 math with `epoch=0` and the same `date +%s`.
   - They must agree within 3s. Show the math.
7. **Real video on screen.**
   - `uv run task vm-shot`, then again 3s later (pass `--out` to keep both).
   - Look at both images and describe them: they must show a video frame, not black and not the NO SIGNAL card.
   - The two images must differ (motion).
8. **Second reboot (cache):** `uv run task deploy-vm`. The log shows indexing time < 1s and a tune at the new, correct position (re-check step 6).
9. **Pull the plug.** `uv run task usb-unplug`. Within 5s:
   - `/media/rabbitears` is no longer in `/proc/mounts` (lazy umount by mdev)
   - the log shows NO SIGNAL
   - a screenshot shows the test pattern
10. **Re-insert.** `uv run task usb-plug`. Within 10s:
    - mdev mounts it again
    - the app retunes at the *current* schedule position (re-check step 6)
    - a screenshot shows video
11. **Boot with no drive.** `uv run task usb-unplug`, then `uv run task deploy-vm`: NO SIGNAL after boot, and verify still OK (verify must not depend on the drive). `uv run task usb-plug`: video.
12. **Crash recovery.**
    - `pkill -9 mpv` → the app exits 1, supervise-daemon respawns it, video returns.
    - `pkill -9 -f 'python3 -m rabbitears'` → mpv dies with it (PDEATHSIG; no orphan holding DRM), respawn, video returns.
13. **Idempotent deploy.** `uv run task deploy-vm` twice more:
    - the second `setup.sh` run reports `0 changed`
    - exactly one `BEGIN rabbitears` block in `/etc/mdev.conf`
    - no lbu WARNING
    - `verify: OK`
    - the service is playing after each reboot
14. **Drift on the managed block.** Over SSH, hand-edit the `rabbitears` block in `/etc/mdev.conf` (a deliberate test of the guard).
    - `uv run task vm-verify` must report `DRIFT` for it and exit 1.
    - `uv run task deploy-vm` must repair it (`1 changed`) and end with `verify: OK`.

---

## Completion criteria

1. The stick image is partitioned exFAT labelled `RABBITEARS`, built idempotently by `vm/make-usb.sh` from SCOPE §5-compliant content staged at `/var/lib/rabbitears-vm/media`.
2. `vmctl.py run --usb IMG` attaches it as a USB mass-storage device; `vmctl.py usb unplug|plug` hot-unplugs and re-inserts it; QMP event interleaving is handled.
3. The drive is mounted solely by the mdev rule plus its boot-time coldplug fallback. That mechanism is identical for VM and box, with no detection branch, no fstab entry and no 9p.
4. From `uv run task vm-rebuild` → `uv run task deploy-vm`, the box boots, indexes, and plays the schedule-correct file at the schedule-correct offset, with no manual steps.
5. Pull, re-insert, boot-without-drive, mpv crash, and app crash all recover with no intervention.
6. All files the box needs at boot are lbu-covered (the `setup.sh` self-check is clean).
7. Repeat deploys report `0 changed`. `verify.sh` covers the mdev handler (via the manifest) and the `/etc/mdev.conf` managed block, and catches hand edits to either.
8. `usb-build`, `vm-run-usb`, `usb-unplug` and `usb-plug` exist with accurate help.
9. All previously passing tests still pass, plus the new vmctl and app tests.

---

## Report back with

- Transcode results: files transcoded / skipped / failed (list failures with the ffmpeg error), plus the `--check` output.
- `usb-build` summary from the first and second runs.
- First-boot index: number of files probed, number with valid durations, total loop length, indexing time. Then the second-boot indexing time.
- The schedule math from step 6 (epoch, now, loop length, position, file, offset) next to mpv's reported `path`/`time-pos`.
- Screenshot paths, dimensions, and what's visible in each (steps 7, 9, 10).
- The `rabbitears-usb` log lines for mount, lazy unmount, and re-mount, and what busybox sets `ACTION` to during coldplug.
- Output of the final `deploy-vm` run (setup's `changed` count and `verify: OK`), the managed-block DRIFT test from step 14, and `uv run task --list`.
- Any problems with the USB emulation, exFAT, the mdev rule ordering, the mpv DRM context on virtio-gpu, or OpenRC wiring, and anything from the paradigm you couldn't satisfy.
