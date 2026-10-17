# Agent prompt: VM screenshot validation loop + NO SIGNAL test pattern

**Two goals:**
1. Make the VM screenshot capability a clean, first-class part of the development loop — one command, sane defaults, useful output.
2. Generate the `tools/testpattern.png` NO SIGNAL image that the app will display when there is nothing to play (SCOPE §4.7, §6).

Repository is at `/home/michael/rabbitears`. Read `vm/vmctl.py`, `SCOPE.md`, and `vm/VAGRANT.md` in full before making any changes.

---

## Context

`vmctl.py` now has two screenshot paths: a VNC-based background thread (`--screenshot`) and a QMP `screendump` subcommand (`vmctl.py screenshot --monitor SOCK --out FILE`). In practice the QMP path is the right one for the development loop — it is synchronous, reliable, and works headless. The VNC path is fragile (depends on a VNC port being open, races against the VM coming up, runs in a daemon thread that can be silently killed). The goal is to make `vmctl.py run` with a `--monitor` socket the standard way to boot and grab a frame, and to make the grab itself a single obvious command.

---

## Part 1: Clean up `vmctl.py run` defaults and remove the VNC screenshot path

The VNC screenshot machinery (`--screenshot`, `--screenshot-out`, `--screenshot-delay`, `--vnc-host`, `--vnc-port`, `_screenshot_worker`, `start_screenshot_thread`, `_grab_vnc_frame_pyvnc`, `_grab_vnc_frame_raw`, `_grab_vnc_frame`, `_save_png`, and the `VNC_HOST`/`VNC_PORT`/`DEFAULT_SCREENSHOT_OUT`/`DEFAULT_SCREENSHOT_DELAY` constants) should be **removed**. It was a reasonable first attempt but the QMP path supersedes it entirely and the VNC path has no VNC endpoint in the current VM setup (provision.sh now uses SPICE). Removing it simplifies the interface and eliminates dead code.

The `struct` and `zlib` imports were used only by the VNC path and `_save_png`; remove them too if nothing else uses them.

After removal the `run` subcommand should have exactly these arguments:
- `--disk` (required)
- `--host` / `--port` (serial console, existing defaults)
- `--monitor` (optional, QMP socket path — same as today)
- `--display {none,spice}` (existing, default `none`)

### Default monitor socket path

When `--monitor` is not supplied, default it to `/tmp/rabbitears-qmp.sock` rather than `None`. This means every `vmctl.py run` automatically exposes a QMP socket without the user having to ask, making `vmctl.py screenshot` work out of the box immediately after boot. The user can override or disable it with `--monitor ""` (treat an empty string as "no socket").

---

## Part 2: Make `vmctl.py screenshot` pleasant to use

The `screenshot` subcommand already exists but requires both `--monitor` and `--out`. Improve it:

- Default `--monitor` to `/tmp/rabbitears-qmp.sock` (matching the new `run` default).
- Default `--out` to `vm/screenshot.png`.
- After a successful grab, print a single clear line to stdout:
  ```
  screenshot saved: vm/screenshot.png  (1280x800)
  ```
  Include the actual pixel dimensions read from the PPM header.
- On failure, print a clear warning to stderr and exit 1 (not 0 — so a script can detect the failure).

Reading the PPM header for dimensions: a PPM written by QEMU starts with `P6\n<width> <height>\n<maxval>\n`. Parse width and height from that header to include in the success message.

---

## Part 3: Generate `tools/testpattern.png`

Create `tools/generate_testpattern.py` — a standalone script that generates and writes `tools/testpattern.png`. Run it as part of this task to produce the file.

