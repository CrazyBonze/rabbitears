# Agent prompt: VM + deploy foundation fixes (run before first-channel-vm-playback)

**Goal: fix the bugs and structural problems that would stop the dev VM from acting like a faithful, idempotently-built copy of the appliance. After this task there is one VM, built one way and run one way. A single scripted path gets the repo onto any target (VM or box). A read-only check proves the target still matches the repo, and every common action is a `uv run task …` that goes through those scripts. The player, init script, and media tooling are hardened for the first-channel work that follows.**

This task adds **no new app features**. `src/rabbitears/__main__.py`, USB mounting, and the mdev hotplug rule belong to the next prompt (`docs/prompts/first-channel-vm-playback.md`). Don't start them here.

Work from the repository root (paths below are repo-relative). Read these in full before making any changes: `AGENTS.md`, `SCOPE.md`, `docs/alpine-install.md`, `vm/VAGRANT.md`, `vm/provision.sh`, `vm/install.py`, `vm/vmctl.py`, `vm/export-disk.sh`, `vm/setup-sudoers.sh`, `pyproject.toml` (the `[tool.taskipy]` sections), `alpine/setup.sh`, `alpine/packages.txt`, `alpine/answerfile`, `system/openrc/rabbitears`, `src/rabbitears/player.py`, `src/rabbitears/config.py`, `src/rabbitears/index.py`, `tests/test_player.py`, `tests/test_vmctl.py`, `tests/test_config.py`, `tests/test_index.py`.

---

## The paradigm (every change must respect this)

1. **The repo is the only source of truth.** A target (VM, Box A, Box B) is base install + `alpine/deploy.sh`. Nobody hand-edits a target. If a target drifts, fix the repo.
2. **Everything is scripted and idempotent.** Re-running any script converges to the same state. It must not reinstall, duplicate config lines, or fail on the second run.
3. **Only the base install differs between VM and box** (`docs/alpine-install.md`). Standard install on the VM, diskless + lbu on the box. `setup.sh` must not guess which machine it is on or branch on it.
4. **The VM mirrors the Wyse 3040** where it matters: 4 vCPU, 2 GB RAM, 8 GB system disk, `linux-lts` kernel, OpenRC, mdev.
5. **Service validation means a real reboot**, not `rc-service restart` (`docs/alpine-install.md` §3, "M1 gate").
6. **Every file the box needs at boot must be persisted by lbu.** That means it lives under `/etc` or under a path that `setup.sh` passes to `lbu include`. The VM is a standard (non-diskless) install, so it will *not* catch violations of this. Check by inspection.

### Host-side conventions

- **Never prefix `vm/provision.sh` with `sudo`.** Run it as `./vm/provision.sh …`. It elevates itself (`exec sudo "$0" "$@"`). `vm/setup-sudoers.sh` installs a NOPASSWD rule for this checkout's absolute path to it, so it runs without a password prompt. Keep the self-elevation working, and keep the script at `vm/provision.sh`. If it prompts for a password, the user hasn't run `./vm/setup-sudoers.sh` yet: stop and ask them to.
- **Host Python runs through `uv`, never bare `python3`.** The project pins Python 3.14 (`.python-version`, `pyproject.toml`), the same version as the VM and appliance. Bare `python3` is correct only **inside the VM/box**: Alpine's system `/usr/bin/python3` from apk runs the app, and there is no uv there.
- **Common actions are taskipy tasks** (`[tool.taskipy.tasks]` in `pyproject.toml`; `uv run task --list`). Use `uv run task test`, `uv run task vm-build`, etc., rather than retyping the underlying commands. Inside a task's `cmd`, `python` already resolves to the uv environment.
- **Task rules.** Every task that changes a target (VM or box) must call the repo's idempotent scripts (`provision.sh`, `deploy.sh` → `setup.sh`).
  - Never add a task that runs an ad-hoc command on a target over SSH or serial.
  - Every host-side script a developer runs repeatedly gets a task with a `help` string.
  - taskipy appends extra CLI args to the main `cmd` only (not to `pre_`/`post_` hooks), so a task whose follow-up step needs the same args must do that step inside the script.
