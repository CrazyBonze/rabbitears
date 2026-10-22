"""Remote input: the button map, the wire format, and the repeat state machine.

Pure module (like ``schedule.py``): no I/O, no FIFO opening, no clock reads
except the injectable timer. The evdev daemon (box) and the e2e test double (VM)
both write one line per button event to the FIFO; the app reads the FIFO, parses
each line, and feeds it to :class:`Remote`, which maps the button to a
:class:`~rabbitears.commands.Command` (the semantic vocabulary) and hands it to
the app via an injected ``handle(command)`` callback. The app is the only
process that acts on a button (SCOPE §4.4).

The remote is the Adomax 2.4GHz USB remote (``0627:697d``), read via evdev key
codes (SCOPE §4.4, docs/remote.md §2) — **not** NEC1/IR. Every button comes from the
keyboard interface; Power is also on the System Control interface. The remote
sends clean press/release pairs and does **not** autorepeat, so holding a button
sends one press and one release; the app-side repeat is what makes it repeat.
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
from collections.abc import Callable

from .commands import Command

_log = logging.getLogger("rabbitears")

# evdev key code -> button name (SCOPE §4.4 + docs/remote.md §2, verified live).
# The MVP set (SCOPE §4.4) plus the reserved buttons the app must recognize and
# ignore (M3). A code not in this map is dropped by the daemon and by parse_line.
BUTTONS: dict[int, str] = {
    116: "power",         # KEY_POWER
    113: "mute",          # KEY_MUTE
    114: "vol-down",      # KEY_VOLUMEDOWN
    115: "vol-up",        # KEY_VOLUMEUP
    103: "channel-up",    # KEY_UP
    108: "channel-down",  # KEY_DOWN
    105: "arrow-left",    # KEY_LEFT   (sleep chooser: previous)
    106: "arrow-right",   # KEY_RIGHT  (sleep chooser: next)
    28:  "ok",            # KEY_ENTER
    158: "back",          # KEY_BACK
    127: "menu",          # KEY_COMPOSE (reserved, M3)
    1:   "home",          # KEY_HOME    (reserved, M3; docs/remote.md says HOMEPAGE 172)
    14:  "sun",           # KEY_BACKSPACE (sleep timer)
    111: "screen-arrow",  # KEY_DELETE  (reserved, M3)
}

# The set of valid button names (for wire-format validation).
NAMES: frozenset[str] = frozenset(BUTTONS.values())

# Button name -> semantic Command (None = reserved, recognized but ignored).
# Mapping buttons to commands is this module's job (SCOPE §4.4); the wire
# format and the evdev codes stay here, never in the app.
COMMANDS: dict[str, Command | None] = {
    "channel-up": Command.CHANNEL_UP,
    "channel-down": Command.CHANNEL_DOWN,
    "vol-up": Command.VOLUME_UP,
    "vol-down": Command.VOLUME_DOWN,
    "mute": Command.MUTE,
    "power": Command.POWER,
    "ok": Command.INFO,
    "back": Command.BACK,
    "sun": Command.SLEEP,
    "arrow-left": Command.LEFT,
    "arrow-right": Command.RIGHT,
    # Reserved (M3): recognized so they can be logged, but ignored.
    "menu": None,
    "home": None,
    "screen-arrow": None,
}

# The only commands that repeat while held (SCOPE §4.4: channel up/down).
REPEATABLE: frozenset[Command] = frozenset({Command.CHANNEL_UP, Command.CHANNEL_DOWN})

# Wire format: one line per EV_KEY event. A press is the button name; a release
# is the name followed by "~" (the app needs the release to stop repeating).
_RELEASE = "~"


def parse_line(raw: str) -> tuple[str, bool] | None:
    """One wire-format line -> ``(name, is_press)``, or None if not a real button.

    A press line is a button name (e.g. ``"channel-up"``); a release line is the
    name with a trailing ``"~"`` (e.g. ``"channel-up~"``). Unknown names and
    garbage return None.
    """
    if not isinstance(raw, str):
        return None
    line = raw.strip()
    if not line:
        return None
    is_press = True
    if line.endswith(_RELEASE):
        is_press = False
        line = line[: -len(_RELEASE)]
    if line not in NAMES:
        return None
    return line, is_press


def name_for_code(code: int) -> str | None:
    """The button name for an evdev key code, or None if it's not a remote button."""
    return BUTTONS.get(code)


