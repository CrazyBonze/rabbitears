"""Rabbit Ears application core (SCOPE §4).

The long-running app: one mpv, a clock-based schedule, and a small state
machine that recovers from every failure mode without a shell, desktop, or
error dialog. Kept separate from ``__main__.py`` so it is unit-testable: the
player, clock, config, and ``media_present`` are all injected.

The two states are NO_SIGNAL (no media / no channels) and PLAYING, plus two
orthogonal viewer toggles: standby (video hidden, mpv still running) and the
info overlay (M4). Everything here keeps the box in one of the safe states
(SCOPE §1).

Input is the ``Command`` vocabulary (``commands.py``). ``handle(command,
source)`` is the single entry point every source (remote, control socket)
drives; it implements the state table and logs each command, its source, and
its outcome. The evdev daemon writes button lines to a FIFO; the app reads the
FIFO (``ButtonReader``) and maps buttons to ``Command`` via ``remote.Remote``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from . import channels as channels_mod
from . import index as index_mod
from . import schedule as schedule_mod
from . import state as state_mod
from .channels import Channel
from .commands import Command
from .config import Config
from .player import Player, PlayerError
from .remote import ButtonReader, Remote
from .state import State

_log = logging.getLogger("rabbitears")

# How often step() is called (seconds).
_STEP_INTERVAL = 0.1
# How often to re-scan for media while in NO_SIGNAL (seconds).
_MEDIA_POLL_INTERVAL = 5.0
# If mpv goes idle within this many seconds of a load that was not near the
# file's end, the file is treated as unplayable (SCOPE §4.7).
_UNPLAYABLE_TIMEOUT = 2.0
# How close to a file's end an offset must be to count as a normal EOF.
_NEAR_END = 2.0
# Minimum interval between state flushes (volume persistence; SCOPE §4.4).
_STATE_FLUSH_INTERVAL = 2.0


class PlayerLike(Protocol):
    """The subset of :class:`Player` the app drives (duck-typed for tests)."""

    def start(self) -> None: ...
    def set_volume(self, volume: int) -> None: ...
    def set_mute(self, muted: bool) -> None: ...
    def show_image(self, path: Path) -> None: ...
    def load(self, path: Path, offset: float = 0.0) -> None: ...
    def hide_video(self) -> None: ...
    def show_video(self) -> None: ...
    def is_idle(self) -> bool: ...
    def is_running(self) -> bool: ...
    def stop(self) -> None: ...


class AppCrash(Exception):
    """mpv died. The app exits 1 so supervise-daemon respawns it (SCOPE §4.7)."""


# --- Pure helpers (unit-tested) -------------------------------------------

def media_present(mount_point: Path) -> bool:
    """True if `mount_point` is a mounted drive with a `channels/` directory.

    Existence alone is not enough: on the box `/media/rabbitears` can be an
    empty directory with nothing mounted (RAM root).
    """
    try:
        if not os.path.ismount(str(mount_point)):
            return False
        return (mount_point / "channels").is_dir()
    except OSError:
        return False


def select_channel(channels: list[Channel], saved_number: int) -> Channel | None:
    """Return the saved channel if it still exists, else the lowest; None if none.

    SCOPE §4.8: tune to the last channel from state, or the lowest channel if
    there's no state.
    """
    if not channels:
        return None
    for ch in channels:
        if ch.number == saved_number:
            return ch
    return channels[0]


def tune_target(
    files: list[tuple[Path, float]], epoch: float, now: float
) -> tuple[Path, float]:
    """Return (path, offset) for the schedule position at time `now`.

    `files` is [(path, duration), ...] in playlist order. The position is
    recomputed from the wall clock (never advanced with a counter, SCOPE §4.2).
    """
    loop_length = sum(d for _, d in files)
    position = schedule_mod.compute_position(epoch, loop_length, now)
    return schedule_mod.find_file(files, position)


def channel_label(ch: Channel) -> str:
    """e.g. ``CH 03 · Magic School Bus`` for ``03-magic-school-bus``."""
    return f"CH {ch.number:02d} · {ch.name}"


def _fmt_duration(seconds: float) -> str:
    """Format seconds as ``<H>h<MM>m<SS>s`` (e.g. ``1h05m09s``)."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _count_mp4(channel_path: Path) -> int:
    try:
        return sum(1 for p in channel_path.iterdir() if p.is_file() and p.suffix == ".mp4")
    except OSError:
        return 0


# --- The app ---------------------------------------------------------------

