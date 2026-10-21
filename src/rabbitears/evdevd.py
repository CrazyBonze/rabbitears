"""The evdev remote daemon (box side) — a small, separate process.

Finds the Adomax remote's evdev nodes (by USB vendor ``0x0627``, no udev/by-id
dependency), grabs them exclusively, reads ``EV_KEY`` events, and writes one line
per button to the FIFO the app reads (SCOPE §4.4). It never touches mpv — the app
is the only process that acts on a button.

The daemon tracks which buttons are held and, when a device vanishes, writes a
release line for each held button. Otherwise a kid who is holding a channel key
when the dongle is yanked would leave the app's repeater firing forever (the
release never arrives).

Box side: stdlib + ``py3-evdev`` (an apk package). The pure event->line mapping
(:func:`event_to_line`) and the daemon's device management are unit-tested
without evdev: the device ``discover`` factory, the ``wall``/``monotonic``
clocks, ``sleep``, and the ``log`` are all injected into :class:`RemoteDaemon`,
and the ``evdev`` import stays lazy inside :func:`find_remote_devices`, so
importing this module (and testing the rest) needs no evdev.

Run as an OpenRC service (``supervise-daemon``): ``python3 -m rabbitears.evdevd``.
If the app (the FIFO reader) is gone, opening the FIFO for writing fails and this
exits; the supervisor restarts it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import sys
import time
from collections.abc import Callable

from .remote import name_for_code

_log = logging.getLogger("rabbitears.evdevd")

# The Adomax 2.4GHz remote's USB vendor ID (SCOPE §4.4, docs/remote.md §1). The real
# value comes from config.remote_vendor; this is the documented default.
VENDOR = 0x0627
# input-event type for a key (evdev.ecodes.EV_KEY); kept literal so the pure
# event mapping is testable without importing evdev.
EV_KEY = 1
# A press older than this many seconds (vs wall clock) is dropped, so a kid
# mashing buttons during a slow index doesn't burst once the loop catches up.
STALE_AGE = 1.0
# How often to rescan /dev/input for a (re)plugged dongle (SCOPE §4.4).
RESCAN_INTERVAL = 3.0


def event_press_release(ev_type: int, code: int, value: int) -> tuple[str, bool] | None:
    """``(name, is_press)`` for a recognized EV_KEY press/release, else None.

    Repeats (value 2) are dropped: the remote does not autorepeat, and repeat
    is the app's job (SCOPE §4.4).
    """
    if ev_type != EV_KEY or value not in (0, 1):
        return None
    name = name_for_code(code)
    if name is None:
        return None
    return name, value == 1


def event_to_line(ev_type: int, code: int, value: int) -> str | None:
    """The wire-format line for one evdev event, or None to drop it.

    A press yields the button name; a release yields ``name~``.
    """
    pr = event_press_release(ev_type, code, value)
    if pr is None:
        return None
    name, is_press = pr
    return name if is_press else name + "~"


def is_stale(timestamp: float, wall: float, max_age: float = STALE_AGE) -> bool:
    """True if an event timestamp is more than ``max_age`` older than ``wall``.

    Future timestamps (clock skew) are accepted, not treated as stale.
    """
    return wall - timestamp > max_age


def find_remote_devices(vendor: int = VENDOR) -> list:
    """Return the remote's InputDevices (USB vendor ``vendor``). Lazy evdev import.

    The remote exposes several interfaces (keyboard, mouse, system control); all
    share the vendor ID, so all are returned and filtered by code downstream.
    """
    # py3-evdev is a box-side apk package (not a host dependency); the LSP
    # can't resolve it here, but it is present where this code runs.
    from evdev import InputDevice, list_devices  # type: ignore[import-unresolved]

    devices = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if dev.info.vendor == vendor:
            devices.append(dev)
    return devices


class RemoteDaemon:
    """Read the remote's evdev nodes and write button lines to the FIFO.

    The per-iteration logic (:meth:`tick`) is separated from the infinite
    :func:`run` loop so it is unit-testable without evdev: the device
    ``discover`` factory, the ``wall``/``monotonic`` clocks, ``sleep``, and the
    ``log`` are all injected. Device objects are duck-typed (``.fd``, ``.grab()``,
    ``.read_loop()``, ``.close()``), so tests pass fakes.
    """

    def __init__(
        self,
        fifo: int,
        vendor: int,
        rescan_interval: float,
        discover: Callable[[int], list],
        wall: Callable[[], float],
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        log: logging.Logger,
    ) -> None:
        self._fifo = fifo
        self._vendor = vendor
        self._rescan_interval = rescan_interval
        self._discover = discover
        self._wall = wall
        self._monotonic = monotonic
        self._sleep = sleep
        self._log = log
        self._devices: dict = {}   # fd -> device
        self._held: dict = {}      # fd -> set of held button names
        # -inf forces an immediate rescan on the first tick.
        self._last_rescan = float("-inf")
        self._remote_present = False
        self._stale_drops = 0

    # -- device management --------------------------------------------------

    def rescan(self) -> None:
        """Update the device set for hot-plug: close gone nodes, grab new ones."""
        fresh = self._discover(self._vendor)
        fresh_fds = {d.fd for d in fresh}
        for fd in list(self._devices):
            if fd not in fresh_fds:
                self._drop_device(fd)
        for dev in fresh:
            if dev.fd not in self._devices:
                self._open_device(dev)
        self._log_transition()

    def _open_device(self, dev) -> None:
        """Grab a newly found device and start reading it (grab failure is not fatal)."""
        try:
            dev.grab()
        except OSError:
            # A failed grab means keys also reach the VT console / acpid, but we
            # keep reading (SCOPE §4.4: grab failure is logged, not fatal).
            self._log.warning(
                "grab failed for %s — reading without exclusive access",
                getattr(dev, "path", "?"),
            )
        self._devices[dev.fd] = dev
        self._held.setdefault(dev.fd, set())
        info = getattr(dev, "info", None)
        self._log.info(
            "opened remote device %s (%s, %04x:%04x)",
            getattr(dev, "path", "?"),
            getattr(info, "name", "?"),
            getattr(info, "vendor", 0) or 0,
            getattr(info, "product", 0) or 0,
        )

    def _drop_device(self, fd: int) -> None:
        """Close a vanished device and release every button it still held."""
        dev = self._devices.pop(fd, None)
        for name in self._held.pop(fd, set()):
            self._write(name + "~")
        self._log.info("device lost (fd %d) — released held buttons", fd)
        if dev is not None:
            with contextlib.suppress(OSError):
                dev.close()

    def _log_transition(self) -> None:
        """Log remote present/absent once per transition, not every scan."""
        present = bool(self._devices)
        if present == self._remote_present:
            return
        self._remote_present = present
        if present:
            self._log.info("remote present")
        else:
            self._log.info("no remote found")

    # -- the per-iteration step ---------------------------------------------

    def tick(self) -> None:
        """One iteration of the daemon loop: rescan if due, then read events."""
        now = self._monotonic()
        if now - self._last_rescan >= self._rescan_interval:
            self.rescan()
            self._last_rescan = now
        if not self._devices:
            self._sleep(0.5)  # no remote yet; wait for the next rescan
            return
        ready, _, _ = select.select(list(self._devices), [], [], 0.5)
        wall = self._wall()
        for fd in ready:
            dev = self._devices.get(fd)
            if dev is None:
                continue
            try:
                events = dev.read_loop()
            except OSError:
                self._drop_device(fd)
                continue
            for ev in events:
                pr = event_press_release(ev.type, ev.code, ev.value)
                if pr is None:
                    continue
                name, is_press = pr
                if is_press and is_stale(ev.timestamp(), wall):
                    self._stale_drops += 1
                    if self._stale_drops == 1:
                        self._log.info("dropping stale button events (loop was slow)")
                    continue
                hset = self._held.setdefault(fd, set())
                if is_press:
                    hset.add(name)
                    self._write(name)
                else:
                    hset.discard(name)
                    self._write(name + "~")

    def _write(self, line: str) -> None:
        """Write one button line; a dropped write (reader slow/gone) is not fatal."""
        with contextlib.suppress(OSError):
            os.write(self._fifo, (line + "\n").encode())

    def close(self) -> None:
        """Ungrab/close every device (best effort)."""
        for dev in list(self._devices.values()):
            with contextlib.suppress(OSError):
                dev.close()
        self._devices.clear()
        self._held.clear()


def run(
    fifo_path: str,
    vendor: int = VENDOR,
    rescan_interval: float = RESCAN_INTERVAL,
    discover: Callable[[int], list] | None = None,
    wall: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Main loop: read the remote, write button lines to the FIFO.

    The app creates the FIFO and is the reader. Opening it ``O_WRONLY|O_NONBLOCK``
    fails immediately (ENXIO) if there is no reader, so a dead app makes this
    exit for the supervisor to restart.
    """
    try:
        fifo = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return  # no reader (app not running); exit for respawn
    daemon = RemoteDaemon(
        fifo, vendor, rescan_interval,
        discover or find_remote_devices, wall, monotonic, sleep, _log,
    )
    try:
        while True:
            daemon.tick()
    finally:
        daemon.close()
        os.close(fifo)


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the daemon against the configured FIFO path and vendor."""
    from .config import load_config

    args = argv if argv is not None else sys.argv[1:]
    cfg = load_config()
    fifo_path = args[0] if args else str(cfg.buttons_path)
    run(fifo_path, vendor=cfg.remote_vendor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
