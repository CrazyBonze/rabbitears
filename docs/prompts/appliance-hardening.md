# Agent prompt: appliance hardening (M5). ×20 pull-the-plug soak + close the v1.0 gaps

**Goal: prove the box survives a week of a kid yanking the plug, killing mpv,
and pulling the remote dongle — then close the three documented-but-missing
pieces so the repo's actual state matches what SCOPE says.** This is the last
milestone before the reproducibility push (M6).

SCOPE §8 defines M5 as "OpenRC respawn, NO SIGNAL screen, USB hot-plug,
unplayable-file skip, pull-the-plug tests (×20)". The first four are **already
implemented** (foundation + first-channel prompts); the only literal remaining
item is the **×20 pull-the-plug soak**, and today nothing runs it — `vm/e2e.py`
does one unplug/replug and one mpv-kill. The soak belongs in the VM: the
emulated stick (`usb-unplug`/`usb-plug`) and the mpv-kill already exercise the
exact recovery paths the 3040 will need, and the VM can repeat them 20× in
minutes where the real box can't.

**Current state (2026-10-21, verified against the tree — start from this):**

- M2/M3/M4 code is all committed (latest commit is the README/docs pass).
  `vm/e2e.py` runs the full pipeline once and passes.
- `vm/e2e.py` has `c_unplug`, `c_replug`, `c_crash` as single-shot check
  methods; `E2E.run()` hardcodes the `steps` list and runs each exactly once.
  `build_parser()` has `--skip-crash` but **no soak/stress mode**.
- `tools/index.py` **does not exist**, yet SCOPE §5 says "run `tools/index.py`
  on the desktop against the USB drive" and §6 lists it under `tools/`. The
  logic already exists as a library: `src/rabbitears/index.py`
  `get_durations(channel_path, index_path)` probes missing durations and saves
  the cache atomically, and `src/rabbitears/channels.py`
  `scan_channels(root) -> list[Channel]` discovers channels.
