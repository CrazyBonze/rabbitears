"""Channel discovery and zapping (SCOPE.md §4.1).

Each directory in the `channels/` folder on the USB drive is one TV
channel.  Folder names follow the `NN-slug` format: the leading number
is the channel number and the slug becomes the display name
(`03-magic-school-bus` → **Magic School Bus**).  An optional
`channel.toml` inside the folder can override the display name and
enable shuffled playback.  Channel numbers may have gaps; up/down
zapping skips missing numbers and wraps around.

Stdlib only: `tomllib` (Python 3.11+) for TOML, `dataclasses` for the
models.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

_FOLDER_RE = re.compile(r"^(\d{2})-(.+)$")


@dataclass
class Channel:
    """A single channel: a directory of files that is always "on the air"."""

    number: int
    name: str
    path: Path
    shuffle: bool = False


@dataclass
class ChannelConfig:
    """Schema for the optional `channel.toml` in a channel folder."""

    name: str | None = None
    shuffle: bool = False


def _name_from_slug(slug: str) -> str:
    """Turn a hyphen-separated slug into a title-cased display name."""
    return " ".join(word.capitalize() for word in slug.split("-") if word)


def _load_config(channel_dir: Path) -> ChannelConfig:
    """Parse and validate channel.toml; return defaults if absent/invalid."""
    config_path = channel_dir / "channel.toml"
    if not config_path.is_file():
        return ChannelConfig()
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return ChannelConfig()

    name = data.get("name")
    if name is not None and not isinstance(name, str):
        return ChannelConfig()
    shuffle = data.get("shuffle", False)
    if not isinstance(shuffle, bool):
        return ChannelConfig()
    return ChannelConfig(name=name, shuffle=shuffle)


def scan_channels(root: Path) -> list[Channel]:
    """Scan `root` for `NN-slug` directories and return channels by number.

    Non-matching entries (files, badly named dirs) are skipped silently.
    A `channel.toml` in a matching directory may override the display
    name and set `shuffle = true`.
    """
    channels: list[Channel] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return channels

    for entry in entries:
        if not entry.is_dir():
            continue
        match = _FOLDER_RE.match(entry.name)
        if not match:
            continue
        number = int(match.group(1))
        name = _name_from_slug(match.group(2))
        config = _load_config(entry)
        if config.name is not None:
            name = config.name
        channels.append(
            Channel(number=number, name=name, path=entry, shuffle=config.shuffle)
        )

    channels.sort(key=lambda c: c.number)
    return channels


def channel_up(channels: list[Channel], current: int) -> Channel:
    """Return the next channel above `current`, wrapping around.

    If `current` is not in the list, returns the lowest channel.
    Gaps in channel numbers are handled by list position, not number.
    """
    if not channels:
        raise ValueError("no channels")
    for i, channel in enumerate(channels):
        if channel.number == current:
            return channels[(i + 1) % len(channels)]
    return channels[0]


def channel_down(channels: list[Channel], current: int) -> Channel:
    """Return the next channel below `current`, wrapping around.

    If `current` is not in the list, returns the highest channel.
    Gaps in channel numbers are handled by list position, not number.
    """
    if not channels:
        raise ValueError("no channels")
    for i, channel in enumerate(channels):
        if channel.number == current:
            return channels[(i - 1) % len(channels)]
    return channels[-1]