class KeyRepeater:
    """Pure hold-to-repeat state machine (injectable clock).

    A press emits its command immediately. If the command is repeatable it arms
    a repeat: after ``delay`` seconds, then every ``interval`` seconds, until
    release. ``poll`` emits at most one repeat per call, so a late poll never
    catches up with a burst. A second repeatable press takes over the held slot
    (TV behaviour); non-repeatable presses don't disturb a held key.
    """

    def __init__(
        self,
        delay: float,
        interval: float,
        clock: Callable[[], float],
    ) -> None:
        self._delay = delay
        self._interval = interval
        self._clock = clock
        self._held: Command | None = None
        self._next = 0.0

    def press(self, command: Command, now: float) -> list[Command]:
        """Handle a press; return the commands to emit now (always just ``command``)."""
        if command in REPEATABLE:
            self._held = command
            self._next = now + self._delay
        return [command]

    def release(self, command: Command, now: float) -> None:
        """Handle a release; stops repeating only if ``command`` is the held key."""
        if command == self._held:
            self._held = None

    def poll(self, now: float) -> list[Command]:
        """Return at most one repeat command if a held key's interval elapsed."""
        if self._held is None or now < self._next:
            return []
        # Resync from ``now``: a late poll emits once, then the cadence resumes,
        # so a slow loop can't fire a burst to "catch up".
        self._next = now + self._interval
        return [self._held]

    def next_deadline(self) -> float | None:
        """Monotonic time the next repeat is due, or None if nothing is held."""
        return None if self._held is None else self._next

    def clear(self) -> None:
        """Drop any held key (e.g. the device vanished before the release arrived)."""
        self._held = None


class ButtonReader:
    """Read wire-format lines off a non-blocking file descriptor.

    Buffers partial lines (a write and its newline can arrive in separate reads)
    and yields only complete, recognized button events. ``read_available()``
    never blocks: it uses a zero-timeout ``select`` and a single ``os.read``.
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._buf = b""

    def read_available(self) -> list[tuple[str, bool]]:
        events: list[tuple[str, bool]] = []
        r, _, _ = select.select([self._fd], [], [], 0)
        if r:
            with contextlib.suppress(BlockingIOError):
                self._buf += os.read(self._fd, 4096)
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            ev = parse_line(line.decode("utf-8", "replace"))
            if ev is not None:
                events.append(ev)
        return events


class Remote:
    """The remote state machine: button events -> ``Command`` -> ``handle``.

    Pure: it takes a ``handle(command)`` callback, a repeat delay/interval, and
    a clock. It maps each parsed button event to its :class:`Command`, calls
    ``handle`` for presses (and releases, for repeatable keys), and — via
    :meth:`step` — emits repeats for a held channel key. It keeps no copy of the
    app's state; whether a command is ignored (NO SIGNAL, standby) is the app's
    decision in ``handle`` (SCOPE §4.4, §4.6).
    """

    def __init__(
        self,
        handle: Callable[[Command], None],
        clock: Callable[[], float],
        delay: float,
        interval: float,
    ) -> None:
        self._handle = handle
        self._clock = clock
        self._repeater = KeyRepeater(delay, interval, clock)

    def handle(self, name: str | None, is_press: bool = True) -> None:
        """Handle one parsed button event (a press or a release)."""
        if name is None:
            return
        command = COMMANDS.get(name)
        if command is None:
            # Reserved buttons (menu, home, screen-arrow): ignored but still
            # recognized so they can be logged (SCOPE §4.4).
            if is_press:
                _log.debug("remote: %s ignored (reserved)", name)
            return
        now = self._clock()
        if is_press:
            for c in self._repeater.press(command, now):
                self._handle(c)
        else:
            self._repeater.release(command, now)

    def step(self) -> None:
        """Call once per main-loop iteration: fire repeats for a held channel key."""
        for c in self._repeater.poll(self._clock()):
            self._handle(c)
