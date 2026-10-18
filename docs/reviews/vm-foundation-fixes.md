# Review: uncommitted work for `docs/prompts/vm-foundation-fixes.md`

Date: 2026-10-18. Scope: every uncommitted change in the working tree, checked
against the prompt, `AGENTS.md` and `SCOPE.md`.

## Summary

Every Part of the prompt has been attempted, but several bugs mean the
completion criteria can't be met.

- `uv run task test`: 123 tests, OK, 4 expected skips.
- `bash -n` passes on all new/changed bash scripts; `dash -n alpine/setup.sh`
  passes (POSIX syntax).
- `shellcheck` is not installed on the host, so it was not run.
- The libvirt domain was still running at review time. That fact, plus the
  bugs below, suggests the prompt's end-to-end validation (steps 3-8) was never
  run.

## Blockers (each breaks a completion criterion)

1. **`setup.sh` will never print `0 changed`** (`alpine/setup.sh:54`).
   `stat -c '%a'` returns `644`, but the mode it's compared against is `0644`
   (confirmed on the host). The mode check always fails, so every run
   reinstalls `mpv.conf`, the init script and the test pattern ("3 changed").
   Fix: compare against the mode with its leading zeros stripped, or format
   both sides the same way.

2. **The lbu path format probably doesn't match** (`alpine/setup.sh:182`,
   `alpine/setup.sh:201`, `alpine/verify.sh:158`). I believe `lbu include`
   stores paths without the leading `/` (alpine-conf's `listadd` strips it), so
   `lbu include -l` prints `opt/rabbitears`. **Not confirmed on the VM**:
   running `lbu include -l` there will settle it. If this is right:
   - `grep -qx /opt/rabbitears` never matches, so `lbu include` re-runs every
     time (harmless, because lbu de-duplicates, but noisy).
   - The coverage check prints a WARNING on every run.
   - `verify.sh` always reports `LBU … not in lbu include list`, so it can
     never say `verify: OK`.

   Fix: normalise both sides (strip or add the leading `/`) before comparing.

3. **`tools/transcode.sh` can't produce any file.** I tested it on synthetic
   clips:
   - **No output format** (`tools/transcode.sh:144`): ffmpeg writes to
     `*.mp4.part` and can't guess the format from that extension ("Unable to
     choose an output format"). Add `-f mp4`.
   - **`set -e` kills the script on `wait`** (`tools/transcode.sh:195`,
     `tools/transcode.sh:214`): `wait` returns the job's exit code, so a
     skipped file (rc 10) or a failed one exits the script before the summary.
     I confirmed this, so a second run that should skip everything dies on the
     first file. Use `rc=0; wait "$pid" || rc=$?`.
   - With both fixed (tested on a patched copy), a 1080p VP9/Opus `.webm`
     became H.264 High, level 4.1, 1280×720, yuv420p, AAC 48 kHz stereo; a
     640×360 H.264 file stayed 640×360; `--check` passed (0 violations); and
     the re-run reported `0 transcoded, 2 skipped, 0 failed`.

4. **The orphan-mpv guard never takes effect on Alpine**
   (`src/rabbitears/player.py:48`). The code loads `ctypes.CDLL("libc.so.6")`,
   but musl has no `libc.so.6`. The `OSError` is suppressed, so
   `PR_SET_PDEATHSIG` is silently never set on the box. Fixes:
   - Use `ctypes.CDLL(None, use_errno=True)`, which works on glibc and musl.
   - Load libc once at import time, not inside `preexec_fn`. Calling
     `dlopen` between fork and exec can deadlock in a multithreaded parent.
   - For the next prompt: `PDEATHSIG` fires when the forking *thread* exits,
     not the process. Spawn mpv from the main thread.

5. **Re-running `vm-build` isn't a no-op** (`vm/provision.sh:417`).
   `pkill -f "guest=${VM_NAME}"` runs before the "already installed" check
   (`vm/provision.sh:477-482`), so re-running `vm-build` while the domain is
   running hard-kills it. Move the "already installed" check above the pkill
   and the domain definition.

## Real bugs

- **`deploy.sh --reboot` races the shutdown** (`alpine/deploy.sh:153-167`).
  - After `reboot` it sleeps 5s and then waits for SSH. OpenRC shutdown can
    take longer, so the old system may still be answering and the
    status/verify checks can hit the dying system.
  - It never polls for `rabbitears` to report "started" (the prompt requires
    it); it logs the status once and moves on.
  - Fix: wait for SSH to go down, or compare
    `/proc/sys/kernel/random/boot_id` before and after, then poll
    `rc-service rabbitears status` until it reports started or times out.
- **The diskless guard runs too late and fails open**
  (`alpine/deploy.sh:130-135`, `alpine/deploy.sh:154`).
  - It's checked inside `do_reboot`, after `setup.sh` has already changed the
    target. It should refuse before doing anything.
  - If the SSH call fails, `check_diskless` returns "not diskless" and the
    reboot goes ahead. It should fail closed.
  - `awk '$2=="/"'` prints every matching line; if there are two (e.g.
    `rootfs` and `tmpfs`) the comparison fails. Take the last match.
  - There's no unit test or dry-run for it (validation step 8).
- **No `-o BatchMode=yes` in deploy/verify SSH** (`alpine/deploy.sh:83-92`,
  `alpine/verify.sh:67-76`). A failed key auth or an unknown host key on a
  real box can hang on a prompt that's hidden by `2>/dev/null` in
  `wait_for_ssh`.
- **vmctl preflight is easy to bypass** (`vm/vmctl.py:337-351`). If `virsh`
  fails (e.g. permission denied because the user's libvirt group membership
  isn't active yet, which is the case in the current session), the
  domain-running check is silently skipped. A `pgrep -f guest=rabbitears-vm`
  fallback works without privileges.
- **The manifest write isn't atomic** (`alpine/setup.sh:216-219`). `mktemp`
  creates the file in `/tmp`, which may be a different filesystem from `/etc`,
  so `mv` becomes copy + delete. The file is also briefly `0600` before the
  `chmod`. Create it with `mktemp /etc/rabbitears/.manifest.XXXXXX` and chmod
  it before the `mv`.
- **`provision.sh stop` on an undefined domain** (`vm/provision.sh:283-302`)
  waits 60s, runs `virsh destroy`, and then claims "shut off". Check that the
  domain exists first.

## Spec gaps

- **SCOPE §3 (Part 4):** the sentence recording `/var/log/rabbitears.log` as
  an intended exception to "the app never writes to `/`" is missing. It's only
  in the init script comment and the §6 listing.
- **App tree (Part 3), `alpine/setup.sh:109-159`:**
  - It doesn't go through `install_file`, so file modes aren't checked.
  - It only looks at top-level files (`"$APP_SRC"/*` with `[ -f ]`), so
    subpackages are ignored by the "identical" check and the manifest
    (`cp -r` still copies them).
  - The whole swap counts as `1 changed` regardless of how many files changed.
- **lbu coverage check** (`alpine/setup.sh:194-209`): it checks two hardcoded
  directories instead of every manifest path outside `/etc`, and doesn't print
  the paths. A future `install_file` to, say, `/usr/local/bin` would not be
  caught.
- **IPC timeout** (`src/rabbitears/player.py:130`): `settimeout` applies per
  `recv`, not to the whole exchange as the prompt asks. A steady stream of
  events could extend it indefinitely. Use a deadline and adjust the timeout
  before each read.
- **`--commit` on the VM** (`alpine/deploy.sh:143`): it logs a note; the
  prompt asks for a warning.
- **STALE detection** (`alpine/verify.sh:98-111`): it only checks files
  already in the manifest. A file added to `src/rabbitears/` since the last
  deploy isn't reported. The prompt doesn't strictly require this, but the
  check is weaker than it looks.
- **AGENTS.md:**
  - It lists `stop` as a `provision.sh` subcommand without a task, but
    `vm-stop` exists.
  - It says preflight suggests `uv run task vm-stop`; the code
    (`vm/vmctl.py:346`) says `./vm/provision.sh stop`. Make them agree.
- **vmctl module docstring** (`vm/vmctl.py:1-30`): it still shows
  `run --disk DISK.qcow2` and calls the VM "throwaway"; `-smp 4` and the
  default disk aren't mentioned.
- **Validation not done:** steps 3-8 (fresh rebuild, double deploy, drift
  test, preflight refusal, respawn, transcode, diskless guard) have no
  evidence and would currently fail on blockers 1-3.

## Problems for the next prompt (`first-channel-vm-playback.md`)

- **Python will write `__pycache__` into `/opt/rabbitears`** once `command`
  becomes `python3 -m rabbitears` (it runs as root with `PYTHONPATH=/opt`).
  That breaks "the app never writes to `/`" and makes `verify.sh` report
  UNMANAGED files. Add `--env PYTHONDONTWRITEBYTECODE=1` to
  `supervise_daemon_args` in `system/openrc/rabbitears`.
- **`get_durations` will spam the log** (`src/rabbitears/index.py:110-121`).
  It warns about every non-`.mp4` file on each call, and it's called on every
  tune or EOF, into a RAM-backed log with no rotation.
- **`.MP4` files are now accepted.** The suffix check is case-insensitive,
  a small behaviour change from the old `glob("*.mp4")`. Probably fine, but
  the prompt said "behaviour otherwise unchanged".
- **`setup.sh` uses `apk add --no-cache`** (`alpine/setup.sh:83`). This was
  already there before this change, but on the diskless box it keeps packages
  out of the apk cache, so offline package reinstall at boot (SCOPE §3) would
  fail.
- **`transcode.sh` gaps:**
  - It rejects the "Constrained Baseline" profile (`tools/transcode.sh:51-54`).
  - `a.mkv` and `a.mp4` in the same source folder both become `a.mp4`, and in
    parallel mode they race.
  - `-map 0:V:0` would be safer than `-map v:0`, because it skips attached
    cover-art streams.
  - `-j` isn't validated as a positive integer.
- **`player._send`** doesn't catch `UnicodeDecodeError` from `readline()`.
  It's a `ValueError`, so it escapes as a non-`PlayerError` exception.

## What looks correct

- Removing the guest-agent channel from the domain XML, and deleting
  `vm/export-disk.sh` and `vm/setup-export-sudoers.sh`. `SCRIPTS` in
  `vm/setup-sudoers.sh` is accurate.
- The "already installed" detection itself (no `<kernel>`, has
  `<boot dev='hd'/>`) matches what `vm/install.py`'s `switch_to_disk_boot`
  produces. One caveat: after `provision.sh liveboot` the domain has a
  `<kernel>` again, so a later `vm-build` would reinstall.
- `-smp 4`, `DEFAULT_DISK`, the injectable preflight and its tests.
- The init script's logging, `respawn_max=0`, `respawn_delay=2`,
  `need localmount` and `after bootmisc`.
- `request_id` matching and event skipping in `player._send`; `get_property`,
  `is_idle`, `is_running`, `show_image`; stale-socket removal;
  `--image-display-duration=inf`; and the fake-mpv tests.
- `config.testpattern_path` and its tests.
- The new taskipy tasks and their help strings.
- Most of the doc updates (`SCOPE.md` §6/§7/§10, `docs/alpine-install.md`,
  `vm/VAGRANT.md` VAAPI correction).