- **Exception: leave `provision.sh`'s root-side Python alone.** Its root-side Python (`install.py`, and the `console` heredocs that need apt's `python3-paramiko`/`python3-serial`) runs as root under sudo's `secure_path`. Leave it on system `python3` unless you've confirmed uv works there; changing it is out of scope.
- For any other command that needs root, stop and ask the user to run it. Don't add sudoers rules, setuid bits, or disk copies to work around it.
- **No personal paths.** Don't hardcode a home directory, username, repo location or media library path in any script, test or doc. Take them as arguments or derive them (`$SUDO_USER`, the script's own location).

---

## Part 1: One VM. `provision.sh` builds it, `vmctl.py` runs it

Today there are two VMs. One is the libvirt domain from `provision.sh` (4 vCPU, IDE, e1000, NAT). The other is the raw QEMU from `vmctl.py` (`-smp 2`, virtio, user-net, SSH on 2222). Both point at the same qcow2, and a running libvirt domain locks the image. Settle the roles: **`provision.sh` installs the base OS and leaves the disk at rest. `vmctl.py` is the only way the dev loop boots it.**

### 1a. `vm/provision.sh`

- **Re-running `create` must not reinstall.** Read `vm/install.py` and confirm this: on an already-installed disk, a re-run waits for `login:`, logs in, and runs `setup-alpine -f` again with `ERASE_DISKS` set, wiping the installed system. Fix `create` so that if the domain is already defined and already boots from disk (no `<kernel>` in `virsh dumpxml`, `<boot dev='hd'/>` present), it logs "already installed; use `destroy` to rebuild" and skips `install.py`.
- **Leave the disk at rest after install.** After `install.py` succeeds, shut the domain down: `virsh shutdown`, wait up to 60s for `shut off`, then fall back to `virsh destroy`. The domain stays defined (for `console`/`liveboot` debugging) but not running, so `vmctl.py` can open the disk.
- **Add a `stop` subcommand** that does the same shutdown, idempotently (a no-op if already off). Add it to the usage header and to the `die "unknown command"` message. That message is already out of date: it omits `console`, `console-cmd`, `diskboot`, `liveboot`, `proxy`.
- **Remove the guest-agent channel** bound at `/tmp/rabbitears-qmp.sock` from the domain XML in `create`. It is a virtio-serial guest-agent channel, not a QMP monitor. It collides with `vmctl.py`'s QMP socket path, and no guest agent is installed.
- **Update the "Next steps" banner** to the new loop: `uv run task vm-run` → `uv run task deploy-vm` (see Parts 2 and 9).
- **Usage header:** change `sudo ./vm/provision.sh …` to `./vm/provision.sh …` and add a note that it self-elevates under the NOPASSWD rule.

### 1b. `vm/vmctl.py`

- `qemu_args()` default `smp="4"`, to mirror `VM_VCPUS=4` and the 3040's 4 cores. Update the docstring and `VAGRANT.md`.
- `run --disk` defaults to `/var/lib/libvirt/images/rabbitears-vm.qcow2` (constant `DEFAULT_DISK`). It is no longer required.
- **Preflight in `cmd_run`**, before spawning QEMU. Each check exits 1 with a one-line, actionable message:
  - Run `virsh -c qemu:///system domstate rabbitears-vm`. If it reports `running`, tell the user to run `./vm/provision.sh stop`. Best-effort: if `virsh` is missing or errors, skip this check.
  - If the disk is not readable and writable by the current user, report the path and its owner/group and point at the `kvm` group (provision.sh adds the user to it; a re-login is needed).
- Factor the preflight into a function that takes its `subprocess.run`/`os.access` dependencies as parameters, or patch them in tests, so it is unit-testable.

### 1c. Retire the disk-copy workaround

`vm/export-disk.sh` existed only to let an unprivileged `vmctl.py` boot a copy of the libvirt-locked disk. It also hardcodes a disk path and is meant for a per-user sudoers rule. With 1a/1b it's obsolete: delete it, and delete `vm/setup-export-sudoers.sh` too if it's still in the tree. **Don't touch `/etc/sudoers.d/`.** `vm/setup-sudoers.sh` (already in the repo) is the only supported way to grant root, and it removes the old `rabbitears-export-disk` rule when the user runs it. Leave `vm/setup-sudoers.sh` as-is except to keep its `SCRIPTS` list accurate.

### 1d. Tests (`tests/test_vmctl.py`)

