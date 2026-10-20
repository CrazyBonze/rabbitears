# AGENTS.md — Rabbit Ears

Kid-safe "cable box" for a Dell Wyse 3040: Alpine (diskless + lbu), OpenRC, one long-running mpv driven by a small Python app. **`SCOPE.md` is the source of truth.** Anything not in it is out of scope, including Kodi, X11/Wayland, streaming, on-demand browsing and number-key entry. When a design decision changes, update `SCOPE.md` (§6 layout, §10 decisions log) in the same change.

## Commands (run from the repo root)

- Common actions are **taskipy** tasks (dev dependency, `[tool.taskipy.tasks]` in `pyproject.toml`). Run `uv run task --list` to see them.
  - Tasks run from the repo root (`cwd = "."`).
  - Extra args are appended to the task's main `cmd` only, never to its `pre_`/`post_` hooks.
- Tests: `uv run task test`. 4 skips in `test_player.IntegrationStubs` are expected (they need a real mpv).
- Single test: `uv run task unit tests.test_schedule.ComputePositionTest.test_basic_position_within_loop`
- **Deploying to a target:** `uv run task deploy-vm` (VM) or `uv run task deploy-box --host H` (box) is the **only** way to change a target. SSH into a target is for read-only inspection and deliberate test actions, not config changes. `uv run task vm-verify` reporting `DRIFT` means someone hand-edited the target: fix the repo and redeploy.
- **Adding a task:**
  - Every host-side script that people run repeatedly gets a task with a `help` string.
  - Tasks that change a target (VM/box) must call the idempotent repo scripts (`provision.sh`, `deploy.sh` → `setup.sh`).
  - Never add a task that runs ad-hoc commands on a target.
