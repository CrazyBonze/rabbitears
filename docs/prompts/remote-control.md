# Agent prompt: remote control (M3). Command actions + input sources, standby, power-key override

> **Status (2026-10-20): implemented, with one architectural deviation.**
> The two-layer idea below (a `Command` vocabulary + swappable input sources) is
> exactly what shipped: `commands.py` (`Command`, `parse_command`), `App.handle`,
> and the `control.py`/`ctl.py` control socket are all in place. The one
> difference is the **transport** for the physical remote: instead of `remote.py`
> being an evdev `InputSource` in a single-threaded `select` loop, the evdev
> reading lives in a separate daemon (`evdevd.py`) that writes one line per
> button to a FIFO (`/run/rabbitears/buttons`); the app reads the FIFO and maps
> buttons to `Command`s via `remote.Remote`. This keeps the app single-threaded
> (the mpv `PR_SET_PDEATHSIG` constraint) and every action testable without a
> physical remote. The rest of this prompt (the Part 1b table, the config keys,
> the control socket, Part 3 acpid override, Part 4 vmctl remote, Part 5 tasks)
> was followed as written; where it says "InputSource/select loop", read
> "FIFO reader + `Remote`". See `SCOPE.md` §4.4/§10 for the recorded design.

**Goal: the Adomax remote drives the box: channel up/down with gaps, wrap and hold-to-repeat, volume with a cap, mute, and power as standby/wake. The power key must never shut the box down. The work is built as two layers so every action can be tested without hardware:**

1. **Actions:** a fixed `Command` vocabulary and `App.handle(command)`, which does the work. It knows nothing about evdev, sockets or key codes.
2. **Input sources:** turn the outside world into `Command`s.
   - `remote.py` reads the evdev remote.
   - `control.py` serves a local control socket used for tests and for scripted checks over SSH.

   A future remote, or the parking-lot web UI, is just another source.

**Prerequisite:** `docs/prompts/first-channel-vm-playback.md` must be complete. This prompt relies on what it delivered:
- `src/rabbitears/app.py` with `App` (injected player, clock, config, `media_present`), the NO_SIGNAL/PLAYING states, `tune_target`, `select_channel`, and `step()` called every 100 ms. `__main__.py` is a thin entry point.
- The init script runs `python3 -m rabbitears` under supervise-daemon.
- The mdev mount of the `RABBITEARS` stick, plus `vm/make-usb.sh` and the tasks `usb-build`, `vm-run-usb`, `usb-unplug` and `usb-plug`.
- `vmctl.py run --usb IMG` (with `qemu-xhci`), `vmctl.py usb {plug,unplug}`, and `_qmp_exchange` skipping async QMP events.

If any of these is missing, stop and report. Don't re-implement them. Where the real `app.py` differs in detail from what this prompt assumes, adapt to the existing code rather than rewriting it, and report the differences.

Work from the repository root (paths below are repo-relative). Read these in full before making changes: `AGENTS.md`, `SCOPE.md` (especially §4.4, §4.6, §3 "Power button"), `docs/remote.md` (the verified button map), `docs/prompts/first-channel-vm-playback.md`, `vm/VAGRANT.md`, `pyproject.toml` (`[tool.taskipy]`), `vm/vmctl.py`, `alpine/setup.sh`, `alpine/verify.sh`, `alpine/packages.txt`, `system/openrc/rabbitears`, everything in `src/rabbitears/`, and `tests/test_app.py` and `tests/test_vmctl.py`.

---

## The paradigm (unchanged; every change must respect it)

The rules in `first-channel-vm-playback.md` ("The paradigm" and "Host-side conventions") still apply in full:
- the repo is the only source of truth
- scripted and idempotent
- no VM-vs-box branches
- the VM mimics the appliance
- validation is a real reboot
- boot-time files are under `/etc` or lbu
- host Python goes through `uv`
- tasks for every common action
- no personal paths

Additional rules for this task:

1. **Strict layering.**
   - `App.handle()` takes only `Command` values.
   - Input sources import `commands.py` and nothing from `app.py`.
   - Key codes appear only in `remote.py`.
   - No layer reaches around another.
