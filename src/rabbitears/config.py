"""Configuration loading with hardcoded defaults (SCOPE.md §4, §6).

The app reads ``/etc/rabbitears/config.toml`` on boot.  Every value has a
hardcoded default so the box boots and runs correctly even when the file is
absent (a fresh install before the operator has written one).

Semantics:
  * Unknown keys are ignored.
  * An invalid value for a key falls back to that key's default *only* — the
    rest of the file is still honored.
  * :func:`load_config` always returns a fully populated :class:`Config`.

Stdlib only: ``tomllib`` for parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tomllib

_DEFAULT_CONFIG_PATH = Path("/etc/rabbitears/config.toml")


@dataclass
class Config:
    """Runtime configuration for the Rabbit Ears box.

    Every field has a default so a ``Config()`` (or a :func:`load_config`
    against a missing file) is always fully usable.
    """

    # Unix timestamp the schedule is anchored to (SCOPE §4.2).  0.0 means the
    # schedule runs from 1970-01-01, which is acceptable per SCOPE §4.2.
    epoch: float = 0.0
    # Maximum volume, 0-100 (SCOPE §4.4: volume is clamped to a max cap).
    volume_cap: int = 80
    # OSD channel-flash color (SCOPE §4.5: blocky green).
    osd_color: str = "#33CC33"
    # Channel-flash duration in seconds (SCOPE §4.5: about 2.5s).
    osd_duration: float = 2.5
    # OSD font size (SCOPE §4.5: large, all-caps, thick).
    osd_font_size: int = 72
    # mpv JSON IPC socket (SCOPE §4.3).
    socket_path: Path = field(default_factory=lambda: Path("/run/rabbitears/mpv.sock"))
    # USB drive mount point (SCOPE §3, §6).
    mount_point: Path = field(default_factory=lambda: Path("/media/rabbitears"))


# A pristine instance used as the per-key fallback source, so the defaults
# live in exactly one place (the dataclass).
_DEFAULTS = Config()


def _as_float(value: object, default: float) -> float:
    """Coerce a TOML value to float, or return ``default`` if it isn't numeric."""
    if isinstance(value, bool):  # bool is a subclass of int; reject it explicitly
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _as_int(value: object, default: int) -> int:
    """Coerce a TOML value to int, or return ``default`` if it isn't an int."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _as_str(value: object, default: str) -> str:
    """Return ``value`` if it is a string, else ``default``."""
    if isinstance(value, str):
        return value
    return default


def _as_path(value: object, default: Path) -> Path:
    """Coerce a TOML string to a Path, or return ``default`` if it isn't a string."""
    if isinstance(value, str):
        return Path(value)
    return default


def load_config(path: Path = _DEFAULT_CONFIG_PATH) -> Config:
    """Load configuration from ``path``, falling back to defaults per key.

    A missing file, unreadable file, or corrupt TOML yields an all-default
    :class:`Config`.  When the file parses, each known key is validated
    independently: a bad value for one key uses that key's default while the
    other keys are honored.  Unknown keys are ignored.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    return Config(
        epoch=_as_float(data.get("epoch"), _DEFAULTS.epoch),
        volume_cap=_as_int(data.get("volume_cap"), _DEFAULTS.volume_cap),
        osd_color=_as_str(data.get("osd_color"), _DEFAULTS.osd_color),
        osd_duration=_as_float(data.get("osd_duration"), _DEFAULTS.osd_duration),
        osd_font_size=_as_int(data.get("osd_font_size"), _DEFAULTS.osd_font_size),
        socket_path=_as_path(data.get("socket_path"), _DEFAULTS.socket_path),
        mount_point=_as_path(data.get("mount_point"), _DEFAULTS.mount_point),
    )