The image must:
- Be **1280×720** (720p — the max resolution per SCOPE §5, and a safe target for both the VM and the Wyse 3040 via composite)
- Use **only stdlib** (`zlib`, `struct`) — no Pillow, no numpy, no external tools. The same `_save_png` pattern already in vmctl.py is fine to adapt.
- Look like a real analog TV "no signal" card: **not** a modern flat "no input" screen. The design should feel like a 1980s/90s broadcast test card — something a kid would recognize as "the TV isn't working." Specific requirements:
  - **Black background**
  - **"NO SIGNAL"** in large white text, centered. Render the text as a pixel bitmap using a hardcoded 8×8 or 5×7 bitmap font embedded directly in the script (a small ASCII subset is enough — you only need A–Z, 0–9, space, and a handful of punctuation). No font files, no PIL.
  - **Colored vertical bars** across the top third of the image — the classic SMPTE-style bar pattern (white, yellow, cyan, green, magenta, red, blue, black, left to right). These are immediately recognizable as a broadcast test card.
  - **Scan-line effect**: every other horizontal line should be 85% brightness (multiply RGB by 0.85) to simulate a CRT scan-line texture across the whole image.
  - The overall mood: lo-fi, analog, broadcast. Not slick.

The script should be runnable directly:
```sh
python3 tools/generate_testpattern.py
# -> writes tools/testpattern.png and prints: generated tools/testpattern.png (1280x720)
```

It must be idempotent (safe to re-run, overwrites silently).

---

## Part 4: Update `VAGRANT.md`

Replace the "Screenshot (visual confirmation)" section and the "Taking a screenshot via QMP (headless)" section with a single consolidated section:

```markdown
## Taking a screenshot (development loop)

Every `vmctl.py run` automatically opens a QMP monitor socket at
`/tmp/rabbitears-qmp.sock`. Once the VM has booted, grab the current
framebuffer from a second terminal:

```sh
python3 vm/vmctl.py screenshot
# -> saves vm/screenshot.png and prints: screenshot saved: vm/screenshot.png (1280x800)
```

Override the defaults when needed:

```sh
python3 vm/vmctl.py screenshot --monitor /tmp/other.sock --out /tmp/frame.png
```

The grab is synchronous and reliable (QMP `screendump` via the QEMU monitor
socket). It works with `--display none` (headless) and `--display spice`.
```

Keep the "Live display with SPICE" section unchanged.

---

## Part 5: Update `tests/test_vmctl.py`

- Remove all tests that cover the deleted VNC screenshot machinery
  (`ScreenshotTest` and any helpers that only supported it).
- Update `QmpScreendumpTest`:
  - `test_qemu_args_monitor_default` — verify that calling `qemu_args()` with no `monitor_sock` argument still produces a `-qmp` flag pointing at `/tmp/rabbitears-qmp.sock`.
  - `test_qemu_args_monitor_override` — verify a custom `monitor_sock=Path("/tmp/other.sock")` is used instead.
  - `test_qemu_args_monitor_disabled` — verify that passing `monitor_sock=None` explicitly omits `-qmp` entirely. (This is the escape hatch for `--monitor ""`.)
  - Existing tests for `display=none`, `display=spice`, QMP handshake sequence, and protocol error should be kept and still pass.
- Add `test_generate_testpattern` in a new `TestPatternTest` class:
  - Import `generate_testpattern` from `tools/generate_testpattern.py` (add `tools/` to `sys.path` in the test setUp, or import it directly).
  - Call the generation function with a temp output path.
  - Assert the file exists, starts with `b"\x89PNG"`, and is larger than 10 KB (a sanity check that it isn't empty).

---

## Completion criteria

1. `python3 -m unittest discover tests/ -v` — all vmctl tests pass; no ScreenshotTest (VNC) tests remain.
2. `python3 vm/vmctl.py run --disk /path/to/disk.qcow2` automatically exposes `/tmp/rabbitears-qmp.sock`.
3. `python3 vm/vmctl.py screenshot` (no arguments) grabs from `/tmp/rabbitears-qmp.sock` and saves `vm/screenshot.png`.
4. `python3 tools/generate_testpattern.py` produces `tools/testpattern.png` — a valid PNG, 1280×720, with colored bars and scan-line texture.
5. `tools/testpattern.png` is committed to the repo (it is a static asset, not generated at runtime).

---

## Report back with

- Summary of every file changed or created and what changed.
- Full `python3 -m unittest discover tests/ -v` output.
- Confirmation that `tools/testpattern.png` was written and its file size.
- Any decisions made about the test pattern design (font choice, bar colors, etc.).
