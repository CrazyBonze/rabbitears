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
    # Volume change per VOLUME_UP/DOWN press (invalid if < 1).
    volume_step: int = 5
    # Seconds a channel key must be held before it starts repeating (SCOPE §4.4).
    repeat_delay: float = 0.5
    # Seconds between repeats while held (invalid if < 0.1).
    repeat_interval: float = 0.4
    # Sleep-timer presets in minutes (SCOPE §4.4 Sun): the chooser's options in
    # cycle order; the chooser always appends "Off" as the final option.
    # Must be non-empty positive ints (a bad value falls back to the default).
    sleep_presets: list[int] = field(default_factory=lambda: [30, 60, 90])
    # Seconds before the sleep deadline that the ticking countdown appears.
    sleep_countdown: float = 60.0
    # How long the sleep chooser stays open before it auto-dismisses (no change
    # unless OK is pressed).
    sleep_chooser_duration: float = 5.0
    # USB vendor ID the remote scan looks for (SCOPE §4.4: Adomax 0x0627).
    remote_vendor: int = 0x0627
    # Local root-only control socket (commands + read-only status).
    control_socket: Path = field(default_factory=lambda: Path("/run/rabbitears/control.sock"))
    # OSD channel-flash color (SCOPE §4.5: blocky green).
    osd_color: str = "#33CC33"
    # Channel-flash duration in seconds (SCOPE §4.5: about 2.5s).
    osd_duration: float = 2.5
    # Info-overlay duration in seconds (SCOPE §4.5: auto-dismisses; longer than
    # the flash so three lines are readable).
    osd_info_duration: float = 5.0
    # OSD font size (SCOPE §4.5: large, all-caps, thick). One size for the
    # channel flash, the info overlay, and the volume/mute indicator.
    osd_font_size: int = 36
    # mpv JSON IPC socket (SCOPE §4.3).
    socket_path: Path = field(default_factory=lambda: Path("/run/rabbitears/mpv.sock"))
    # Button FIFO: the evdev daemon (writer) -> the app (reader) (SCOPE §4.4).
    buttons_path: Path = field(default_factory=lambda: Path("/run/rabbitears/buttons"))
    # USB drive mount point (SCOPE §3, §6).
    mount_point: Path = field(default_factory=lambda: Path("/media/rabbitears"))
    # NO SIGNAL test pattern image (SCOPE §4.7).
    testpattern_path: Path = field(default_factory=lambda: Path("/usr/share/rabbitears/testpattern.png"))


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


def _as_int_list(value: object, default: list[int]) -> list[int]:
    """Coerce to a list of positive ints, or ``default`` if not a list of them."""
    if not isinstance(value, (list, tuple)):
        return default
    out: list[int] = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            return default
        out.append(v)
    return out


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

    # Coerce first, then apply range checks: a value that parses but is out of
    # range falls back to that key's default (per-key fallback, never a crash).
    volume_step = _as_int(data.get("volume_step"), _DEFAULTS.volume_step)
    if volume_step < 1:
        volume_step = _DEFAULTS.volume_step
    repeat_interval = _as_float(data.get("repeat_interval"), _DEFAULTS.repeat_interval)
    if repeat_interval < 0.1:
        repeat_interval = _DEFAULTS.repeat_interval
    osd_duration = _as_float(data.get("osd_duration"), _DEFAULTS.osd_duration)
    if osd_duration <= 0:
        osd_duration = _DEFAULTS.osd_duration
    osd_info_duration = _as_float(data.get("osd_info_duration"), _DEFAULTS.osd_info_duration)
    if osd_info_duration <= 0:
        osd_info_duration = _DEFAULTS.osd_info_duration
    osd_font_size = _as_int(data.get("osd_font_size"), _DEFAULTS.osd_font_size)
    if osd_font_size < 8:
        osd_font_size = _DEFAULTS.osd_font_size
    sleep_presets = _as_int_list(data.get("sleep_presets"), _DEFAULTS.sleep_presets)
    if not sleep_presets:
        sleep_presets = _DEFAULTS.sleep_presets
    sleep_countdown = _as_float(data.get("sleep_countdown"), _DEFAULTS.sleep_countdown)
    if sleep_countdown <= 0:
        sleep_countdown = _DEFAULTS.sleep_countdown
    sleep_chooser_duration = _as_float(data.get("sleep_chooser_duration"), _DEFAULTS.sleep_chooser_duration)
    if sleep_chooser_duration <= 0:
        sleep_chooser_duration = _DEFAULTS.sleep_chooser_duration

    return Config(
        epoch=_as_float(data.get("epoch"), _DEFAULTS.epoch),
        volume_cap=_as_int(data.get("volume_cap"), _DEFAULTS.volume_cap),
        volume_step=volume_step,
        repeat_delay=_as_float(data.get("repeat_delay"), _DEFAULTS.repeat_delay),
        repeat_interval=repeat_interval,
        sleep_presets=sleep_presets,
        sleep_countdown=sleep_countdown,
        sleep_chooser_duration=sleep_chooser_duration,
        remote_vendor=_as_int(data.get("remote_vendor"), _DEFAULTS.remote_vendor),
        control_socket=_as_path(data.get("control_socket"), _DEFAULTS.control_socket),
        osd_color=_as_str(data.get("osd_color"), _DEFAULTS.osd_color),
        osd_duration=osd_duration,
        osd_info_duration=osd_info_duration,
        osd_font_size=osd_font_size,
        socket_path=_as_path(data.get("socket_path"), _DEFAULTS.socket_path),
        buttons_path=_as_path(data.get("buttons_path"), _DEFAULTS.buttons_path),
        mount_point=_as_path(data.get("mount_point"), _DEFAULTS.mount_point),
        testpattern_path=_as_path(data.get("testpattern_path"), _DEFAULTS.testpattern_path),
    )
