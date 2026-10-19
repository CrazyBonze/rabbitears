# Agent prompt: screenshot + NO SIGNAL follow-ups (honest failures, real tests)

**Status:** the original version of this prompt was completed in commit `5369434`. That version covered the QMP socket on by default, the VNC removal, the zero-argument `vmctl.py screenshot`, the `tools/testpattern.png` generator and asset, and the consolidated VAGRANT.md section. The original text is at `git show ced2442:docs/prompts/validate-and-nosignal.md`. **Don't redo any of it.** Later commits changed the conventions that version assumed: `--disk` now defaults to the libvirt image, everything runs through `uv run task …`, and VAGRANT.md is organised around tasks. This prompt covers only the gaps found in review.

**Goal: a screenshot either produces a real PNG or fails loudly, and the tests check what the image actually contains, not its file size.**

This task adds **no app features** and **doesn't change how the test card looks**. Bars, font, layout and scan lines all stay exactly as they are.

Work from the repository root (paths are repo-relative). Read these in full before making changes: `AGENTS.md`, `vm/vmctl.py`, `tools/generate_testpattern.py`, `tests/test_vmctl.py`, `vm/VAGRANT.md`, and `pyproject.toml` (the `[tool.taskipy]` sections).

---

## Problems being fixed

1. **A screenshot can fail silently.** `ppm_to_png` tries Pillow, then ImageMagick `convert`. If both are missing it copies the raw PPM to `screenshot.png`, prints `screenshot saved: …` and exits 0. Pillow isn't in the uv env, so on a clone without ImageMagick every screenshot is a PPM with a `.png` name and nothing reports a problem.
2. **The `vmctl.py` docstring is wrong.** It says "Stdlib only", but the conversion step depends on Pillow or `convert`. `vm/VAGRANT.md` (Prerequisites) makes the same claim ("`vmctl.py` is stdlib-only"). Part 1 makes that claim true, so VAGRANT.md needs no edit there.
3. **The screenshot path has no tests.** Nothing covers `ppm_to_png`, `_ppm_dimensions` or `cmd_screenshot`.
4. **The test-pattern test is weak.** It only checks `len(data) > 10 KB`. The asset is 10,712 bytes, so a design change that compresses better (removing the scan lines, say) would fail the test even though the PNG is valid. Meanwhile a wrong image of the right size passes. Nothing checks that the committed `tools/testpattern.png` still matches the generator.
5. **`TestPatternTest` imports the generator through `sys.path`.** AGENTS.md says `vm/` and `tools/` scripts are loaded by file path, as `test_vmctl.py` already does for `vmctl.py`.

---

## Part 1: stdlib PPM→PNG in `vm/vmctl.py`

Replace the Pillow/`convert`/copy fallback chain with a stdlib-only converter (`zlib`, `struct`):

- **`_read_ppm(path) -> tuple[int, int, bytes]`** returns width, height and the RGB bytes.
  - Parse the P6 header as whitespace-separated tokens: magic, width, height, maxval. Skip `#` comment lines. Don't rely on `\n` positions.
  - Require magic `P6` and maxval `255`.
  - Require exactly `width * height * 3` bytes of pixel data after the single whitespace byte that ends the header.
  - Raise `ValueError` with a clear message on any violation.
- **`_encode_png(width, height, rgb) -> bytes`** produces 8-bit RGB (colour type 2) with filter type 0 on every scanline and `IHDR`/`IDAT`/`IEND` chunks with correct CRCs.
  - The same pattern already exists as `_save_png` in `tools/generate_testpattern.py`. **Duplicate it** rather than importing across scripts: both are standalone files loaded by path, and the code is about 15 lines.
- **`ppm_to_png(ppm_path, png_path) -> tuple[int, int]`** reads, encodes, and writes `png_path` atomically (temp file in the same directory, then `os.replace`). It returns `(width, height)` and **raises** on failure. If it fails, an existing `png_path` must be left untouched.
- **Remove `_ppm_dimensions`.** `ppm_to_png` returns the dimensions now.
- **`cmd_screenshot`** uses the returned dimensions.
  - Success prints exactly `screenshot saved: <out>  (<W>x<H>)` to stdout, exit 0. That's the current format, two spaces before the parenthesis.
  - Any exception prints `[vmctl] WARNING: …` to stderr, exit 1. Keep the temp-dir cleanup.
- **Fix the module docstring:** stdlib only, no Pillow or ImageMagick. When you're done, `vm/vmctl.py` must contain no `PIL` import and no `convert` call.

---

## Part 2: tests in `tests/test_vmctl.py`

