"""Runtime state persistence (SCOPE.md §3, §4.8).

The last channel and volume are saved to ``.rabbitears/state.json`` on the
USB drive so a reboot tunes back to where the kid left off (SCOPE §4.8).
The drive may be absent, read-only, or corrupt at any time — the app must
never crash on it (SCOPE §3 rule).  Writes are atomic (temp file + rename),
matching the index cache, so a pulled plug can't leave a torn file behind.

Stdlib only: ``json`` for the payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os


@dataclass
class State:
    """Last channel and volume.

    ``channel`` defaults to -1, a sentinel meaning "no saved state — use the
    lowest channel" (SCOPE §4.8).
    """

    channel: int = -1
    volume: int = 50


# A pristine instance used as the per-key fallback source, so the defaults
# live in exactly one place (the dataclass).
_DEFAULTS = State()


def _as_int(value: object, default: int) -> int:
    """Coerce a JSON value to int, or return ``default`` if it isn't an int."""
    if isinstance(value, bool):  # bool is a subclass of int; reject it explicitly
        return default
    if isinstance(value, int):
        return value
    return default


def load_state(state_path: Path) -> State:
    """Load state from ``state_path``; return defaults if absent, corrupt, or malformed.

    Each known key is validated independently: a bad value for one key uses
    that key's default while the other is honored.  Unknown keys are ignored.
    """
    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return State()
    if not isinstance(data, dict):
        return State()

    return State(
        channel=_as_int(data.get("channel"), _DEFAULTS.channel),
        volume=_as_int(data.get("volume"), _DEFAULTS.volume),
    )


def save_state(state_path: Path, state: State) -> None:
    """Write state atomically (temp file + rename).

    Any :class:`OSError` is swallowed: the USB drive may be absent or
    read-only, and the app must never crash on it (SCOPE §3).
    """
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = state_path.with_name(state_path.name + ".tmp")
        payload = {"channel": state.channel, "volume": state.volume}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_path)
    except OSError:
        pass