2. **Single-threaded.** One main-thread loop multiplexes every input source with `select`. No threads anywhere: `PR_SET_PDEATHSIG` fires when the *forking thread* exits, so mpv must be spawned from, and stay tied to, the main thread (see `player.py`).
3. **The control surface is closed.** Neither source can do anything that isn't a `Command`, plus a read-only `status`. In particular there's no "load this path", no shell, and no network listener. Kid-proof by construction (SCOPE §1 goal 3).
4. **Never crash on input.** Unknown keys, a vanished device, a garbage line on the control socket, or a client that connects and goes silent are all logged and ignored. A remote that isn't plugged in is a normal state, not an error.
5. **Box side is stdlib + apk only.** `py3-evdev` is already in `alpine/packages.txt`. It isn't installed in the host uv env, and it must not become a host dependency: **`import rabbitears.remote` must succeed without evdev installed.** Import evdev lazily inside the device-opening code, and inject a device factory in tests.

---

## Part 1: Actions

### 1a. `src/rabbitears/commands.py`: the shared contract

```python
class Command(enum.Enum):
    CHANNEL_UP = "channel_up"
    CHANNEL_DOWN = "channel_down"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    MUTE = "mute"        # toggle
    POWER = "power"      # toggle standby
    INFO = "info"        # toggle info overlay (M4)
    BACK = "back"        # dismiss overlay (M4)
```

- Add `parse_command(text: str) -> Command | None`. It strips whitespace, is case-insensitive, and returns `None` for anything unknown. It never raises.
- Commands are *semantic* (what happens), not button names. Mapping buttons to commands is an input source's job.
- No other imports from the app, so any source can depend on it.

### 1b. `App.handle(command: Command) -> None` in `src/rabbitears/app.py`

Add a `standby: bool` flag **orthogonal** to the NO_SIGNAL/PLAYING state, plus `muted: bool` (session only; not persisted). Behaviour:

| Command | NO_SIGNAL | PLAYING | Standby (either state) |
|---|---|---|---|
| `CHANNEL_UP` / `CHANNEL_DOWN` | ignored (log) | `channel_up` / `channel_down` from `channels.py` (gaps and wrap already handled), then tune | ignored |
| `VOLUME_UP` / `VOLUME_DOWN` | applied | applied | ignored |
| `MUTE` | toggled | toggled | ignored |
| `POWER` | enter standby | enter standby | wake |
| `INFO` / `BACK` | logged no-op ("overlay is M4") | logged no-op | ignored |

**Channel change**
- Index the target with `get_durations`. If it has **no playable files**, keep stepping in the same direction, up to `len(channels)` attempts, so a dead channel is skipped rather than shown as a black screen.
- If no channel is playable, go to NO_SIGNAL.
- Tune at the *current* clock position (SCOPE §4.2), save state, and log in the existing tune format.

**Volume**
- Step by `config.volume_step`, clamped to `0..config.volume_cap`. `player.set_volume`.
- `VOLUME_UP`/`DOWN` while muted also unmutes, like a real TV.
- Volume persists in `state.json`. Mark state dirty and let `step()` flush it **at most once every 2 s**, so a burst of presses doesn't mean a burst of fsyncs to the stick.

**Mute:** `player.set_mute(muted)`.

**Standby (SCOPE §4.6)**
- *Enter:* `player.hide_video()` + `player.set_mute(True)`. mpv keeps running.
- *Wake:*
  - `player.show_video()`
  - restore the pre-standby mute state
  - if PLAYING, **re-tune at the current schedule position** (time kept passing while "off")
  - if NO_SIGNAL, `show_image(testpattern)`
- Existing `step()` logic (EOF retune, drive pulled, re-insert) keeps running during standby. Verify in the VM (Part 7) that video stays hidden across an EOF retune and a drive pull while in standby. If `vid=no` doesn't survive a `loadfile` in this mpv version, re-apply `hide_video()` after every load while in standby, and say which you needed.

**Every `handle()` call logs** the command, its source (passed in by the loop for logging only), and the outcome, e.g.:
- `[rabbitears] remote: channel_up -> CH 03 · Magic School Bus`
- `[rabbitears] control: volume_up -> 45 (cap 80)`
- `[rabbitears] remote: channel_up ignored (standby)`

