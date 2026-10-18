"""Duration cache for the clock-based schedule (SCOPE.md §4.2, §3).

The schedule needs the duration of every file in a channel to compute
`loop_length`.  Running ffprobe on the whole library on every boot is
slow, so durations are cached in `.rabbitears/index.json` on the USB
drive.  ffprobe is only invoked for files that are new or missing from
the cache.

The index is keyed by **filename only** (not full path) so it survives
the drive being mounted at a different path (desktop vs. box).  Writes
are atomic (temp file + rename) so a pulled plug can never leave a
corrupt index behind; a corrupt index is simply treated as empty and
rebuilt.

Stdlib only: `subprocess` for ffprobe, `json` for the cache.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

_log = logging.getLogger("rabbitears.index")
_FFPROBE_TIMEOUT = 30  # seconds; a stuck ffprobe must not hang boot


def load_index(index_path: Path) -> dict[str, float]:
    """Load the duration cache; return {} if absent, corrupt, or malformed."""
    try:
        with open(index_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only string -> number entries; ignore the rest.
    return {
        key: float(value)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, (int, float))
    }


def save_index(index_path: Path, index: dict[str, float]) -> None:
    """Write the duration cache atomically (temp file + rename)."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = index_path.with_name(index_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, index_path)


def probe_duration(path: Path) -> float | None:
    """Return the duration of a media file in seconds, or None on any error.

    Runs ffprobe as a subprocess.  A missing binary, non-zero exit,
    timeout, or unparseable output all yield None — an unplayable file
    is skipped, never fatal (SCOPE §4.7).
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        duration = float(proc.stdout.strip())
    except ValueError:
        return None
    if duration <= 0:
        return None
    return duration


def get_durations(channel_path: Path, index_path: Path) -> list[tuple[Path, float]]:
    """Return [(path, duration), ...] for a channel's *.mp4 files.

    Files are sorted by filename (the deterministic playlist order from
    SCOPE §4.2).  Cached durations are reused; ffprobe runs only for
    files missing from the cache.  The updated index is saved when
    anything new was probed.  Files whose duration cannot be determined
    are skipped.  Non-mp4 files and unprobeable files are logged at
    WARNING so they are never dropped silently.
    """
    index = load_index(index_path)
    durations: list[tuple[Path, float]] = []
    changed = False

    try:
        all_files = sorted(channel_path.iterdir(), key=lambda p: p.name)
    except OSError:
        return durations

    for path in all_files:
        # Skip directories, dotfiles, and channel.toml.
        if not path.is_file() or path.name.startswith(".") or path.name == "channel.toml":
            continue
        # Non-mp4 files: log and skip. Case-sensitive to match the original
        # glob("*.mp4") behaviour — .MP4 files are not accepted.
        if path.suffix != ".mp4":
            _log.warning("skipping %s: not .mp4 — transcode with tools/transcode.sh", path.name)
            continue
        cached = index.get(path.name)
        if cached is not None:
            durations.append((path, cached))
            continue
        probed = probe_duration(path)
        if probed is None:
            _log.warning("skipping %s: ffprobe failed", path.name)
            continue
        index[path.name] = probed
        changed = True
        durations.append((path, probed))

    if changed:
        save_index(index_path, index)
    return durations
