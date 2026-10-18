# AGENTS.md — Rabbit Ears

Kid-safe "cable box" for a Dell Wyse 3040: Alpine (diskless + lbu), OpenRC, one long-running mpv driven by a small Python app. **`SCOPE.md` is the source of truth.** Anything not in it is out of scope, including Kodi, X11/Wayland, streaming, on-demand browsing and number-key entry. When a design decision changes, update `SCOPE.md` (§6 layout, §10 decisions log) in the same change.

## Commands (run from the repo root)

- Common actions are **taskipy** tasks (dev dependency, `[tool.taskipy.tasks]` in `pyproject.toml`). Run `uv run task --list` to see them.
  - Tasks run from the repo root (`cwd = "."`).
  - Extra args are appended to the task's main `cmd` only, never to its `pre_`/`post_` hooks.
- Tests: `uv run task test`. 4 skips in `test_player.IntegrationStubs` are expected (they need a real mpv).
- Single test: `uv run task unit tests.test_schedule.ComputePositionTest.test_basic_position_within_loop`
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
- `uv run task vm-build` builds the KVM VM via libvirt and an unattended `setup-alpine` (`vm/provision.sh` → `vm/install.py`).
  - Its `pre_` hook starts the apk caching proxy (`vm-proxy`). The answerfile points apk at `192.168.122.1:8081`; the public CDN over the VM's NAT is very slow.
  - `uv run task vm-rebuild` = destroy + build, which is the reproducibility check.
- **Never prefix `provision.sh` with `sudo`.** It self-elevates, and the rule above makes that passwordless. The same applies to `make-usb.sh`.
- `provision.sh` subcommands without a task: `console`, `console-cmd`, `diskboot`, `liveboot`, `eject`, `destroy`.
- `provision.sh` generates `vm/keys/rabbitears-vm` (gitignored) and rewrites `ROOTSSHKEY` in `alpine/answerfile`, so a diff there after provisioning is expected.
- `vm/vmctl.py` (tasks `vm-run`, `vm-serial`, `vm-shot`) boots the same qcow2 in a raw QEMU: SSH on `127.0.0.1:2222`, serial on `:4321`, QMP at `/tmp/rabbitears-qmp.sock`.
  - It can't run while the libvirt domain holds the disk.
  - `screenshot` writes `vm/screenshot.png`, which is tracked; don't commit incidental changes to it.
- For any other root command, ask the user. Don't add sudoers rules, setuid bits, or disk-copy workarounds. A new script that needs root goes in `SCRIPTS` in `vm/setup-sudoers.sh`, and the user re-runs it.

## Docs and workflow

- `docs/prompts/*.md` are task prompts the user hands to agents. When executing one, read every file it lists first, satisfy its completion criteria, and answer its "Report back with" section. If it changes commands or the dev loop, update this file.
- `REPORT.md` is the verified remote dossier (button map, USB ID `0627:697d`). It still says "Zapbox"/Arch; SCOPE refers to this content as `docs/remote.md`, which doesn't exist yet.
- **Never commit or push unless the user asks.** Existing commit style is imperative and short, sometimes tagged with a milestone, e.g. `Add config.py + state.py with unit tests (M2 core)`.