- Update `test_run_spawns_qemu_with_exact_flags` for `-smp 4`.
- `test_run_disk_defaults_to_libvirt_image`
- `test_preflight_refuses_when_libvirt_domain_running`
- `test_preflight_skips_domain_check_when_virsh_missing`
- `test_preflight_refuses_unwritable_disk`

---

## Part 2: `alpine/deploy.sh`, the one way code reaches a target

SCOPE §6 lists `alpine/deploy.sh`, but it doesn't exist. Nothing gets the repo onto the VM today. Create it (bash, host-side, `set -euo pipefail`).

```
alpine/deploy.sh [--vm | --host H [--port P] [--key K]] [--restart | --reboot] [--commit]
```

- `--vm` is shorthand for `--host 127.0.0.1 --port 2222 --key vm/keys/rabbitears-vm`, plus `StrictHostKeyChecking=no` and `UserKnownHostsFile=/dev/null`. The VM's host key changes on every rebuild. Only `--vm` relaxes host-key checking; real boxes use normal SSH verification.
- **Transport: `tar` over `ssh`, not rsync.** A freshly installed box has no rsync, and `setup.sh` is what installs packages, so deploy must not depend on anything beyond the base install. Stream the working tree (not `git archive`; the dev loop deploys uncommitted changes) restricted to `alpine/ system/ src/ tools/testpattern.png`, excluding `__pycache__` and `*.pyc`. Unpack into `/tmp/rabbitears-repo` on the target after `rm -rf` of the previous copy. That path is RAM on the box, transient by design: the installed result is what persists, not the staging copy.
- Before anything else, wait (up to 120s) for SSH to answer, so `uv run task vm-run &` followed immediately by `uv run task deploy-vm` works while the VM is still booting.
- Run `sh /tmp/rabbitears-repo/alpine/setup.sh` on the target and stream its output.
- `--commit`: `lbu commit` after setup and **before** any reboot (a box release action). On the VM (`LBUOPTS=none`) it's a no-op; warn that it is.
- `--restart`: `rc-service rabbitears restart`. `--reboot`: `reboot`, then wait (up to 120s) until SSH answers again and `rc-service rabbitears status` reports started. Print the result. These two are mutually exclusive.
- **Diskless guard.** On the diskless box, `/` is RAM, so `--reboot` without `--commit` silently throws away everything `setup.sh` just did and boots the last committed apkovl. Detect a diskless target by `/` being `tmpfs` in the target's `/proc/mounts`; there, refuse `--reboot` unless `--commit` is also given, with a message explaining why. This check lives in `deploy.sh`, not `setup.sh`: it reflects the one sanctioned difference, the base install.
- **Every deploy ends by running `alpine/verify.sh`** (Part 3b) against the same target, and its exit status becomes deploy's exit status. `--no-verify` skips it. This lives in the script, not a taskipy `post_` hook, so it also gets the `--host` args.
- Running it twice in a row must produce the same target state with no errors, and the second `setup.sh` run must report `0 changed` (Part 3).

Update the SCOPE §6 one-line description of `deploy.sh` to match (tar over ssh, then `setup.sh`), and add the reason to the decisions log.

---

## Part 3: `alpine/setup.sh`: fill the deploy TODOs, fix lbu coverage

- **App code** (replace the step-4 TODO). Install `${REPO_ROOT}/src/rabbitears/` to `/opt/rabbitears` with an atomic swap and no rsync dependency:
  1. Copy to `/opt/rabbitears.new`.
  2. Delete any `__pycache__`.
  3. `mv` the old tree aside, move the new one into place, remove the old.

  A re-run must leave exactly the repo's files (files deleted from the repo disappear from the target).
- **Test pattern.** Install `tools/testpattern.png` to `/usr/share/rabbitears/testpattern.png` and add `lbu include /usr/share/rabbitears`. Not `/opt/tools/`: that would not be lbu-included and would vanish on the diskless box after a reboot.
- Keep `lbu include /opt/rabbitears`. Make every `lbu include` idempotent: check `lbu include -l` first so re-runs don't duplicate entries.
- Leave the `fstab.snippet` TODO in place. The next prompt replaces it with the mdev hotplug rule. Don't add any VM-vs-box detection.
- At the end, print every path `setup.sh` wrote outside `/etc` and confirm each one is under an `lbu include` path. Print a loud `WARNING` line for any that isn't. This is the inspection check from paradigm rule 6, made automatic.
- **Change-aware installs.** Add a helper `install_file SRC DEST MODE`. It writes only when DEST is missing or its content or mode differs, and increments a `CHANGED` counter when it writes. Use it for every file `setup.sh` places, including each file of the app tree. Skip the `/opt/rabbitears` swap entirely when the tree is already identical. End with `log "setup complete: ${CHANGED} changed"`. A second run with no repo changes must print `0 changed`; that's the idempotency proof.
- **Install manifest.** Write `/etc/rabbitears/manifest` atomically, at the end of every run. It lives under `/etc`, so lbu persists it. Format: one line per managed file, tab-separated `sha256<TAB>target path<TAB>repo-relative source`. It records exactly what the repo put on the target.