class App:
    """Drives one mpv through the clock schedule and the failure states.

    ``step()`` is called every ~100 ms by :meth:`run`. The player, clock,
    config, and ``media_present`` are injected so the logic is testable.
    """

    def __init__(
        self,
        player: PlayerLike,
        clock: Callable[[], float],
        config: Config,
        media_present_fn: Callable[[Path], bool] | None = None,
        control_socket: Path | None = None,
    ) -> None:
        self.player = player
        self.clock = clock
        self.config = config
        self._media_present = media_present_fn or (lambda mp: media_present(mp))
        self._control_socket = control_socket
        self.state = State()  # in-memory; reloaded from the drive when present
        self._channels: list[Channel] = []
        self._current: Channel | None = None
        self._playlist: list[tuple[Path, float]] = []
        self._skip: set[Path] = set()  # per-session unplayable files
        self._mode = "NO_SIGNAL"
        self._last_media_poll = 0.0
        self._logged_waiting = False
        self._load_time = 0.0
        self._last_loaded_path: Path | None = None
        self._last_loaded_offset = 0.0
        self._stop = False
        # Viewer toggles (orthogonal to NO_SIGNAL/PLAYING).
        self._muted = False
        self._standby = False
        self._pre_standby_muted = False
        # Remote input (SCOPE §4.4): the app is the FIFO reader and the only
        # process that acts on a button. Disabled if the FIFO can't be opened.
        self._buttons_fd: int | None = None
        self._reader: ButtonReader | None = None
        self._remote: Remote | None = None
        # Volume persistence throttle (SCOPE §4.4: flush at most once per 2 s).
        self._state_dirty = False
        self._last_state_flush = 0.0
        # The local control socket (commands + read-only status), if enabled.
        self._control = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Startup (SCOPE §4.8): start mpv, set volume, show NO SIGNAL."""
        self.player.start()
        self.player.set_volume(min(self.state.volume, self.config.volume_cap))
        # Show the test pattern immediately so the screen is NO SIGNAL, not a
        # console, while we wait for media or index.
        self.player.show_image(self.config.testpattern_path)
        self._setup_buttons()
        self._setup_control()
        self._enter_no_signal()

    def _setup_control(self) -> None:
        """Create the local control socket if one was requested (real runtime)."""
        if self._control_socket is None:
            return
        from .control import ControlServer

        control = ControlServer(self._control_socket, self.handle, self.status)
        try:
            control.start()
        except OSError as e:
            _log.warning("control socket unavailable: %s — control disabled", e)
            return
        self._control = control

    def _setup_buttons(self) -> None:
        """Create the button FIFO (we're the reader) and wire the Remote.

        The evdev daemon (box) or the e2e test double (VM) is the writer. If the
        FIFO can't be created/opened (e.g. no permission in a test), the remote
        is disabled and the app still runs.
        """
        path = self.config.buttons_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                os.mkfifo(path)
            self._buttons_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            _log.warning("buttons FIFO unavailable: %s — remote disabled", e)
            return
        self._reader = ButtonReader(self._buttons_fd)
        self._remote = Remote(
            handle=self.handle,
            clock=self.clock,
            delay=self.config.repeat_delay,
            interval=self.config.repeat_interval,
        )

    def run(self) -> int:
        """Install signal handlers, start, and run the step loop. Returns an exit code."""
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self.start()
        while not self._stop:
            try:
                self.step()
            except AppCrash as e:
                _log.error("mpv crashed: %s — exiting for respawn", e)
                self._shutdown(save=False)
                return 1
            except PlayerError as e:
                _log.error("player error: %s — exiting for respawn", e)
                self._shutdown(save=False)
                return 1
            time.sleep(_STEP_INTERVAL)
        self._shutdown(save=True)
        return 0

    def _on_signal(self, signum: int, frame) -> None:
        _log.info("received signal %s — shutting down", signum)
        self._stop = True

    def _shutdown(self, save: bool) -> None:
        if save and self._media_present(self.config.mount_point):
            self._save_state()
        if self._control is not None:
            with contextlib.suppress(OSError):
                self._control.close()
            self._control = None
        if self._buttons_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._buttons_fd)
            self._buttons_fd = None
        with contextlib.suppress(PlayerError, OSError, Exception):
            self.player.stop()

    # -- the step loop ------------------------------------------------------

    def step(self) -> None:
        """One iteration of the main loop (called every ~100 ms)."""
        if not self.player.is_running():
            raise AppCrash("mpv exited")
        self._step_remote()
        if self._control is not None:
            self._control.serve()
        now = self.clock()
        self._flush_state(now)
        if self._mode == "NO_SIGNAL":
            self._step_no_signal(now)
        else:
            self._step_playing(now)

    def _step_remote(self) -> None:
        """Read remote button events and feed the Remote (commands + repeat)."""
        if self._reader is None or self._remote is None:
            return
        for name, is_press in self._reader.read_available():
            self._remote.handle(name, is_press)
        self._remote.step()

    def _flush_state(self, now: float) -> None:
        """Flush a dirty state at most once per ``_STATE_FLUSH_INTERVAL``."""
        if self._state_dirty and now - self._last_state_flush >= _STATE_FLUSH_INTERVAL:
            self._save_state()

    def _step_no_signal(self, now: float) -> None:
        if now - self._last_media_poll < _MEDIA_POLL_INTERVAL:
            return
        self._last_media_poll = now
        if not self._media_present(self.config.mount_point):
            if not self._logged_waiting:
                _log.info("waiting for media at %s", self.config.mount_point)
                self._logged_waiting = True
            return
        self._logged_waiting = False
        self._try_tune()

    def _step_playing(self, now: float) -> None:
        # Drive pulled: show NO SIGNAL and drop to that state. Detection happens
        # on the next step (~100 ms after the lazy unmount).
        if not self._media_present(self.config.mount_point):
            self.player.show_image(self.config.testpattern_path)
            _log.warning("media removed — NO SIGNAL")
            self._enter_no_signal()
            return
        if self.player.is_idle():
            since_load = now - self._load_time
            if since_load < _UNPLAYABLE_TIMEOUT and not self._offset_near_end():
                self._handle_unplayable()
                return
            # Normal end-of-file: recompute from the clock (never a counter).
            self._tune()

    # -- command handling (the Part 1b state table) ------------------------

    def handle(self, command: Command, source: str = "remote") -> None:
        """Perform one ``Command``; logs the command, its source, and the outcome.

        This is the single action entry point (SCOPE §4.4): every input source
        funnels into it, so the source is swappable and the surface is closed
        (nothing but a ``Command`` plus a read-only ``status``).
        """
        if self._standby and command is not Command.POWER:
            _log.info("%s: %s ignored (standby)", source, command.value)
            return
        if command is Command.POWER:
            self._power(source)
        elif command in (Command.CHANNEL_UP, Command.CHANNEL_DOWN):
            if self._mode != "PLAYING":
                _log.info("%s: %s ignored (no signal)", source, command.value)
                return
            self._zap(
                1 if command is Command.CHANNEL_UP else -1, source, command
            )
        elif command in (Command.VOLUME_UP, Command.VOLUME_DOWN):
            step = self.config.volume_step
            self._volume(step if command is Command.VOLUME_UP else -step, source, command)
        elif command is Command.MUTE:
            self._mute(source)
        else:  # INFO / BACK (M4): overlay isn't built yet.
            _log.info("%s: %s ignored (overlay is M4)", source, command.value)

    def _power(self, source: str) -> None:
        """Toggle standby (SCOPE §4.6): hide video + mute; wake re-tunes."""
        if self._standby:
            self._standby = False
            self.player.show_video()
            self.player.set_mute(self._muted)
            if self._mode == "PLAYING":
                # Re-tune at the *current* schedule position: time kept passing
                # while "off" (SCOPE §4.6).
                self._tune()
            else:
                self.player.show_image(self.config.testpattern_path)
            _log.info("%s: power on", source)
        else:
            self._standby = True
            self._pre_standby_muted = self._muted
            self.player.hide_video()
            self.player.set_mute(True)
            _log.info("%s: power standby", source)

    def _volume(self, delta: int, source: str, command: Command) -> None:
        new = max(0, min(self.config.volume_cap, self.state.volume + delta))
        if new != self.state.volume:
            self.state.volume = new
            self.player.set_volume(new)
            self._state_dirty = True
        # A volume press unmutes, like a real TV (SCOPE §4.4).
        if self._muted:
            self._muted = False
            self.player.set_mute(False)
        _log.info(
            "%s: %s -> %d (cap %d)", source, command.value, new, self.config.volume_cap
        )

    def _mute(self, source: str) -> None:
        self._muted = not self._muted
        self.player.set_mute(self._muted)
        _log.info("%s: mute %s", source, "on" if self._muted else "off")

    def _zap(self, direction: int, source: str, command: Command) -> None:
        """Tune to the next/prev playable channel in `direction`, skipping empties.

        Steps at most ``len(channels)`` times (SCOPE §4.4: a dead channel is
        skipped, not shown black). If no channel is playable, drops to NO SIGNAL.
        """
        if not self._channels:
            _log.info("%s: %s ignored (no channels)", source, command.value)
            return
        idx = 0
        for i, ch in enumerate(self._channels):
            if ch.number == self.state.channel:
                idx = i
                break
        n = len(self._channels)
        mount_index = self.config.mount_point / ".rabbitears" / "index.json"
        for step in range(1, n + 1):
            ch = self._channels[(idx + direction * step) % n]
            durations = index_mod.get_durations(ch.path, mount_index)
            if durations:
                self._current = ch
                self._playlist = durations
                self._tune()
                _log.info("%s: %s -> %s", source, command.value, channel_label(ch))
                return
            _log.info("%s: %s skipped %s (no playable files)", source, command.value, channel_label(ch))
        # Nothing playable anywhere: NO SIGNAL (SCOPE §4.7).
        _log.warning("%s: %s no playable channels", source, command.value)
        self.player.show_image(self.config.testpattern_path)
        self._enter_no_signal()

    # -- tuning -------------------------------------------------------------

    def _try_tune(self) -> None:
        """Scan, select, index, and tune. Stays in NO_SIGNAL if nothing playable."""
        mount = self.config.mount_point
        # Load state once media is present, and apply the saved volume.
        self.state = state_mod.load_state(mount / ".rabbitears" / "state.json")
        self.player.set_volume(min(self.state.volume, self.config.volume_cap))

        self._channels = channels_mod.scan_channels(mount / "channels")
        ch = select_channel(self._channels, self.state.channel)
        if ch is None:
            return  # no channels; stay in NO_SIGNAL

        label = channel_label(ch)
        total_files = _count_mp4(ch.path)
        _log.info("indexing %s (%d files)...", label, total_files)
        t0 = self.clock()
        durations = index_mod.get_durations(ch.path, mount / ".rabbitears" / "index.json")
        dt = self.clock() - t0
        playable = len(durations)
        skipped = total_files - playable
        loop = sum(d for _, d in durations)
        _log.info(
            "CH %02d: %d playable, %d skipped, loop %s (indexed in %.1fs)",
            ch.number, playable, skipped, _fmt_duration(loop), dt,
        )
        if not durations:
            return  # nothing playable; stay in NO_SIGNAL

        self._current = ch
        self._playlist = durations
        self._tune()

    def _tune(self) -> None:
        """Load the schedule-correct file at the schedule-correct offset."""
        assert self._current is not None and self._playlist
        ch = self._current
        loop = sum(d for _, d in self._playlist)
        now = self.clock()
        position = schedule_mod.compute_position(self.config.epoch, loop, now)
        path, offset = schedule_mod.find_file(self._playlist, position)
        self.player.load(path, offset)
        if self._standby:
            # mpv may re-enable video on loadfile; keep it hidden while off.
            self.player.hide_video()
        self._load_time = now
        self._last_loaded_path = path
        self._last_loaded_offset = offset
        self.state.channel = ch.number
        self._save_state()
        self._mode = "PLAYING"
        _log.info(
            "%s — %s +%dm%02ds (position %.0fs / loop %.0fs)",
            channel_label(ch), path.name, int(offset // 60), int(offset % 60),
            position, loop,
        )

    def _save_state(self) -> None:
        state_mod.save_state(
            self.config.mount_point / ".rabbitears" / "state.json", self.state
        )
        self._state_dirty = False
        self._last_state_flush = self.clock()

    def status(self) -> dict:
        """A read-only snapshot of in-memory app state (no IPC to mpv)."""
        return {
            "state": "playing" if self._mode == "PLAYING" else "no_signal",
            "standby": self._standby,
            "channel": self._current.number if self._current is not None else None,
            "channel_name": self._current.name if self._current is not None else None,
            "file": self._last_loaded_path.name if self._last_loaded_path is not None else None,
            "volume": self.state.volume,
            "muted": self._muted,
        }

    # -- unplayable-file handling ------------------------------------------

    def _offset_near_end(self) -> bool:
        """Was the offset we loaded near the end of that file? (=> normal EOF)"""
        if self._last_loaded_path is None:
            return False
        for path, dur in self._playlist:
            if path == self._last_loaded_path:
                return self._last_loaded_offset >= dur - _NEAR_END
        return False

    def _handle_unplayable(self) -> None:
        path = self._last_loaded_path
        _log.warning("unplayable file: %s — skipping", path.name if path else "?")
        if path is not None:
            self._skip.add(path)
        old_loop = sum(d for _, d in self._playlist)
        self._playlist = [(p, d) for (p, d) in self._playlist if p not in self._skip]
        new_loop = sum(d for _, d in self._playlist)
        _log.info("playlist loop changed: %.0fs -> %.0fs", old_loop, new_loop)
        if not self._playlist:
            self._enter_no_signal()
            return
        self._tune()

    # -- state transitions --------------------------------------------------

    def _enter_no_signal(self) -> None:
        self._mode = "NO_SIGNAL"
        self._current = None
        self._playlist = []
        self._last_loaded_path = None
        self._last_loaded_offset = 0.0
        self._logged_waiting = False
        self._last_media_poll = 0.0  # poll immediately on the next step


def main() -> int:
    """Entry point: wire up the real player/clock/config and run. Returns an exit code."""
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="[rabbitears] %(message)s"
    )
    from .config import load_config

    config = load_config()
    player = Player(config.socket_path)
    app = App(
        player=player, clock=time.time, config=config,
        control_socket=config.control_socket,
    )
    return app.run()