- **Host Python always goes through `uv`.** The project pins 3.14 (`.python-version`); the dev (uv), VM and appliance Python versions are all 3.14 and must stay aligned. A bare `python3` may be another version and won't have the editable `rabbitears` install.
- The test framework is stdlib `unittest`, not pytest. Mock `subprocess`/sockets; tests never start real QEMU or mpv.
- `tests/test_vmctl.py` loads `vm/vmctl.py` by file path (it isn't a package). Follow that pattern for other `vm/` or `tools/` scripts.
- No lint/format gate: ruff isn't configured and the tree isn't ruff-clean. **Don't reformat or lint-fix files you aren't otherwise changing.**

## Two runtimes: know which side code runs on

- **Box/VM side** (`src/rabbitears/`, `alpine/setup.sh`, `system/`):
  - Runs on Alpine's system `/usr/bin/python3`: **3.14** in Alpine 3.24, identical to the dev pin. Write stdlib-only code that runs on it.
  - **Stdlib only.** There is no pip/uv on the box. A runtime dependency must be an apk package added to `alpine/packages.txt` (e.g. `py3-evdev`).
  - Shell must be **POSIX sh / busybox ash**, not bash.
- **Host side** (`vm/`, `tools/`): bash is fine. Python runs via `uv run`.
- OpenRC init scripts need the `#!/sbin/openrc-run` shebang, not `#!/bin/sh`. With `#!/bin/sh` the service silently never starts. Use `supervisor=supervise-daemon` with `command`/`command_args`, and don't define `start()`/`stop()`.

## App code standards (`src/rabbitears/`)

- **Never crash on bad input.** Loaders (`config`, `state`, `index`, `channels`) return defaults for missing, corrupt or malformed files, with **per-key** fallback: one bad key doesn't discard the others. Unplayable media is skipped, never fatal.
- **Atomic writes:** temp file, then `fsync`, then `os.replace` (see `state.save_state`, `index.save_index`).
- **The app never writes to `/` at runtime.** State and index live on the USB drive (`<mount>/.rabbitears/`). If the drive is absent, state is kept in memory only.
- `schedule.py` stays pure (no I/O, no clock reads). Position is always recomputed from the wall clock, never advanced with a counter.
- Default paths come from `config.Config` (mount `/media/rabbitears`, mpv socket `/run/rabbitears/mpv.sock`). Don't hardcode them elsewhere.

## Appliance / reproducibility rules

- Targets are built only from the repo (`alpine/setup.sh`, which must stay idempotent). Never hand-edit a box or the VM to make something work; fix the repo and redeploy. SSH into a target is for read-only inspection and deliberate test actions, not config changes.
- On the diskless box, anything needed at boot must live under `/etc` or a path passed to `lbu include`. The VM is a standard install (`LBUOPTS=none`), so it **won't** catch violations of this; check by inspection.
- Validate services with a **real reboot**, not `rc-service restart` (`docs/alpine-install.md` §3).
- The VM uses virtio-gpu, which has **no VAAPI**. It validates DRM output, app logic and OpenRC wiring, not hardware decode; that is checked only on a real 3040. `vm/VAGRANT.md`'s `vainfo`/i965 checklist is wrong on this point.
- Media on the drive (real or mock) must follow SCOPE §5 (MP4, H.264 ≤ L4.1, ≤720p, 8-bit, AAC). Typical source files (VP9/WebM, 1080p, MKV) don't. Transcode them on the host (SCOPE §5 names `tools/transcode.sh`) and check with `ffprobe`; never copy them raw. The VM decodes in software, so non-compliant media "works" there and fails on the 3040.
- **No personal paths in the repo.** Media libraries, home directories and usernames vary per clone: take them as script arguments, or derive them (`$SUDO_USER`, the script's own location). Don't hardcode them in scripts, tests or docs.

## VM dev loop

- Host must be Debian/Ubuntu (`provision.sh` installs qemu/libvirt with `apt`) with `/dev/kvm`.
- **One-time per clone:** `uv run task setup-sudoers` (asks for the password once). It installs NOPASSWD rules in `/etc/sudoers.d/rabbitears` for this checkout's `vm/provision.sh` and `vm/make-usb.sh`, the only scripts that need root. Re-run it after moving the repo. If a root script prompts for a password, this hasn't been run: ask the user to run it.
- **Group membership:** `vm-build` (`provision.sh`) adds you to the `kvm` and `libvirt` groups so `vm-run`/`vm-stop` work without sudo. That only takes effect in **new** terminals — an already-open shell fails `vm-run`'s disk preflight until you log out/in (stopgap: `sg kvm -c "uv run task vm-run"`). Don't `usermod` yourself: it's already done by `vm-build`.
- **The loop:** `uv run task vm-build` (build; leaves the disk at rest) → `uv run task vm-run` (boot in QEMU) → `uv run task deploy-vm` (deploy + reboot + verify) → `uv run task vm-shot` (screenshot).
- **E2E check:** `uv run task vm-e2e` boots the VM with the mock stick, deploys, and checks service, mdev mount, playback, schedule position (±3s), on-screen motion, remote standby/wake + zap + autofire + volume (via the `e2e-remote.py` test double on the button FIFO), unplug → NO SIGNAL, replug → retune, mpv crash → respawn, `verify: OK`, then shuts down and confirms no QEMU still holds the disk. `--rebuild` starts from `vm-rebuild`. Logs, screenshots and `report.json` go to `/tmp/rabbitears-e2e/`. Run it after changes to `vm/`, `alpine/`, `system/` or the app's player/mount paths. It's the proof that the pipeline works, so don't hand-run the steps instead.
- **Mock media drive:** an emulated USB stick, mounted by the *same* mdev rule as the box. `uv run task usb-build` builds the exFAT `RABBITEARS` image from the staging dir; `uv run task vm-run-usb` boots the VM with it attached; `uv run task usb-unplug` / `usb-plug` yank and re-insert it (the pull-the-plug test).
- `uv run task vm-build` builds the KVM VM via libvirt and an unattended `setup-alpine` (`vm/provision.sh` → `vm/install.py`). It leaves the domain shut off so `vmctl.py` can open the disk.
  - Its `pre_` hook starts the apk caching proxy (`vm-proxy`), and `provision.sh` refuses to install if the answerfile's apk repos don't answer. The answerfile points apk at `192.168.122.1:8081/alpine/v3.24/{main,community}`; the public CDN over the VM's NAT is very slow.
  - `uv run task vm-rebuild` = destroy + build, which is the reproducibility check (about 2 min with a warm proxy).
  - Re-running `vm-build` on an already-installed VM is a no-op ("already installed").
  - **Every serial byte of the install goes to `/var/lib/rabbitears-vm/install-serial.log`** (readable without sudo; `uv run task vm-install-log`). On failure, `install.py` prints the last console lines and the reason, then destroys the live VM. `uv run task vm-build --keep-on-failure` leaves the live VM up instead, for `virsh -c qemu:///system console rabbitears-vm`.
  - `install.py` fails fast rather than hanging: on an interactive question on the console (an answerfile option is missing), a setup-alpine section overrunning its budget, 10 min of console silence, or the disk not growing once "Disk & Install" starts.
- **`alpine/answerfile` must set every option setup-alpine consults.** An unset `*OPTS` makes setup-alpine ask on the serial console, and the install hangs before it writes the disk. This is how `PROXYOPTS`/`USEROPTS` broke it. `tests/test_install.py` sources the file and enforces this, including that the apk repos are versioned and match `ALPINE_VERSION`. Re-check it against alpine-conf when bumping Alpine.
- **Never prefix `provision.sh` with `sudo`.** It self-elevates, and the rule above makes that passwordless. The same applies to `make-usb.sh`.
- `provision.sh` subcommands without a task: `console`, `console-cmd`, `diskboot`, `liveboot`, `eject`, `destroy`. (`stop` has a task: `uv run task vm-stop`.)
- `provision.sh` generates `vm/keys/rabbitears-vm` (gitignored). The repo's `alpine/answerfile` holds a `ROOTSSHKEY` placeholder, and `install.py` injects the clone's public key into the copy it sends. The tracked file is never rewritten.
- **Only one QEMU may hold the disk.** `uv run task vm-stop` stops vmctl's QEMU (`vmctl.py stop`: SIGTERM, then SIGKILL, then verify it's gone) and the libvirt domain (`provision.sh stop`, which also verifies). `vmctl.py run`, `provision.sh create/destroy/diskboot/liveboot` refuse while anything holds the disk (`uv run task vm-status` lists holders). vmctl's QEMU gets `PR_SET_PDEATHSIG` and dies with vmctl. Never `pkill -f <pattern>`: it matches the caller's own command line.
- `vm/vmctl.py` (tasks `vm-run`, `vm-serial`, `vm-shot`, `vm-key`, `remote-unplug`, `remote-plug`, `vm-powerbutton`) boots the same qcow2 in a raw QEMU (4 vCPU, 2 GB): SSH on `127.0.0.1:2222`, serial on `:4321`, QMP at `/tmp/rabbitears-qmp.sock`. The disk path defaults to the libvirt image; no `--disk` argument needed.
  - It can't run while another QEMU holds the disk. Preflight checks for this and tells you to run `uv run task vm-stop`.
  - `screenshot` writes `vm/screenshot.png`, which is tracked; don't commit incidental changes to it.
  - **The emulated remote is a QEMU `usb-kbd`** (vendor `0x0627` — the Adomax dongle's ID), attached by default so the app's real vendor scan finds it with no VM branch (`run --no-remote` omits it). `vm-key BUTTON` injects a key via QMP `input-send-event`; consumer-page keys (`back`/`ac_back`, possibly the volume keys and `power`) may not arrive through the boot keyboard — check the app log and fall back to `rabbitears.ctl` over SSH for anything that doesn't.
  - **`rabbitears.ctl` is the sanctioned SSH test action for driving the app** on a target: `PYTHONPATH=/opt python3 -m rabbitears.ctl status` (or any `Command` value). It hits the root-only control socket at `/run/rabbitears/control.sock`; there is deliberately no taskipy task that runs it on a target.
- For any other root command, ask the user. Don't add sudoers rules, setuid bits, or disk-copy workarounds. A new script that needs root goes in `SCRIPTS` in `vm/setup-sudoers.sh`, and the user re-runs it.

## Docs and workflow

- `docs/prompts/*.md` are task prompts the user hands to agents. When executing one, read every file it lists first, satisfy its completion criteria, and answer its "Report back with" section. If it changes commands or the dev loop, update this file.
- `REPORT.md` is the verified remote dossier (button map, USB ID `0627:697d`). It still says "Zapbox"/Arch; SCOPE refers to this content as `docs/remote.md`, which doesn't exist yet.
- **Never commit or push unless the user asks.** Existing commit style is imperative and short, sometimes tagged with a milestone, e.g. `Add config.py + state.py with unit tests (M2 core)`.