## Part 3b: `alpine/verify.sh`, the read-only drift check

Hand edits on a target are the failure mode the paradigm forbids, and today nothing would notice one. Create `alpine/verify.sh` (bash, host-side). It takes the same target flags as `deploy.sh` (`--vm | --host H [--port P] [--key K]`) and **never modifies the target**. Over SSH it checks:

1. **Drift on target.** Every manifest entry's current target hash equals the recorded hash. Report each mismatch as `DRIFT <path>: changed on the target since the last deploy. Fix the repo and redeploy; don't edit the target.`
2. **Stale deploy.** Every recorded hash equals the current hash of its repo source (computed locally). Report each mismatch as `STALE <path>: the repo changed since the last deploy. Redeploy (uv run task deploy-vm / deploy-box).`
3. **Unmanaged files** in `/opt/rabbitears` and `/usr/share/rabbitears` that aren't in the manifest.
4. **Packages.** Every package in `alpine/packages.txt` is installed (`apk info -e`). Parse the file the same way `setup.sh` does.
5. **Services.** `sshd`, `chronyd` and `rabbitears` are in the `default` runlevel, and `rabbitears` is started.
6. **lbu.** Each `lbu include` path appears exactly once in `lbu include -l`.

Print one line per finding and a final `verify: OK` or `verify: N problems`; exit 0 only if clean. A missing manifest means "never deployed": report it, exit 1. Structure it so the next prompt can add checks (it adds the mdev managed block).

---

## Part 4: `system/openrc/rabbitears`: logging, ordering, respawn

Keep the placeholder `command` for now (the next prompt switches it to `python3 -m rabbitears`), but fix everything around it:

- **Logging.** `output_log="/var/log/rabbitears.log"` and `error_log="/var/log/rabbitears.log"`. On the box `/var/log` is RAM and is discarded at reboot. Record this as an explicit, intended exception to SCOPE §3's "never writes to `/`" rule (logs only, RAM-backed, non-persistent), and add a sentence to SCOPE §3 saying so.
- **Environment.** `supervise_daemon_args="--env PYTHONPATH=/opt"`, so the next prompt only has to change `command`/`command_args`. Verify on the VM that the supervised process actually sees it (`cat /proc/<pid>/environ`).
- **Respawn.** supervise-daemon's default `respawn_max` gives up after repeated crashes, which leaves the box stuck. That violates SCOPE Goal 2. Set `respawn_max=0` (unlimited) and `respawn_delay=2` (no tight crash loop).
- **Ordering.** `depend()`: add `need localmount` and `after bootmisc`; keep `use net`.

---

## Part 5: `src/rabbitears/player.py`: make it usable by a real main loop

The next prompt's main loop needs to detect end-of-file, show a still image, and survive crashes. The current wrapper can't support that:

1. **IPC replies vs. events.** mpv writes async `{"event": ...}` lines to every client, and they can arrive before the reply to a command. `_send` reads one line and would misparse an event as the reply. Fix it: attach an incrementing `request_id` to every command and read lines until the reply with the matching `request_id` arrives, skipping event lines and replies with other IDs. Apply the existing IPC timeout to the whole exchange.
2. **`get_property(name, default=None)`.** Returns the `data` field. Returns `default` when mpv answers `"property unavailable"` (e.g. `time-pos` while idle); other errors still raise `PlayerError`.
3. **`is_idle() -> bool`.** `get_property("idle-active", False)`. With `--idle=yes --keep-open=no`, mpv goes idle at end-of-file, so this is the EOF signal.
4. **`is_running() -> bool`.** The mpv process is alive.
5. **`show_image(path)`.** `loadfile` the image. Add `--image-display-duration=inf` to the start args: mpv's default is 1 second, after which it goes idle and the NO SIGNAL screen would turn black.
6. **Stale socket.** In `start()`, create the socket's parent directory and unlink any existing socket file before launching. Otherwise a socket left by a crashed previous run makes the "wait for socket" loop return immediately, before mpv is listening.
7. **No orphaned mpv.** If the Python app is SIGKILLed, its mpv child survives and keeps DRM master, so the respawned app's mpv can't open the display. Launch mpv with a `preexec_fn` that calls `prctl(PR_SET_PDEATHSIG, SIGTERM)` via `ctypes` (works on musl). Fall back silently if `prctl` is unavailable.

Tests (`tests/test_player.py`): use a real `AF_UNIX` listener in a thread acting as a fake mpv. Cover:
- an event line arriving before the reply
- a reply with a mismatched `request_id`
- `get_property` returning `default` for "property unavailable" and raising on other errors
- `is_idle`
- `show_image` sending `loadfile`
- `start()` removing a stale socket file (update `test_start_returns_when_socket_appears` so it still holds)
- `--image-display-duration=inf` present in the args

Keep all existing tests passing; update them only where the request_id change requires it.

---

## Part 6: `src/rabbitears/config.py`

Add `testpattern_path: Path`, default `/usr/share/rabbitears/testpattern.png`, loaded and validated like the other path keys. Add tests mirroring the existing `mount_point` tests.

---

## Part 7: `src/rabbitears/index.py`: never drop files silently

`get_durations` only globs `*.mp4`. Other files in a channel folder (`.webm`, `.mkv`, …) are ignored without a trace, and so are unprobeable files. Log both via `logging.getLogger("rabbitears.index")` at WARNING: one line per skipped file, with the reason ("not .mp4 — transcode with tools/transcode.sh" / "ffprobe failed"). Ignore `channel.toml` and dotfiles. Behaviour otherwise unchanged. Add tests with `assertLogs`.

---

## Part 8: `tools/transcode.sh`, which makes content follow SCOPE §5

Typical source media is a mix of `.mp4`, `.webm` and `.mkv`. Much of it is **VP9** (banned by SCOPE §5) or 1080p H.264. Copying originals onto the "USB drive" would make the VM fall back to software decode of content the 3040 can't hardware-decode, and the run would pass falsely. SCOPE §5 names `tools/transcode.sh`, but it doesn't exist. Create it (bash, desktop-only, never run on the box). The source directory is always an argument, never a path baked into the script.

```
tools/transcode.sh [-j JOBS] SRC_DIR DEST_DIR     # transcode
tools/transcode.sh --check DIR                    # verify compliance, exit 1 on any violation
```

**Transcode mode.** Every video file in `SRC_DIR` (`mp4 mkv webm avi mov m4v`, case-insensitive; non-recursive) becomes `DEST_DIR/<same basename>.mp4`. Filenames become on-screen titles, so keep them.
- Video: libx264, `-profile:v high -level:v 4.1`, `yuv420p` (8-bit).
- Scaling: fit within 1280×720 preserving aspect ratio, **never upscale**, even dimensions.
- Audio: first audio stream, AAC stereo 48 kHz. Files with no audio are allowed.
- `-movflags +faststart`. Video and first audio only; no subtitle or data streams.
- **Idempotent and resumable.** Skip a file if its output exists, is newer than the source, and passes the same compliance check as `--check`. Write to `<name>.mp4.part` and `mv` into place only on success, so an interrupted run never leaves a half-file that later looks done.
- `-j` runs N files in parallel (default: half the cores).
- End with a summary: transcoded / skipped / failed counts.

**Check mode.** ffprobe every `.mp4` in `DIR` and fail any file that isn't: H.264 High (or a lower profile), level ≤ 4.1, ≤ 1280×720, 8-bit `yuv420p`, AAC (if audio is present). Print one line per violation.

**Verify on 3 files only.** Ask the user which source media directory to use; don't guess or search their filesystem. From it, pick at least one VP9 `.webm`, one 1080p H.264 and one ≤720p file; use `ffprobe` to find them. Copy those 3 into a temp source dir, transcode into another temp dir, and run `--check` on the result. Then re-run the transcode and confirm all 3 are skipped. If the user's library lacks one of those kinds, generate a short synthetic clip with `ffmpeg -f lavfi -i testsrc2=…` (e.g. `-c:v libvpx-vp9` at 1920×1080). **Don't** run a full-library transcode; that's the next prompt.

---

## Part 9: taskipy tasks

`taskipy` is already a dev dependency, and `pyproject.toml` already has these working tasks: `test`, `unit`, `setup-sudoers`, `vm-proxy`, `vm-build` / `vm-rebuild` (each with a `pre_` hook that starts the proxy), `vm-status`, `vm-run`, `vm-serial`, `vm-shot`. Keep them, keep `cwd = "."`, and give every new task a `help` string.

- `vm-run`: drop the `--disk …` argument once `DEFAULT_DISK` exists (1b).
- Add:

| Task | Command |
|---|---|
| `vm-stop` | `./vm/provision.sh stop` |
| `deploy-vm` | `alpine/deploy.sh --vm --reboot` (verify runs inside) |
| `vm-verify` | `alpine/verify.sh --vm` |
| `deploy-box` | `alpine/deploy.sh --commit --reboot`; the user appends `--host H`. `--commit` comes first because the box is diskless. |
| `box-verify` | `alpine/verify.sh`; the user appends `--host H` |
| `transcode` | `tools/transcode.sh`; args appended |
| `media-check` | `tools/transcode.sh --check`; args appended |

- **Don't add** a `--restart` deploy task. The paradigm validates with a reboot, and `deploy.sh --restart` stays available for whoever needs it explicitly.
- **Don't add** any "run this command on the VM" task.
- Run `uv run task --list` and check that every task's help is accurate.

---

## Part 10: Docs

- **`AGENTS.md`:** update the Commands and VM dev loop sections to the task names. State that `task deploy-vm` (or `deploy-box`) is the only way to change a target, and that SSH is for read-only inspection. Note that `vm-verify` reporting DRIFT means someone hand-edited the target: fix the repo and redeploy.
- **`vm/VAGRANT.md`:**
  - Rewrite around the single loop: `uv run task setup-sudoers` (once per clone) → `uv run task vm-build` (build; leaves the disk at rest) → `uv run task vm-run` → `uv run task deploy-vm` → `uv run task vm-shot`. Show the underlying script for each step once, so the docs still explain what runs.
  - Every host-side `python3 vm/vmctl.py …` becomes `uv run vm/vmctl.py …` (or its task). The "Prerequisites" line becomes "uv (the project pins Python 3.14, matching the VM/appliance; vmctl.py is stdlib-only)".
  - Update the QEMU invocation (`-smp 4`, default disk). Drop references to creating disks by hand.
  - **Correct the VAAPI claims.** virtio-gpu has no VAAPI/i965, so this VM validates the DRM/KMS output path, the app, OpenRC wiring and media handling, **not** hardware decode. Remove `vainfo | grep i965` and "no fallback to software decode" from the VM checklist, and state that hwdec is validated only on Box A (M1).
- **`docs/alpine-install.md`:** drop the `sudo` prefix from every `vm/provision.sh` command (it self-elevates). Add `./vm/setup-sudoers.sh` as the one-time first step in §1. In §3's checklist and the M1 gate block, replace "`./alpine/setup.sh` ran clean" with `alpine/deploy.sh … --reboot`, and mention `/var/log/rabbitears.log`.
- **`SCOPE.md`:**
  - §6 "Installed on the box": add `/usr/share/rabbitears/testpattern.png` and `/var/log/rabbitears.log`.
  - §6 repo layout: drop nothing yet.
  - §6 repo layout: add `alpine/verify.sh`.
  - §7 development workflow: use the task names, and add a step: `deploy` ends with `verify`, and DRIFT means fix the repo, not the box.
  - §10 decisions log, add:
    - provision.sh builds / vmctl.py runs
    - deploy via tar over ssh
    - VM validates the output path, not VAAPI
    - taskipy tasks as the entry points; manifest + `verify.sh` drift check

---

## Validation

1. `uv run task test`: all pass.
2. `shellcheck alpine/setup.sh alpine/deploy.sh alpine/verify.sh tools/transcode.sh vm/provision.sh vm/setup-sudoers.sh` if shellcheck is installed: no errors. Note warnings you chose to keep.
3. Fresh build, end to end. This **destroys the current VM**, which is expected because it holds only a base install. First check `sudo -n -l` lists `vm/provision.sh` under NOPASSWD; if not, ask the user to run `uv run task setup-sudoers`.
   ```sh
   uv run task vm-rebuild     # proxy, destroy, build; ends with the domain shut off
   uv run task vm-build       # second run: "already installed", no reinstall
   uv run task vm-run &       # defaults: disk, 4 vCPU, QMP socket
   uv run task deploy-vm      # ends with verify: OK
   uv run task deploy-vm      # second deploy: setup reports "0 changed", verify: OK
   ```
   After the second deploy, over SSH, confirm:
   - `rc-service rabbitears status` → started
   - `/var/log/rabbitears.log` exists
   - the supervised process has `PYTHONPATH=/opt`
   - `/opt/rabbitears` matches `src/rabbitears` with no `__pycache__`
   - `/usr/share/rabbitears/testpattern.png` exists
   - `lbu include -l` lists both paths exactly once
   - `setup.sh` printed no lbu WARNING
   - `nproc` is 4
4. **Drift detection.** Over SSH, append a comment to `/etc/init.d/rabbitears` (the one deliberate hand edit, made to test the guard). `uv run task vm-verify` must exit 1 with a `DRIFT` line for that file. `uv run task deploy-vm` must repair it, report `1 changed`, and end with `verify: OK`. Then touch a comment into `system/openrc/rabbitears` in the repo without deploying: `vm-verify` must report `STALE`. Revert the repo change and redeploy.
5. Stop `vmctl.py` (Ctrl+C), start the libvirt domain (`virsh -c qemu:///system start rabbitears-vm`), and confirm `uv run task vm-run` is refused by preflight with the actionable message. Then run `uv run task vm-stop` and confirm `uv run task vm-run` boots again.
6. Kill the supervised child: `supervise-daemon` respawns it after ~2s, indefinitely.
7. The 3-file transcode check from Part 8, run through `uv run task transcode …` and `uv run task media-check …`.
8. The diskless guard: unit-test or dry-run it. `deploy.sh --reboot` without `--commit` against a target whose `/` is `tmpfs` must refuse. Mock or stub the SSH call; no real box is needed.

