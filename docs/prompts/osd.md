# Agent prompt: on-screen display (M4). Green channel flash + info overlay

> **Status (2026-10-21): implemented and verified in the VM (`vm-e2e` passes
> with the five new OSD checks).** The design shipped as written: pure
> `osd.py`, `show_osd_text`/`clear_osd` on the player, the flash at the end of
> `_tune()`, the `INFO`/`BACK` overlay in `handle()`, the config range checks,
> and the `e2e` checks. Four mpv 0.41 quirks had to be resolved empirically in
> the VM (Part 2's "one empirical unknown"), and each is recorded in SCOPE §10:
> (1) `show-text` escapes `{…}` by default, so ASS tags are turned back on with
> the byte `\u0092` + `"a0"` (what `osd-ass-cc/0` expands to), not the literal
> `${osd-ass-cc/0}`; (2) `font-terminus` is a bitmap font with no face at 72 px
> and renders nothing, so the OSD uses `font-dejavu` (a scalable outline font,
> now in `alpine/packages.txt`); (3) ASS anchors position *lines*, not inline
> spans, so the top-right flash and the centred overlay are separate `\n` lines
> in one payload; (4) the overlay is rendered at half the flash's font size so
> its three lines fit on screen. One test-double fix: `tools/e2e-remote.py`
> now sends a press *and* a release (a tap) for every button, because the
> app's `KeyRepeater` repeats a held channel key until its release arrives and
> a bare press re-tuned every 0.4 s, corrupting the later e2e checks. See
> `SCOPE.md` §4.5/§10 for the recorded design.

**Goal: the box shows two viewer-facing displays, both rendered as text over
live video (SCOPE §4.5):**

1. **Channel flash:** on every tune, `CH 03` appears in blocky green
   (`#33CC33`-ish) in the top-right for about 2.5 s, then disappears on its own.
2. **Info overlay:** `OK` shows a small panel with the channel number + name,
   the current show/episode title (from the filename), and the time remaining
   in the current file. `Back` dismisses it. `OK` toggles it.

Readability over composite on a CRT is the bar: large, all-caps, thick border
(SCOPE §4.5). This is the last remote-facing milestone; after it, the only
visible surface left is appliance hardening (M5).

**Current state (2026-10-20, verified against the tree — start from this):**

- `src/rabbitears/osd.py` **does not exist**. Create it.
- `src/rabbitears/app.py:361` stubs `INFO`/`BACK`:
  `_log.info("%s: %s ignored (overlay is M4)", ...)`. Replace it.
- `Command.INFO` / `Command.BACK` already exist in `commands.py` (`INFO` is
  "toggle info overlay", `BACK` is "dismiss overlay") and the evdev map already
  routes `KEY_ENTER`→`INFO`, `KEY_BACK`→`BACK`. No key-map work.
- `src/rabbitears/player.py` has **no** OSD text method. It has `set_pause`
  whose docstring says "used for the info overlay / guide" — that's a leftover
  from a scrapped "guide" design (see the decision below); nothing calls it.
- `config.py` **already** has `osd_color="#33CC33"`, `osd_duration=2.5`,
  `osd_font_size=72`, wired into `load_config` with per-key fallback and partly
  tested. **Do not re-add them**; use them, and note that `osd_font_size` has
  no range validation yet (see Part 4).
- `system/mpv/mpv.conf` sets OSD styling (`osd-font-size=90`,
  `osd-color="#33CC33"`, `osd-border-size=3`, `osd-bar=no`) with a comment that
  "the green channel flash is drawn by the app, not mpv's OSD". Reconcile that
  comment with whatever mechanism you actually ship (Part 7).
- Fonts: `font-terminus` is already in `alpine/packages.txt` (SCOPE §3).
- `app.py` already has `channel_label(ch)` (`CH 03 · Magic School Bus`) and a
  private `_fmt_duration`; `App._tune()` is the single funnel for every tune
  (initial, zap, EOF retune, standby wake, unplayable skip), and `App.step()`
  runs every ~100 ms with `now = self.clock()` available.

**Prerequisite:** `docs/prompts/remote-control.md` (M3) must be complete. This
prompt relies on everything it delivered — `App.handle` as the single action
entry point, the `Command` vocabulary, the `Player` wrapper, the FIFO reader,
the control socket and `ctl.py`, the `vm-key`/`remote-*` tasks, and the
`vm-e2e` pipeline. If any of that is missing, stop and report; don't re-do it.

Work from the repository root (paths are repo-relative). Read these in full
before making changes: `AGENTS.md`, `SCOPE.md` (especially §4.5, §4.6, §6),
`docs/prompts/remote-control.md`, `docs/prompts/first-channel-vm-playback.md`,
`pyproject.toml` (`[tool.taskipy]`), `src/rabbitears/{app,player,config,commands,remote}.py`,
`system/mpv/mpv.conf`, `alpine/packages.txt`, `vm/e2e.py`, and
`tests/{test_app,test_player,test_config,test_remote}.py`.

---

## The paradigm (unchanged)

The rules in `first-channel-vm-playback.md` ("The paradigm" and "Host-side
conventions") and `remote-control.md` still apply in full. Additional rules for
this task:

1. **Pure formatting lives in `osd.py`.** Like `schedule.py`, it does no I/O
   and reads no clocks; it turns plain inputs (a channel number, a title, a
   duration, an offset) into display text. It imports nothing from `app.py`,
   `player.py`, or mpv. Every string it produces is unit-testable on the host.
2. **The app decides *when*; `osd.py` decides *what*; the player decides *how*
   (which mpv IPC command).** No layer reaches around another: `app.py` calls
   `osd.py` for text and `player.py` to show/clear it; `osd.py` never touches
   mpv or the clock; `player.py` never knows about channels or schedules.
3. **Never crash on input.** A bad title, a missing duration, an mpv OSD error,
   or `INFO` in NO_SIGNAL are all logged and ignored. The overlay is cosmetic;
   it must never take the box out of a safe state (SCOPE §1 goal 3).
4. **Box side is stdlib + apk only.** No new runtime dependency: OSD is text
   through mpv's libass, which `mpv` already brings in.

---

## Part 1: `src/rabbitears/osd.py` — pure display formatting

All functions are pure (no I/O, no clock reads, no imports from the app).

- **`channel_flash_text(number: int) -> str`** — returns `CH 03` for number 3:
  exactly `"CH {number:02d}"`, all caps. This is the whole flash; no name, no
  episode, nothing else.
- **`show_title(path: Path) -> str`** — the on-screen title for the file being
  played: `path.stem` (filename without extension). Don't parse `S01E03` out of
  it and don't prettify it (SCOPE §4.5: "from filename"). A filename with no
  extension returns the name unchanged.
- **`remaining_seconds(duration: float, offset: float, elapsed: float) -> float`**
  — seconds left in the current file, given the tune offset and time elapsed
  since load: `duration - (offset + elapsed)`, clamped to `>= 0.0`. Pure math;
  the caller supplies the clock value.
- **`format_remaining(seconds: float) -> str`** — human text for the overlay,
  e.g. `12m 30s` (and `1h 05m 09s` for an hour-plus). Decide the exact format
  and record it; the existing `app._fmt_duration` (`1h05m09s`) is a log format,
  not necessarily an OSD format. Keep `osd.py` self-contained: don't import it
  from `app.py`.
- **`info_text(number: int, name: str, title: str, remaining: str) -> str`** —
  the overlay body. At minimum, three lines in this order:
  1. `CH 03 · Magic School Bus` (same shape as `app.channel_label`)
  2. the title
  3. `12m 30s remaining` (or whatever `format_remaining` returns, suffixed with
     ` remaining`)

If the chosen mpv mechanism needs ASS markup (Part 2), put the *pure* ASS
wrappers here too (e.g. `flash_ass(text) -> str` and `info_ass(text) -> str`
that position the flash top-right and the overlay centre, apply
`config.osd_color` and `config.osd_font_size`, and add a thick border). Keep
them pure: they take the text and config values as arguments, return a string,
and touch nothing else.

## Part 2: `src/rabbitears/player.py` — OSD over the IPC socket

Add the smallest OSD surface that satisfies Part 3:

- **`show_osd_text(text: str, duration_ms: int) -> None`** — display `text` for
  `duration_ms` milliseconds, then let it disappear on its own. Used by the
  channel flash. A `duration_ms <= 0` means "until cleared" (used by the
  overlay, if that's the mechanism you ship).
- **`clear_osd() -> None`** — remove any OSD text now showing (dismiss).

**This is the one empirical unknown in M4, and you must resolve it in the VM,
not by reading docs:** Alpine 3.24 ships mpv 0.41, and the exact IPC surface
for text OSD changed across releases (the `loadfile` option-order lesson from
`remote-control.md` Part 2 applies here). Candidates:

- `show-text "<text>" [duration_ms] [level]` — the OSD message layer; short-lived
  by default, styled by the `osd-*` options in `mpv.conf`.
- ASS override tags inside the `show-text` payload (e.g. `{\an9}` top-right,
  `{\fs72}`, `{\c&H...&}`, `{\bord3}`), gated by the right `osd-*` property, so
  the flash is top-right and the overlay is a centred panel.
- `osd-overlay` / `overlay-add` for a persistent, separately-managed layer.

Requirements the mechanism must meet:

1. The flash appears **top-right**, large and green, and clears itself after
   `config.osd_duration` seconds without the app having to send a second
   command on the happy path (a timer in `step()` is acceptable; so is a
   self-expiring `show-text`).
2. The overlay appears **centre screen** and stays until the app dismisses it,
   independent of the flash (a flash must not wipe the overlay).
3. Both survive a `loadfile`/`show_image` — or, if the chosen mechanism is
   cleared by a subsequent load, re-apply it after every load while shown and
   say so (the M3 standby prompt had the same rule for `vid=no`).
4. No new process, no threads, no file writes (mpv's OSD is in-memory).

**Record your choice in SCOPE §10** (e.g. "OSD is `show-text` with ASS override
tags; flash is top-right `{\an9}` and self-expiring, overlay is a centred
long-duration text cleared with `show-text ''`"), and say in your report which
candidates you tried and what broke.

`show_osd_text`/`clear_osd` raise `PlayerError` on IPC failure exactly like the
existing methods, and must not leave a half-connected socket. Follow the
existing `_send` pattern — no new request plumbing.

## Part 3: `src/rabbitears/app.py` — when to show what

Add two bits of viewer state, orthogonal to NO_SIGNAL/PLAYING and to standby:

- `self._overlay_shown: bool` (False initially)
- `self._overlay_updated_at: float` (monotonic, throttles the remaining line)

**Channel flash.** At the end of `_tune()` (after a successful `load`, before or
after the existing log line), show `osd.channel_flash_text(ch.number)` with
duration `int(config.osd_duration * 1000)`. Because `_tune()` is the single
funnel, this covers initial tune, zap, EOF retune, standby wake, and unplayable
skip. If the overlay is up when a tune happens, refresh it too (see below) so
the overlay never shows a stale channel.

**Info overlay — replace the `INFO`/`BACK` stub in `handle()`:**

- `INFO` (toggle): if the overlay is up, dismiss it; if not, show it. Only
  meaningful in PLAYING — in NO_SIGNAL, log and ignore (the standby gate in
  `handle()` already blocks it in standby).
- `BACK` (dismiss): dismiss if shown, otherwise log-and-ignore.

**Show** means: render `osd.info_text(...)` for the *current* channel and file,
and display it (centred, persistent). **Dismiss** means: `player.clear_osd()`
and `self._overlay_shown = False`.

**The overlay does not pause playback** (SCOPE §4.5 lists "time remaining", and
"feels like real TV" §4.2 means the show keeps playing behind the info). That
makes `player.set_pause` dead code — remove it and its `# used for the info
overlay / guide` docstring, and note the removal in your report. (The "channel
guide that pauses" is the parking-lot `KEY_HOMEPAGE` idea, SCOPE §9.)

**Live time remaining.** While `_overlay_shown`, once per second, recompute the
remaining time from `_last_loaded_offset` and `self.clock() - self._load_time`,
and re-render the overlay. Don't re-render on every `step()` (10 Hz); throttle
to ~1 s via `_overlay_updated_at`.

**Transitions:**
- Entering standby (`_power`): dismiss the overlay and clear the OSD. Wake does
  **not** restore it.
- `_enter_no_signal()`: dismiss the overlay and clear the OSD.
- A channel change (`_zap`) while the overlay is up: the video changes and the
  flash fires; refresh the overlay to the new channel rather than dismissing it
  (the overlay reflects "now").

**Logging.** Keep the existing `handle()` logging style — e.g.
`remote: info -> overlay shown`, `remote: back -> overlay dismissed`,
`remote: info ignored (no signal)`. Every show/dismiss/log-ignore is logged with
its source.

**`status()`.** Add `"overlay": self._overlay_shown` (and, if cheap,
`"remaining": <seconds> | null`) so the e2e and `ctl` can assert overlay state
without a screenshot. Keep everything else unchanged.

## Part 4: config — already present, wire it in

`osd_color`, `osd_duration`, `osd_font_size` already exist with per-key
fallback. Do not re-add them. In this task:

1. Use all three in the OSD path (color and size in the ASS/formatting; duration
   for the flash). If `osd_font_size` turns out not to control anything in your
   chosen mechanism, say so in the report instead of leaving a dead key.
2. `osd_duration` and `osd_font_size` are not range-checked. Add the same
   per-key guard as `volume_step`/`repeat_interval`: `osd_duration` invalid if
   `<= 0`, `osd_font_size` invalid if `< 8`. Follow the "coerce first, then
   range check" comment in `load_config`.
3. Extend `tests/test_config.py` for the two new range checks (valid override,
   out-of-range → default).

## Part 5: unit tests (host: no mpv, no evdev)

Use the existing fake-player pattern from `test_app.py` (a `PlayerLike` stub
that records calls). The fake must add `show_osd_text`/`clear_osd` and drop its
`set_pause` stub (it's at `test_app.py:44`) in step with the real `Player`.

**`tests/test_osd.py`** (new):
- `channel_flash_text`: 1 → `CH 01`, 42 → `CH 42` (zero-padded).
- `show_title`: `foo.mp4` → `foo`; a path with no extension returns the name
  unchanged.
- `remaining_seconds`: exact arithmetic; clamps at 0 for a file that has
  overrun; correct at `elapsed=0`.
- `format_remaining`: a few known values (0, 59, 60, 61, 3600, 3661) produce the
  expected strings.
- `info_text`: contains the channel line, the title, and the remaining line, in
  that order.
- If you added ASS wrappers: flash is top-right, overlay is centred, and the
  colour/font-size appear in the output.

**`tests/test_app.py`** (extend):
- a tune triggers `show_osd_text` with `CH NN` and `osd_duration*1000` ms (the
  exact call the fake records — assert the text and duration, not the styling).
- `INFO` in PLAYING shows the overlay and sets `status()["overlay"]`; a second
  `INFO` dismisses it.
- `BACK` dismisses; `BACK` with nothing shown is a no-op.
- `INFO`/`BACK` in NO_SIGNAL are ignored (no player OSD call).
- zapping while the overlay is up refreshes the overlay to the new channel (the
  text reflects the new channel) and does not dismiss it.
- standby dismisses the overlay; wake does not restore it.
- while shown, the remaining line is recomputed at most once per second (fake
  clock: two `step()`s within 1 s → one re-render; after >1 s → another).
- entering NO_SIGNAL clears the overlay.

**`tests/test_player.py`** (extend):
- `show_osd_text` sends the exact IPC command array you shipped (text, duration,
  level), via the existing fake-socket harness.
- `clear_osd` sends the clear command.
- an IPC error raises `PlayerError` (and, where the existing tests do, leaves
  the socket clean).

## Part 6: end-to-end validation in the VM

No new media is needed — M3's `vm-e2e` already boots the VM with the mock stick
and a real `mpv`. After `uv run task deploy-vm`, extend `vm/e2e.py` with an OSD
check and run the full `uv run task vm-e2e`.

Add to `vm/e2e.py` (between `remote-volume` and `unplug`, matching the existing
`c_remote_*`/`_remote` helpers):

1. **Flash appears.** Zap (or force a tune), then immediately screenshot: the
   top-right shows green text (assert green pixels in the top-right region, the
   same idea as `c_motion`). After ~3 s (longer than `osd_duration`), a second
   screenshot shows it gone.
2. **Overlay toggles.** `_remote("ok")` → `ctl status` reports `overlay: true`
   and a screenshot shows the overlay text (channel line + title + a "remaining"
   suffix). `_remote("back")` → `overlay: false` and the screenshot is clean.
   `_remote("ok")` twice → toggles on then off.
3. **Overlay + zap.** With the overlay up, zap: `ctl status` shows the new
   channel and `overlay` still true; the flash re-fires.
4. **Overlay + standby.** With the overlay up, `power` → `ctl status` shows
   `standby: true, overlay: false`; wake → overlay stays off.
5. **Time remaining is plausible.** From `ctl status`'s `remaining` (or the log),
   sanity-check it against the file duration and the schedule position (the
   first-channel step-6 math), within a few seconds.

Report screenshots (paths + what's visible), not assertions. If the OSD text is
illegible at the VM's 1280x800 over a screenshot, say so and still report the
pixel evidence; legibility on the CRT is a real-3040 check (SCOPE §7.3).

## Part 7: docs

- **`SCOPE.md`:**
  - §8: mark M4 done.
  - §10: add the OSD mechanism decision from Part 2, the "overlay does not
    pause" decision, and the remaining-line format from Part 1.
  - §6: `osd.py` is already listed; leave it. If you added `system/fonts/`
    content, document it — otherwise note the repo still uses `font-terminus`.
- **`system/mpv/mpv.conf`:** reconcile the "drawn by the app, not mpv's OSD"
  comment with the mechanism you shipped, and fix/remove the stale `osd-*`
  fallback claims if they no longer describe reality.
- **`vm/VAGRANT.md`:** a short "On-screen display" note — `vm-key ok`/`back` to
  toggle/dismiss, and that the flash is visible in `vm-shot` right after a zap.
- **`AGENTS.md`:** only if this changes a command or the dev loop (it shouldn't).
- **`docs/prompts/osd.md`** (this file): flip the top to a `Status (2026-10-20):
  implemented, with …` note describing any deviation, exactly as
  `remote-control.md` did.

---

## Completion criteria

1. `osd.py` is pure and unit-tested; `app.py` never formats OSD text inline.
2. `INFO`/`BACK` behave per Part 3; the overlay never pauses playback and never
   leaves the PLAYING/standby/NO_SIGNAL state machine.
3. The channel flash fires on every tune and self-clears; the overlay persists
   until dismissed and survives `loadfile` (or you documented re-applying it).
4. The chosen mpv mechanism is verified against mpv 0.41 in the VM and recorded
   in SCOPE §10.
5. `uv run task test`: all tests pass, only the 4 expected `test_player.IntegrationStubs`
   skips. `uv run task vm-e2e` passes with the new OSD checks.
6. `config.py` range-checks the two OSD numeric keys; tests cover them.
7. SCOPE, `mpv.conf`, and `vm/VAGRANT.md` are updated as in Part 7.
8. No commits unless the user asks.

---

## Report back with

- The mpv mechanism you shipped, which candidates you tried and what broke, and
  the SCOPE §10 wording you added.
- Whether the overlay pauses (it shouldn't) and the `set_pause` removal.
- The exact `osd.py` API and the `format_remaining` format you chose.
- Full `uv run task test` output.
- `vm-e2e` evidence: the OSD check names, `ctl status` lines, and screenshot
  paths for flash-on, flash-off, overlay-on, overlay-off, overlay+zap, and
  overlay+standby.
- Whether the OSD text was legible in the VM screenshots, with a description.
- Anything in this prompt you found wrong or ambiguous and how you resolved it.
