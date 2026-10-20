# E2E validation blockers: KVM VM base install

Status: **resolved (2026-10-19).** `uv run task vm-e2e --rebuild` passes
from scratch: build, deploy, mount, schedule-correct playback, pull/re-insert,
crash recovery and drift check, 15/15 checks. This file keeps the original
findings and records what each one turned out to be and how it was fixed.

## Root causes

The "setup-alpine hangs, disk never grows" behaviour (original Issue 2) had
two causes. The serial transcript from the new `install.py` showed both
within one run.

1. **The answerfile left options unset, so setup-alpine asked questions on
   the serial console.** alpine-conf 3.22's `setup-alpine` calls
   `setup-proxy -q ${PROXYOPTS}` and `setup-user ${USEROPTS:-...}`. With
   `PROXYOPTS` unset it asks *"HTTP/FTP proxy URL?"*, and with `USEROPTS`
   unset it asks *"Setup a user?"*. Both happen before "Disk & Install", which
   is why the disk stayed at 193K. The old `install.py` sent `"\r\n"` (two
   Enters) per line. The spare Enter sometimes answered the first question
   with its default, so the install passed or hung by luck. That explains why
   one attempt reached `base install finished` and the clean ones didn't.
2. **`APKREPOSOPTS` was an unversioned URL** (`http://192.168.122.1:8081/alpine`).
   `setup-apkrepos` writes it verbatim, so apk fetched
   `/alpine/x86_64/APKINDEX.tar.gz`. These were the 502s in the proxy log,
   which the report had dismissed as harmless. With no package index, setup
   steps that `apk add` (tzdata, openssh, the base system) couldn't work.

There was also a third problem: `install.py` treated any `~# ` in the buffer
as "command finished", including a stale prompt and the echoed command line.
It could run ahead of the guest.

After the fixes the base install takes **~45 s** with a warm proxy.

## Original issues → fixes

| # | Issue (as reported) | Resolution |
|---|---|---|
| 1 | Stale raw QEMU held the disk; `vm-stop` didn't verify | `vmctl.py stop` (SIGTERM → SIGKILL → verify) runs in `vm-stop` before `provision.sh stop`, which now also verifies the libvirt QEMU is gone. vmctl's QEMU gets `PR_SET_PDEATHSIG` (verified: dies ~1 s after vmctl is SIGKILLed) and SIGTERM/SIGHUP handlers. `vmctl.py run`, `provision.sh create/destroy/diskboot/liveboot` and `vm-e2e` refuse while *any* QEMU holds the disk and name the PID. `vm-status` lists holders. |
| 2 | setup-alpine stuck, cause unknown | Root causes above. `alpine/answerfile` now sets `PROXYOPTS=none`, `USEROPTS=none` and versioned `main` + `community` repos. `tests/test_install.py` sources the answerfile with `/bin/sh` and fails on any unset option, an unversioned repo, a repo version ≠ `ALPINE_VERSION`, or a non-exported `ERASE_DISKS`. |
| 3 | No console access / no transcript | Every serial byte goes to `/var/lib/rabbitears-vm/install-serial.log` (0644; `uv run task vm-install-log`). On failure the last 30 console lines are printed. `vm-build --keep-on-failure` leaves the live VM up for `virsh -c qemu:///system console rabbitears-vm`. |
| — | `pkill -f` self-match | All `pkill -f "guest=…"` calls are gone. libvirt QEMU is matched by `guest=<vm>,` and disk holders via `fuser` / `/proc` scans that skip the caller. AGENTS.md says never to `pkill -f`. |

The six "what would help" items, all done:

1. Serial transcript: see #3.
2. Console access: `--keep-on-failure` + `virsh console`, documented.
3. Fail fast on a non-growing disk: `DiskGrowthCheck` (5 min after "Disk & Install" starts).
4. `vm-stop` verifies: see #1.
5. Build preflight for a locked disk, plus a check that the apk repos answer before booting anything.
6. Phase-aware timeouts: setup-alpine's section headings are tracked with a
   per-section budget. Also an interactive-question detector (fails after 20 s
   on a prompt, naming the question and pointing at the answerfile) and a
   10-minute console-silence watchdog.

## Other fixes found along the way

- **`install.py` reliability:** commands are confirmed by an `RB:<status>:<token>`
  sentinel that the echoed command line can't match. Stale output is drained
  before each command, and one Enter is sent per line. The answerfile's sha256
  is checked on the guest. The busybox prompt's trailing `ESC[6n` is handled.
  The live system is powered off before switching to disk boot. The SSH check
  confirms it's the *installed* system (hostname, disk-backed root), and the
  VM is powered off over SSH at the end so the disk is left at rest.
- **The repo's answerfile is no longer rewritten.** `provision.sh` used to
  `sed > mktemp; mv` the user's SSH key into `alpine/answerfile` as root,
  leaving it `root:root 0600` (unreadable to git). It now holds a placeholder,
  and `install.py` injects the clone's key into the copy it sends.
- **App bug found by the first e2e run:** `Player.load` sent
  `loadfile <path> replace {"start": <float>}`. mpv 0.41 (Alpine 3.24) rejects
  that with *invalid parameter*, so the app crash-looped and never played.
  mpv ≥ 0.38 wants `loadfile <path> replace -1 {"start": "<str>"}`. Fixed and
  covered by `tests/test_player.py::LoadTest`.

## Verifying the pipeline

- `uv run task test`: unit tests, including `test_install.py` (fake live
  system on a scripted serial console: happy path, hang-at-question,
  corrupted transfer, watchdogs), `test_vmctl.py` (disk holders, stop,
  PDEATHSIG) and `test_e2e.py` (schedule math, frame diff, gating).
- `uv run task vm-e2e [--rebuild]`: the real thing (see `vm/VAGRANT.md`).
  Evidence from the passing run: schedule diff −0.4 s / −0.0 s after re-insert,
  unplug → NO SIGNAL in 0.2 s, replug → playing in 5.0 s, mpv respawned in
  ~3 s, `verify: OK`, no QEMU left on the disk.
