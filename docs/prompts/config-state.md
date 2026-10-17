# Agent prompt: implement `config.py` and `state.py`

**Task: Implement `config.py` and `state.py` for the Rabbit Ears project**

Repository is at `/home/michael/rabbitears`. Read `SCOPE.md` thoroughly before writing any code. The project uses `uv` — add any dependencies to `pyproject.toml` with `uv add <package>` if needed, but prefer stdlib.

---

## 1. `src/rabbitears/config.py`

Loads `/etc/rabbitears/config.toml` with hardcoded defaults for every value. The app must boot and run correctly even if the file is absent.

Define a `Config` dataclass with fields:
- `epoch: float` — Unix timestamp. Default: `0.0` (schedules from 1970-01-01; acceptable per SCOPE §4.2)
- `volume_cap: int` — Maximum volume (0–100). Default: `80`
- `osd_color: str` — OSD channel flash color. Default: `"#33CC33"` (SCOPE §4.5)
- `osd_duration: float` — Channel flash duration in seconds. Default: `2.5`
- `osd_font_size: int` — OSD font size. Default: `72`
- `socket_path: Path` — mpv IPC socket. Default: `Path("/run/rabbitears/mpv.sock")`
- `mount_point: Path` — USB drive mount point. Default: `Path("/media/rabbitears")`

Implement:
- `load_config(path: Path = Path("/etc/rabbitears/config.toml")) -> Config`
  Reads the TOML file if present. Unknown keys are ignored. Invalid values for a key fall back to the default for that key only (not the whole file). Returns a fully populated `Config` in all cases. Use `tomllib` for parsing.

Add a module docstring. Use only stdlib.

---

## 2. `src/rabbitears/state.py`

Persists the last channel number and volume level to `.rabbitears/state.json` on the USB drive. Same atomic write pattern as `index.py` (temp file + rename). The app must function if state is absent, corrupt, or the drive is read-only.

Define a `State` dataclass with fields:
- `channel: int` — Last channel number. Default: `-1` (sentinel meaning "no saved state, use the lowest channel")
- `volume: int` — Last volume level. Default: `50`

Implement:
- `load_state(state_path: Path) -> State`
  Loads `state.json`. Returns defaults if absent, corrupt, or malformed. Validates that loaded values are the right types.
- `save_state(state_path: Path, state: State) -> None`
  Writes atomically. Silently swallows any `OSError` (drive may be absent or read-only — that's fine, SCOPE §3 rule: app never crashes on missing USB).

Add a module docstring. Use only stdlib.

---

## 3. `tests/test_config.py`

Cover:
- Missing file returns all defaults
- Corrupt TOML returns all defaults
- Valid file with all keys overrides all fields correctly
- A file with one invalid value (e.g. `volume_cap = "loud"`) falls back to the default for that key only, preserving other valid values
- Unknown keys in the TOML are silently ignored
- `socket_path` and `mount_point` come back as `Path` objects

---

## 4. `tests/test_state.py`

Cover:
- Missing file returns defaults
- Corrupt JSON returns defaults
- Valid file roundtrips correctly
- Invalid types in JSON fall back to defaults
- `save_state` with a read-only path swallows the error silently (use `chmod 444` on a temp dir or mock `os.replace`)
- Saved and reloaded state matches original

---

## Completion criteria

Run `uv run python -m unittest discover tests/ -v` from the repo root. All non-skipped tests must pass. Fix any failures before finishing.

Report back with a summary of what was implemented, the full test output, and any decisions made that weren't explicitly specified.