Add a module-level helper, `_decode_png(data: bytes) -> tuple[int, int, int, int, bytes]`, returning width, height, bit depth, colour type and raw RGB. It must:
- check the PNG signature
- walk the chunks and verify each CRC
- concatenate and decompress `IDAT`
- support filter type 0 only (fail the test on anything else)
- strip the per-row filter byte

Both new test classes use it.

**`PpmToPngTest`**
- **Round trip.** Write a small P6 PPM (e.g. 3×2, every pixel distinct, with a `# comment` line in the header). Then `ppm_to_png` it and decode the result. Expect the returned dimensions, the IHDR dimensions, bit depth 8, colour type 2, and pixels identical to the input.
- **Bad input** (one subtest each): `P3` magic, maxval `65535`, truncated pixel data, an empty file.
  - Each raises `ValueError`.
  - A pre-existing output file keeps its original bytes.
  - No temp file is left in the output directory.

**`CmdScreenshotTest`** (mock `vmctl.screendump`; no real sockets)
- **Success.** The mocked `screendump` writes a small valid PPM to the path it was given. Expect exit 0, the exact stdout line above, and a decodable PNG at `--out`.
- **QMP failure.** `screendump` raises `ConnectionError`. Expect exit 1, a `[vmctl] WARNING` on stderr, and a pre-existing `--out` file left byte-for-byte unchanged.
- **Bad PPM.** `screendump` writes garbage. Expect exit 1 and the out file unchanged.

**`TestPatternTest`** (rewrite it)
- Load `tools/generate_testpattern.py` with `importlib.util.spec_from_file_location`, following the `vmctl` pattern at the top of the file. Drop the `sys.path` manipulation.
- Generate into a temp dir and decode. Assert 1280×720, bit depth 8, colour type 2. **Remove the `> 10 KB` assertion.**
- Check pixels against the spec, not against the generator's constants. Hardcode the expected values in the test.
  - **Bars.** Bars are 160 px wide and occupy rows 0–239. At the horizontal centre of each of the 8 bars, check one even row and one odd row:
    - even row: white `(255,255,255)`, yellow `(255,255,0)`, cyan `(0,255,255)`, green `(0,255,0)`, magenta `(255,0,255)`, red `(255,0,0)`, blue `(0,0,255)`, black `(0,0,0)`
    - odd row: each channel is `int(c * 0.85)`
  - **Background.** A pixel well below the bars and outside the text, e.g. (40, 650), is black.
  - **Text.** The text band (rows 440–519, columns 280–999) contains at least one pixel with every channel at 255 on an even row. There are no non-black pixels in rows 240–439 or 520–719.
- **The committed asset matches the generator.** Decode `tools/testpattern.png` from the repo and compare its pixels (not its bytes) to the freshly generated image. Compare pixels because zlib builds can produce different compressed bytes for identical images.

Keep every other existing test as it is.

---

## Part 3: `vm/VAGRANT.md`

Extend the "Taking a screenshot" section. Keep it short and use tasks only, no bare `python3`:

````markdown
## Taking a screenshot

```sh
uv run task vm-shot
# → screenshot saved: vm/screenshot.png  (1280x800)
uv run task vm-shot --out /tmp/frame.png   # extra args go to vmctl.py screenshot
```

Uses QMP `screendump` on `/tmp/rabbitears-qmp.sock`, which every `vm-run` opens.
It works headless and needs only the stdlib. It exits non-zero and leaves any
existing file untouched if the VM isn't running or the grab fails.
````

`vm/screenshot.png` is tracked. Don't commit incidental changes to it.

---

## Completion criteria

1. `uv run task test`: all tests pass, with only the 4 expected `test_player.IntegrationStubs` skips.
2. `vm/vmctl.py` contains no `PIL` and no `convert` (check with the Grep tool).
3. **With the VM stopped:** `uv run task vm-shot` exits non-zero and prints a `[vmctl] WARNING` to stderr. `git status` shows `vm/screenshot.png` unchanged.
4. **With the VM running** (`uv run task vm-run` in another terminal; ask the user if it can't start, e.g. because the libvirt domain is up): `uv run task vm-shot` prints the success line, and `file vm/screenshot.png` reports `PNG image data, … 8-bit/color RGB`. Look at the image and describe it. Then restore the tracked file: `git checkout vm/screenshot.png`.
5. `uv run python tools/generate_testpattern.py` still prints `generated tools/testpattern.png (1280x720)`. If the bytes of `tools/testpattern.png` change but the pixel-equality test passes, it's just a different zlib: restore the file with `git checkout` and don't commit it.
6. No commits unless the user asks.

---

## Report back with

- Every file changed and what changed in it.
- Full `uv run task test` output.
- Output of criteria 3 and 4, including what the live screenshot shows (or why the live check couldn't run).
- Anything in the spec you found wrong or ambiguous and how you resolved it.