---

## Completion criteria

1. `provision.sh` re-runs are no-ops on an installed VM and leave the domain shut off; `stop` exists; no guest-agent socket collides with the QMP path.
2. `vmctl.py run` needs no arguments, boots 4 vCPU / 2 GB, and refuses to start against a running libvirt domain.
3. `uv run task deploy-vm` takes a fresh base install to a running `rabbitears` service with no manual in-VM step. It's idempotent (the second run reports `0 changed`), refuses an uncommitted reboot on a diskless target, and ends with a clean `verify.sh`.
4. `setup.sh` installs the app atomically and only when changed, writes the install manifest, puts the test pattern at an lbu-persisted path, and self-checks lbu coverage.
5. `alpine/verify.sh` detects target drift, stale deploys, unmanaged files, missing packages, unwired services and lbu duplication, and is read-only.
6. Every common host action is a `uv run task …` with accurate help, and no task runs ad-hoc commands on a target.
7. The init script logs, orders after `localmount`, exports `PYTHONPATH`, and respawns forever.
8. `Player` handles interleaved events, exposes `get_property`/`is_idle`/`is_running`/`show_image`, clears stale sockets, and can't orphan mpv.
9. `tools/transcode.sh` produces SCOPE §5-compliant files idempotently, and `--check` catches violations.
10. `export-disk.sh` (and `setup-export-sudoers.sh`, if present) are gone; `vm/setup-sudoers.sh` is the only root-grant path. The docs describe the single loop and the VM's real validation scope. No new script contains a personal path.
11. All tests pass.

---

## Report back with

- Every file created, changed, or deleted, with one line on why.
- Full `uv run task test` output, and `uv run task --list`.
- Output of the second `vm-build` run and of the second `deploy-vm` (showing `0 changed` and `verify: OK`).
- The drift-detection run from validation step 4 (the DRIFT and STALE lines, and the repairing deploy).
- Confirmation (or refutation) of the `install.py` reinstall-on-rerun hazard, and how you detected "already installed".
- The 3-file transcode results: source codec/resolution → output codec/profile/level/resolution, and the `--check` output.
- Anything from the paradigm you couldn't satisfy, and why.
- A reminder to the user to re-run `./vm/setup-sudoers.sh`. It's idempotent and removes the obsolete `rabbitears-export-disk` rule.