**`App.status() -> dict`** returns `state` (`"no_signal"`/`"playing"`), `standby`, `channel` (number or `null`), `channel_name`, `file` (basename or `null`), `volume` and `muted`. It reads only in-memory app state, with no IPC to mpv.

### 1c. Config (`src/rabbitears/config.py`)

Add these keys with per-key fallback, following the existing validation pattern:

| Key | Type | Default | Notes |
|---|---|---|---|
| `volume_step` | int | `5` | invalid if < 1 |
| `repeat_delay` | float | `0.5` | seconds before hold-repeat starts |
| `repeat_interval` | float | `0.4` | seconds between repeats; invalid if < 0.1 |
| `remote_vendor` | int | `0x0627` | USB vendor ID scanned for (SCOPE §4.4) |
| `control_socket` | Path | `/run/rabbitears/control.sock` | |

Defaults belong in `Config` only. Don't hardcode them in other modules (AGENTS.md).

---

## Part 2: Input sources

### 2a. The loop contract

Each source implements:

```python
class InputSource(Protocol):
    name: str                                   # "remote", "control" (for logs)
    def filenos(self) -> list[int]: ...         # fds to select() on right now
    def on_readable(self, fd: int) -> list[Command]: ...
    def tick(self) -> list[Command]: ...        # timers: hold-repeat, rescans, timeouts
    def next_deadline(self) -> float | None: ...  # monotonic time tick() next needs to run
    def close(self) -> None: ...
```

The main loop in `app.py`:
1. Calls `select()` on all sources' fds, with timeout `min(0.1, earliest next_deadline - now)`, never negative.
2. Dispatches readable fds and calls every source's `tick()`.
3. Passes each returned `Command` to `App.handle`, then calls `App.step()`.

`step()` still runs at least every 100 ms. Sources take their clocks by injection (`monotonic`, `wall`) so tests control time.

### 2b. `src/rabbitears/remote.py`: evdev remote

**Keymap.** Use numeric evdev codes as named module constants, so tests don't need evdev. Use `docs/remote.md` §2:

| Code | Command |
|---|---|
| `KEY_UP` 103 | `CHANNEL_UP` |
| `KEY_DOWN` 108 | `CHANNEL_DOWN` |
| `KEY_ENTER` 28 | `INFO` |
| `KEY_BACK` 158 | `BACK` |
| `KEY_VOLUMEUP` 115 | `VOLUME_UP` |
| `KEY_VOLUMEDOWN` 114 | `VOLUME_DOWN` |
| `KEY_MUTE` 113 | `MUTE` |
| `KEY_POWER` 116 | `POWER` |

Every other key (left, right, home, menu, sun, screen-arrow, mouse buttons) is ignored and logged at DEBUG (SCOPE §4.4: "All others: ignored in MVP").

**`KeyRepeater`** is pure: no I/O, no clock reads, so it's unit-testable like `schedule.py`.
- `press(code, now) -> list[Command]`, `release(code, now)`, `poll(now) -> list[Command]`, `next_deadline() -> float | None`, `clear()`.
- **A mapped press emits its command immediately.**
- **Only `CHANNEL_UP`/`CHANNEL_DOWN` repeat** (SCOPE §4.4). While held, emit again after `repeat_delay`, then every `repeat_interval`, until release. Don't emit a burst to catch up if `poll` runs late: at most one repeat per poll.
- If a second repeatable key is pressed while one is held, the newer one takes over (TV behaviour).
- `value == 2` (kernel autorepeat) is ignored. The remote sends clean press/release pairs (docs/remote.md §3), and the app owns repeat.

**`RemoteInput(InputSource)`**
- **Discovery.**
  - `list_devices` and `open_device` are injectable. The defaults import evdev lazily.
  - Open every `/dev/input/event*` whose `info.vendor == config.remote_vendor`. The dongle splits its buttons across 3–4 interfaces, so open all of them.
  - Log each one opened (path, name, vendor:product).