- `docs/hardware.md` **does not exist**, yet SCOPE §6 lists it ("3040 notes,
  BIOS settings, adapters, video chain").
- SCOPE §8 milestones are stale: M2 and M3 are unchecked even though their work
  is committed; only M4 is checked. `pyproject.toml` has `vm-e2e`, `usb-unplug`,
  `usb-plug` but **no** `vm-soak` and **no** `media-index` task.

**Prerequisite:** `docs/prompts/osd.md` (M4) must be complete — this prompt
reuses its `vm-e2e` pipeline wholesale. If `uv run task vm-e2e` does not pass
today, stop and report; don't re-do it.

Work from the repository root (paths are repo-relative). Read these in full
before making changes: `AGENTS.md`, `SCOPE.md` (especially §1 goal 2, §4.7, §8,
§10), `docs/prompts/osd.md`, `docs/prompts/first-channel-vm-playback.md`,
`vm/e2e.py`, `vm/vmctl.py`, `pyproject.toml` (`[tool.taskipy]`),
`src/rabbitears/{index,channels}.py`, `tools/transcode.sh`, `README.md`, and
`tests/{test_e2e,test_index,test_vmctl}.py`.

---

## The paradigm (unchanged; every change must respect it)

The rules in `first-channel-vm-playback.md` ("The paradigm" and "Host-side
conventions") still apply in full. Additional rules for this task:

1. **The soak is honest, not a smoke test.** Each cycle must assert *full
   recovery* — drive unmounts, mpv lands on the NO SIGNAL card, drive
   re-mounts, mpv plays again at the schedule-correct position — not just "the
   app didn't crash". Reuse the existing `c_unplug`/`c_replug`/`c_crash`
   assertions; don't write weaker copies.
2. **A failed cycle is recorded, not fatal to the whole soak.** Every cycle is
   independent; collect all results and fail the summary if any cycle failed.
3. **The soak reuses the one run path.** Don't add a second way to boot the VM.
   `--soak` is a flag on `vm/e2e.py`, not a new script.
4. **No new runtime dependency, no new box-side code.** The indexer is a
   host-side convenience; the box already re-indexes at boot.
5. **Never fabricate hardware facts.** `docs/hardware.md` is assembled from
   SCOPE §2/§3 and `docs/remote.md`; anything not already documented (BIOS
   settings, exact adapter part numbers) is marked "to confirm on Box A", not
   invented.

---

## Part 1: `vm/e2e.py --soak N` — the ×20 pull-the-plug soak

### 1a. Plan the soak purely

Add a module-level helper so the soak's *shape* is unit-testable without a VM:

- **`soak_plan(n: int, crash_every: int = 5) -> list[tuple[str, str]]`** — the
  list of `(step_name, method_name)` pairs `run()` appends after the normal
  pipeline. For `n` cycles:
  - `("soak-unplug-<i>", "c_unplug")`, `("soak-replug-<i>", "c_replug")` for
    every cycle `i` in `1..n`;
  - `("soak-crash-<i>", "c_crash")` on every `crash_every`-th cycle;
  - a final `("soak", "c_soak_summary")`.
  Return `[]` when `n <= 0`. Pure: no I/O, no clocks.

### 1b. Run the plan

- Add `--soak N` (int, default `0`) to `build_parser()`. Help text: repeat the
  unplug/replug (and periodic crash) recovery cycle N times after the normal
  pipeline.
- In `E2E.run()`, after the existing `steps` list, extend it with
  `soak_plan(self.args.soak)` **only** when `--soak > 0`. The existing checks
  run unchanged for the one-shot path, so `vm-e2e` without `--soak` is
  byte-identical in behaviour.
- `c_unplug`/`c_replug`/`c_crash` must be safe to run repeatedly back-to-back.
  Audit them for any state that would make cycle 2 behave differently from
  cycle 1 (e.g. the `c_remote_volume` trick of stepping volume down first —
  the soak starts *after* `replug`, with the drive mounted and playing). If a
  check assumes a one-shot environment, factor the assumption out rather than
  weakening the assertion.

### 1c. `c_soak_summary`

Collect the per-cycle recovery latencies already measured inside
`c_unplug` (unplug → NO SIGNAL) and `c_replug` (replug → playing). The summary
returns one line like:

```
soak: 20/20 cycles clean; unplug→NO SIGNAL mean 0.3s max 1.1s; replug→playing mean 5.2s max 8.0s; 4 crash cycles respawned
```

and raises `CheckFailed` naming the failed cycle numbers and their details if
any cycle failed. Latency capture must not perturb the individual checks'
existing return strings (append the detail to the check's own `Result`, or
record it on `self` — your call; pick the one that doesn't change the existing
`PASS`/`FAIL` lines).

### 1d. `vm-soak` task

Add to `[tool.taskipy.tasks]` (with a `help` string):

| Task | Command |
|---|---|
| `vm-soak` | `python vm/e2e.py --soak 20` |

20 is the SCOPE §8 number. The task reuses `vm-e2e`'s boot/deploy path; no new
QEMU handling.

---

## Part 2: `tools/index.py` — the desktop indexer (SCOPE §5/§6)

The box re-indexes missing durations at boot ("slow but safe"), but SCOPE §5
tells the user to pre-index on the desktop against a 256 GB library, and the
tool it names doesn't exist. Add it.

- New file `tools/index.py`, host-side, run through `uv` (the editable
  `rabbitears` install provides `rabbitears.index` and `rabbitears.channels`).
- **CLI:** `tools/index.py MOUNT` where `MOUNT` is the USB drive's mount point
  (required; error out with usage if missing). For every channel under
  `MOUNT/channels/` (via `scan_channels`), call
  `get_durations(ch.path, MOUNT/.rabbitears/index.json)` and print one line per
  channel in the same shape the app logs — `CH NN: N playable, M skipped, loop
  <H>h<MM>m<SS>s` — plus a final `indexed N channels` summary. `get_durations`
  already probes only missing entries and saves atomically, so re-running is a
  cheap no-op; don't reimplement that.
- **Never crash on bad input** (SCOPE §1 goal 3, the indexer is the same rule):
  a missing `channels/`, an unreadable mount, a channel with no `.mp4`, or an
  absent `ffprobe` all degrade to a logged line and exit 0, never a traceback.
- Reuse the existing duration/loop formatting where reasonable, but keep
  `tools/index.py` a thin CLI over the library — no logic copied from
  `src/rabbitears/index.py`.
- **Task:** add `media-index = { cmd = "python tools/index.py", help = "Pre-index media durations on the desktop (append MOUNT)" }`.

### Tests (`tests/test_index.py` or a new `tests/test_index_cli.py`)

Load `tools/index.py` by file path (the `test_vmctl.py` pattern — `tools/` is
not a package), inject/mock `scan_channels` and `get_durations` (tests never
run real ffprobe), and cover:
- the happy path prints one line per channel and the summary;
- a missing `channels/` dir exits 0 with a "no channels" line, no traceback;
- missing `MOUNT` argument prints usage and exits non-zero;
- per-channel skipped counts appear in the output.

---

## Part 3: `docs/hardware.md` — the 3040 notes (SCOPE §6)

Write `docs/hardware.md` from facts already in the repo. Do not invent
anything. Source it from SCOPE §2 (hardware table, box roles, remote pairing),
§3 (platform), and `docs/remote.md`. Cover, at minimum:

- the two box roles (A = dev, B = clean) and why B is never hand-edited;
- the video chain: 2× DP, the active DP→HDMI adapter requirement, the
  HDMI→composite converter for the CRT (SCOPE §2);
- the remote/dongle pairing (same USB ID, factory-paired, label A/B);
- what is and isn't validated on the VM vs. Box A (VAAPI/hwdec only on the
  3040; VM validates DRM output, app, OpenRC, media handling).

Anything not derivable from the repo (exact BIOS settings, specific adapter
part numbers, the headphone-jack audio path from §11) gets an explicit
`> TODO (confirm on Box A):` line rather than a guess. Add `docs/hardware.md`
to the `README.md` repository/docs blurb if it lists docs explicitly.

---

## Part 4: SCOPE — make §8 and §10 match reality

- **§8 milestones:** check off M2 and M3 (`[x]`) — their work is committed and
  e2e-verified. Leave M1 unchecked with a parenthetical that the VM portion is
  done but the on-box diskless install (§2 of `docs/alpine-install.md`) is
  still hardware work. Leave M6 unchecked. Check M5 `[x]` **only after**
  `uv run task vm-soak` actually passes 20/20; add a note that the on-hardware
  ×20 pull-the-plug and the remote hardware sign-off are a Box A step, distinct
  from this VM soak (mirror the "VM validates the output path, not VAAPI"
  decision in §10).
- **§10 decisions log:** add one row for "the ×20 pull-the-plug soak is an
  automated `vm-e2e --soak` loop over the emulated stick; the on-hardware soak
  and remote sign-off stay Box A steps" and one for the desktop indexer if you
  change its interface (e.g. `tools/index.py` as a thin CLI over
  `rabbitears.index`).

---

## Part 5: docs

- **`AGENTS.md`:** under the VM dev loop, mention `uv run task vm-soak` as the
  hardening acceptance test (the ×20 pull-the-plug), and `uv run task
  media-index` next to `transcode`/`media-check` in the media flow. Keep it to
  the existing terse style.
- **`README.md`:** if you add `media-index`, mention it in the "Load media"
  section as the optional pre-index step (the box re-indexes at boot anyway).

---

## Completion criteria

1. `uv run task test`: all pass, only the 4 expected `test_player.IntegrationStubs` skips.
2. `uv run task vm-e2e` (no `--soak`): unchanged, passes.
3. `uv run task vm-soak`: 20 cycles, all clean, with the `c_soak_summary` line
   showing `20/20` and the latency spread. Screenshots/logs under
   `/tmp/rabbitears-e2e/`. If it can't run (VM busy / needs a rebuild), say so
   and show the unit tests for `soak_plan`/`c_soak_summary` instead.
4. `uv run task media-index <staging mount>` prints one line per channel and
   exits 0; a second run is a no-op (no re-probe of cached files).
5. `docs/hardware.md` exists and contains no invented specifics (only repo-sourced
   facts + explicit `TODO (confirm on Box A)` lines).
6. SCOPE §8 shows M2/M3 checked, M5 checked iff the soak passed, and §10 has the
   new decision rows.
7. No commits unless the user asks.

## Report back with

- Every file created/changed/deleted and what changed in it.
- Full `uv run task test` output, and `uv run task --list` showing the new tasks.
- The `c_soak_summary` line from the real `vm-soak` run (or why it couldn't run).
- The `media-index` output for the mock stick (first run and the no-op second run).
- Which cycle assertions, if any, needed to be made repeat-safe, and why.
- Anything in this prompt you found wrong or ambiguous and how you resolved it.
