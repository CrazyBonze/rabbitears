"""The shared command vocabulary (SCOPE §4.4).

``Command`` is the single semantic contract between input sources (the evdev
remote via ``remote.py``, the local control socket via ``control.py``) and the
app (``App.handle``). Commands name *what happens* — channel up, mute — never a
physical button or a key code. Mapping buttons to commands is the input
source's job, so ``commands.py`` imports nothing from the app or any source and
any source can depend on it.

``parse_command`` turns a text line (from the control socket) into a
``Command``; it is case-insensitive, strips whitespace, and returns ``None``
for anything it doesn't recognise. It never raises.
"""

from __future__ import annotations

import enum


class Command(enum.Enum):
    CHANNEL_UP = "channel_up"
    CHANNEL_DOWN = "channel_down"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    MUTE = "mute"        # toggle
    POWER = "power"      # toggle standby
    INFO = "info"        # toggle info overlay (M4); confirm in the sleep chooser
    BACK = "back"        # dismiss overlay (M4); cancel the sleep chooser/timer
    SLEEP = "sleep"      # open/advance the sleep-timer chooser (Sun)
    LEFT = "left"        # sleep chooser: previous option
    RIGHT = "right"      # sleep chooser: next option


def parse_command(text: str) -> Command | None:
    """Parse a command name; ``None`` for anything unknown. Never raises."""
    if not isinstance(text, str):
        return None
    value = text.strip().lower()
    if not value:
        return None
    try:
        return Command(value)
    except ValueError:
        return None