- **Grab.** `grab()` each device exclusively, so keys reach neither the VT console nor `acpid`. If the grab fails, log a WARNING and keep reading.
- **Read.** Read non-blocking. Use only `EV_KEY` events (`type == 1`).
- **Stale presses.** Drop a press whose event timestamp is more than 1.0 s older than `wall()` when processed (future timestamps are accepted). This stops a kid mashing ↑ during a slow, uncached index from zapping ten channels once the loop catches up. Log the drop count.
- **Hot-plug.**
  - If a read raises `OSError` (device gone), close and drop that device, `KeyRepeater.clear()` (a held key whose release we'll never see must stop repeating), and log it.
  - `tick()` rescans every 3 s and opens any new matching device. No remote at all is normal: log "no remote found" once per transition, not every scan.
- `close()` ungrabs and closes everything.

### 2c. `src/rabbitears/control.py`: local control socket

- **Socket.** A Unix stream socket at `config.control_socket`, created with mode `0600` (root only). Remove a stale socket file at startup. `/run` is tmpfs, so this isn't a write to `/` in the SCOPE §3 sense: the mpv socket already lives there.
- **Protocol.** One request per connection: a single line of at most 64 bytes.
  - A `Command` value (`channel_up`, …) → `handle` it, reply `ok\n`.
  - `status` → reply with one line of JSON from `App.status()`. This is the one non-`Command` request: read-only, and dispatched to `App` through a callback the loop passes in. `control.py` still never imports `app.py`.
  - Anything else → `error: unknown command\n`.
  - Then close.
- **Never block the loop.** Use non-blocking accept and read. A client that sends nothing within 1 s, or more than 64 bytes without a newline, is dropped (enforce this in `tick()`).
- **`src/rabbitears/ctl.py`** is a tiny client: `python3 -m rabbitears.ctl <request>`. It prints the reply, exits 0 on `ok`/status, 1 on error or no socket. It's for SSH test actions on a target (`PYTHONPATH=/opt python3 -m rabbitears.ctl status`). **Don't** add a taskipy task that runs it on a target (AGENTS.md: no ad-hoc target tasks).

---

## Part 3: Power key must never shut the box down (SCOPE §3)

The app's exclusive grab covers the remote's `KEY_POWER` while the app runs. It doesn't cover boot, the 2 s respawn window, or the physical/ACPI power button.

1. **Inspect the VM read-only first** and record what you find:
   - `rc-status`
   - whether `acpid` (busybox or package) runs, and its config dir (`ls -R /etc/acpi`)
   - which handler a power event triggers

   busybox `acpid` reads `/dev/input/event*` as well as ACPI, so an ungrabbed USB `KEY_POWER` can reach it.
2. **Decision (record it in SCOPE §10):** neither the remote's `KEY_POWER` nor the ACPI power button shuts the box down; both become a logged no-op at the OS level. The app handles the remote's key as standby. Recovery from anything is pulling the plug, which the diskless design is built for (SCOPE §1 goal 2).
3. Implement this through `alpine/setup.sh` only:
   - files under `system/acpid/` (already reserved in SCOPE §6), installed with `install_file` under `/etc` so lbu persists them and the manifest and `verify.sh` cover them
   - resolve the two `acpid` TODOs in `setup.sh`

   Use `replace_managed_block` (from the first-channel prompt) if you need to edit a stock file rather than own a whole file. Each handler logs with `logger -t rabbitears-power`.
4. Note in `vm/VAGRANT.md` that, because ACPI shutdown is now ignored, `uv run task vm-stop` against a domain with the app deployed falls back to `virsh destroy` after its timeout. That's expected.

---

## Part 4: `vm/vmctl.py`: an emulated remote

**Why `usb-kbd` works:** QEMU's emulated USB HID devices report vendor ID **`0x0627`**, the same ID the Adomax dongle uses (`docs/remote.md` §1). That makes a QEMU `usb-kbd` a faithful stand-in: the app's real vendor scan finds it with no VM-specific branch. **Verify this inside the guest** (`cat /sys/class/input/event*/device/id/vendor`). If it doesn't hold, stop and report. Don't add a VM-only config override.

### 4a. `run --remote / --no-remote` (default: remote attached)

- Add `remote: bool = True` to `qemu_args()` and `Vm`.
- When set, add `-device usb-kbd,bus=xhci.0,id=remote0`.
- Refactor so `-device qemu-xhci,id=xhci` is emitted **exactly once** whenever the stick *or* the remote needs it, and not at all if neither does.
- The default VM therefore mimics a box with its dongle plugged in. Update the exact-flags tests.

### 4b. `vmctl.py key BUTTON [--hold SECONDS] [--monitor SOCK]`

Press a remote button through QMP `input-send-event` with `"device": "remote0"`: key down, sleep `--hold` (default 0.1), key up. Button names are the remote's, mapped to QEMU qcodes:

| Button | qcode | Expected evdev code |
|---|---|---|
| `up` / `down` | `up` / `down` | 103 / 108 |
| `ok` | `ret` | 28 |
| `back` | `ac_back` | 158 |
| `volup` / `voldown` | `volumeup` / `volumedown` | 115 / 114 |
| `mute` | `audiomute` | 113 |
| `power` | `power` | 116 |
| `left` | `left` | 105 (an ignored key, used for the negative test) |

**Verify every row in the guest** using the app log (each key is logged, Part 1b and 2b). QEMU's `usb-kbd` is a boot keyboard, so consumer-page keys (likely `ac_back`, possibly the volume keys or `power`) may not arrive. Report exactly which rows work. Any that don't are covered by the control socket in the VM and checked on the real remote at M3 hardware sign-off. **Don't** work around them with a different emulated device without asking.

### 4c. `vmctl.py remote {unplug,plug}` and `vmctl.py powerbutton`

- `remote unplug|plug`: QMP `device_del` / `device_add` of `remote0`, the same pattern and `DEVICE_DELETED` wait as `usb unplug|plug`. Factor out the shared helper rather than duplicating it.
- `powerbutton`: QMP `system_powerdown`, i.e. an ACPI power-button press, to test Part 3.

### 4d. Tests (`tests/test_vmctl.py`)

- remote attached by default
- `--no-remote` omits it
- xhci exactly once with remote + stick, and absent with neither
- `key` sends down, then up, with the right qcode and `device`
- `--hold` is honoured (mock `time.sleep`)
- unknown button → exit 2 with the list of valid names
- `remote unplug` waits for `DEVICE_DELETED`
- `powerbutton` sends `system_powerdown`

Use a fake QMP server, as in the existing tests.

---

## Part 5: taskipy tasks

Add to `[tool.taskipy.tasks]`, each with a `help` string:

| Task | Command |
|---|---|
| `vm-key` | `python vm/vmctl.py key` (append `BUTTON [--hold S]`) |
| `remote-unplug` | `python vm/vmctl.py remote unplug` |
| `remote-plug` | `python vm/vmctl.py remote plug` |
| `vm-powerbutton` | `python vm/vmctl.py powerbutton` |

These drive QEMU on the host. They don't change the target, so they're consistent with AGENTS.md.

---

## Part 6: Unit tests

The host has no evdev and no mpv. Use fakes everywhere.

**`tests/test_commands.py`**
- `parse_command`: every value round-trips, case and whitespace are tolerated
- unknown and empty input → `None`, never raises

**`tests/test_remote.py`**
- `import rabbitears.remote` works with evdev absent. Assert `"evdev" not in sys.modules` after the import.
- `KeyRepeater`:
  - immediate emit on press
  - no repeat for non-repeatable commands
  - repeat after delay, then every interval
  - stops on release
  - newer held key takes over
  - `value == 2` ignored
  - late poll → at most one emit
  - `clear()` stops a held key
- `RemoteInput` with fake devices (`path`, `info.vendor`, `name`, `fileno()`, `grab()`, `read()`, `close()`, events with `type`/`code`/`value`/`timestamp()`):
  - opens only vendor-matching devices, all of them
  - grabs each
  - a grab failure is logged and reading continues
  - unmapped keys ignored
  - non-`EV_KEY` ignored
  - stale presses dropped
  - device `OSError` → device dropped and repeater cleared, no exception
  - rescan after 3 s picks up a re-plugged device
  - "no remote found" logged once per transition

**`tests/test_control.py`** uses a real `AF_UNIX` socket in a temp dir:
- a command → `ok` and the command reaches the handler
- `status` → valid JSON from the callback
- unknown → `error`
- over-long line → dropped
- a silent client is dropped after 1 s (fake clock) without blocking
- socket mode is `0600`
- a stale socket file is replaced
- `ctl.py` exit codes

**`tests/test_app.py`** (extend; fake player, clock, `media_present` as before):
- **channel:**
  - up/down with gaps (e.g. channels 1, 3, 7) and wrap both ways
  - tune lands at the *current* clock position for the new channel
  - state saved
  - an empty channel is skipped in the direction of travel
  - all channels empty → NO_SIGNAL
- **volume:**
  - clamped at `volume_cap` and at 0
  - step honoured
  - unmutes when muted
  - state flushed at most once per 2 s across a burst
- **mute** toggles
- **standby:**
  - enter hides video and mutes
  - every other command ignored
  - wake after a clock jump re-tunes at the *new* position
  - wake restores the pre-standby mute state
  - wake in NO_SIGNAL shows the test pattern
  - drive pulled during standby → no crash; wake → NO_SIGNAL
- `INFO`/`BACK` → no player calls
- NO_SIGNAL: channel commands ignored, volume/mute/power still work
- `status()` fields in each state
- **loop:** `select` timeout honours the earliest source deadline; commands from two sources are both dispatched; `step()` still runs when no source is readable

**`tests/test_config.py`:** each new key's default, a valid override, and invalid → default (including `volume_step < 1` and `repeat_interval < 0.1`).

---

## Part 7: End-to-end validation in the VM

**Content first.** Zapping needs several channels, including a gap.
- **Ask the user** for at least two more channels: source dirs plus `NN-slug` names, with numbers that leave a gap (e.g. `01`, `03`, `07`).
- Alternatively, if the user prefers, build the extra channels from subsets of already-transcoded files. **Never copy raw source files** (SCOPE §5).
- Then run `uv run task media-check …`, `uv run task usb-build`, and `uv run task vm-run-usb &`. The remote is attached by default. Then `uv run task deploy-vm`.

Report evidence (log excerpts, `ctl status` output, screenshots described), not assertions. Use `ctl status` over SSH as the read-only state probe, and the first-channel step 6 schedule math to check positions.

1. **Unit tests:** `uv run task test`, all passing (4 expected skips).
2. **Remote discovered:**
   - the guest vendor check from Part 4 passes
   - the log shows `remote0`'s event device(s) opened and grabbed
3. **Key map:** run `uv run task vm-key <button>` for every button in the 4b table. Report which arrive, their logged codes and commands, and that `left` is logged as ignored.
4. **Zapping:**
   - `up` from the highest channel wraps to the lowest, and `down` from the lowest wraps to the highest
   - gaps are skipped
   - after each change, `ctl status` and mpv's `path`/`time-pos` agree with the schedule math within 3 s
   - a screenshot after a change shows video, and it differs from the previous channel's
5. **Hold-to-repeat:** `uv run task vm-key down --hold 2.0`. The number of logged `channel_down` commands matches `1 + floor((2.0 − repeat_delay) / repeat_interval)`, ±1. Show the count.
6. **Held key + remote pulled:**
   - start `uv run task vm-key down --hold 10 &`, then run `uv run task remote-unplug` about 3 s in
   - zapping stops within one `repeat_interval` of the unplug, and the log shows the device lost, with no exception
   - `uv run task remote-plug` → re-grabbed within 5 s; a key press works
7. **Volume:**
   - press `volup` until past the cap: mpv `volume` equals `volume_cap` and stops there
   - `state.json` on the stick has the new volume, written within about 2 s
   - `mute` toggles mpv `mute`
   - `volup` while muted unmutes
   - if the volume keys don't arrive via `usb-kbd` (step 3), do this step through `ctl` and say so
8. **Standby:**
   - `power` → screenshot is black, mpv `mute` is true, a channel key is logged as ignored
   - wait at least 60 s, including an EOF retune if a short file allows it
   - `power` → video, at the schedule position for the *wake* time (math shown), not where it was hidden
   - also: unplug the stick during standby, then wake → NO_SIGNAL card; re-plug → video
9. **Control socket over SSH** (deliberate test actions):
   - `PYTHONPATH=/opt python3 -m rabbitears.ctl channel_up` → `ok` and a channel change
   - `status` → JSON
   - `bogus` → `error` and exit 1
   - `ls -l /run/rabbitears/control.sock` shows `srw-------`
10. **Power never shuts down:**
    - `rc-service rabbitears stop` (deliberate test action: no grab now), then `uv run task vm-key power` → the box stays up (same `boot_id`) and `rabbitears-power` logged it
    - `uv run task vm-powerbutton` → same
    - then `uv run task deploy-vm` to return to a clean, rebooted state
11. **Persistence across reboot:** zap to a non-default channel and change the volume, then `uv run task deploy-vm`. After boot it's on that channel at the correct position, with that volume.
12. **Idempotent deploy:** `uv run task deploy-vm` twice → `0 changed` on the second run, no lbu WARNING, `verify: OK`. Then hand-edit the power handler over SSH (a deliberate drift test): `uv run task vm-verify` reports `DRIFT` and the next deploy repairs it.

---

## Part 8: Docs

- **`SCOPE.md`:**
  - §4.4: add the hold-repeat timings, volume step and cap behaviour, and "volume keys unmute".
  - §4.6: "only Power works in standby".
  - §6 repo layout: add `commands.py`, `control.py`, `ctl.py` (`remote.py` is already listed), and the `system/acpid/` contents.
  - §6 installed paths: the acpid handler path(s) and `/run/rabbitears/control.sock` (runtime, tmpfs).
  - §8: standby now lands in M3 with the power key. M4 becomes "OSD: green channel flash, info overlay".
  - §10, add:
    - "Input sources → `Command` → `App.handle`; sources are swappable"
    - "Local root-only control socket for tests/automation; commands + status only, no network"
    - "Neither the remote nor the ACPI power button shuts the box down"
    - "QEMU `usb-kbd` (vendor 0627) stands in for the remote in the VM"
- **`AGENTS.md`:** add the new tasks to the VM dev loop section. Add one line explaining the `usb-kbd`/vendor 0627 stand-in and which buttons can't be injected (from step 3). Add one line saying `rabbitears.ctl` is the sanctioned SSH test action for driving the app.
- **`vm/VAGRANT.md`:** a short "Remote" section covering `vm-key` (with the button table and which buttons work), `remote-unplug`/`remote-plug`, `vm-powerbutton`, `ctl` over SSH, and the `vm-stop` note from Part 3.

---

## Completion criteria

1. `Command` and `App.handle` implement the Part 1 table. Every behaviour is unit-tested through `handle()` with no evdev, socket or mpv.
2. `remote.py` and `control.py` are interchangeable `InputSource`s in a single-threaded `select` loop. `import rabbitears.remote` works without evdev.
3. In the VM, the emulated remote is found by the real vendor scan (no VM branch). Zapping with gaps and wrap, hold-repeat, volume cap, mute and standby/wake all work. Wake and every channel change land at the schedule-correct position.
4. Remote unplug/replug and stick unplug/replug (including during standby and mid-hold) recover with no intervention and no crash.
5. Neither `KEY_POWER` (ungrabbed) nor the ACPI power button shuts the box down. The override is installed only by `setup.sh` and covered by the manifest and `verify.sh`.
6. Repeat deploys report `0 changed` with `verify: OK`. Hand edits to the power handler are caught as `DRIFT`.
7. New tasks exist with accurate help. SCOPE, AGENTS.md and VAGRANT.md are updated as in Part 8.
8. All previously passing tests still pass.

---

## Report back with

- Any deviations between the real `app.py` and this prompt's assumptions, and how you adapted.
- The guest vendor check output, and the Part 7 step 3 key table: button → arrived? → logged code → command.
- Hold-repeat count vs expected (step 5), and the timing of repeat stopping after remote unplug (step 6).
- Schedule math vs mpv's `path`/`time-pos` for one channel change and for the standby wake.
- Screenshots (paths, what's visible) for zap, standby and wake.
- Whether `vid=no` survived `loadfile` in standby, and what you did about it.
- The acpid findings from Part 3 step 1, the files you installed, and the step 10 evidence.
- Final `deploy-vm` output (`changed` count, `verify: OK`), the drift test, and `uv run task --list`.
- Anything in the paradigm you couldn't satisfy, and anything you think is wrong with this prompt.
