"""Unit tests for rabbitears.evdevd.

No real evdev and no real FIFO blocking: the pure event->line mapping, the
press/release classification, and the stale-press check are tested directly, and
the daemon's device management (discovery, grab, hot-plug rescan, device loss,
stale drops, "no remote found" transition) is tested through :class:`RemoteDaemon`
with injected fakes. The ``evdev`` import in evdevd is lazy, so importing the
module needs no evdev package.
"""

import contextlib
import logging
import os
import select
import unittest

from rabbitears import evdevd
from rabbitears import remote as R


class EventToLineTest(unittest.TestCase):
    def test_press_yields_name(self):
        self.assertEqual(evdevd.event_to_line(1, 103, 1), "channel-up")
        self.assertEqual(evdevd.event_to_line(1, 116, 1), "power")
        self.assertEqual(evdevd.event_to_line(1, 113, 1), "mute")

    def test_release_yields_name_with_suffix(self):
        self.assertEqual(evdevd.event_to_line(1, 103, 0), "channel-up~")
        self.assertEqual(evdevd.event_to_line(1, 116, 0), "power~")

    def test_every_button_code(self):
        for code, name in R.BUTTONS.items():
            self.assertEqual(evdevd.event_to_line(1, code, 1), name)
            self.assertEqual(evdevd.event_to_line(1, code, 0), name + "~")

    def test_repeat_is_dropped(self):
        # The remote doesn't autorepeat; repeat is the app's job.
        self.assertIsNone(evdevd.event_to_line(1, 103, 2))

    def test_non_key_event_is_dropped(self):
        self.assertIsNone(evdevd.event_to_line(0, 103, 1))  # EV_SYN
        self.assertIsNone(evdevd.event_to_line(2, 103, 1))  # EV_REL
        self.assertIsNone(evdevd.event_to_line(3, 103, 1))  # EV_ABS

    def test_unknown_code_is_dropped(self):
        self.assertIsNone(evdevd.event_to_line(1, 999, 1))
        self.assertIsNone(evdevd.event_to_line(1, 272, 1))  # a mouse button
        self.assertIsNone(evdevd.event_to_line(1, 0, 1))


class EventPressReleaseTest(unittest.TestCase):
    def test_press_and_release(self):
        self.assertEqual(evdevd.event_press_release(1, 103, 1), ("channel-up", True))
        self.assertEqual(evdevd.event_press_release(1, 103, 0), ("channel-up", False))

    def test_repeat_and_unknown_return_none(self):
        self.assertIsNone(evdevd.event_press_release(1, 103, 2))  # repeat
        self.assertIsNone(evdevd.event_press_release(0, 103, 1))  # not a key
        self.assertIsNone(evdevd.event_press_release(1, 999, 1))  # unknown code


class StalePressTest(unittest.TestCase):
    def test_old_press_is_stale(self):
        self.assertTrue(evdevd.is_stale(100.0, 101.5))

    def test_recent_and_future_are_not_stale(self):
        self.assertFalse(evdevd.is_stale(101.0, 101.5))
        self.assertFalse(evdevd.is_stale(102.0, 101.5))  # future timestamp


# --- Fakes -----------------------------------------------------------------

class FakeEvent:
    def __init__(self, ev_type, code, value, ts=100.0):
        self.type = ev_type
        self.code = code
        self.value = value
        self._ts = ts

    def timestamp(self):
        return self._ts


class FakeInfo:
    def __init__(self, vendor=0x0627, product=0x697d, name="Adomax Remote"):
        self.vendor = vendor
        self.product = product
        self.name = name


class FakeDev:
    """A duck-typed evdev device: a real pipe fd plus injectable behaviour."""

    def __init__(self, path="/dev/input/event7", info=None, events=None,
                 read_error=False, grab_error=False):
        rfd, wfd = os.pipe()
        self._rfd = rfd
        self._wfd = wfd
        self.path = path
        self.info = info or FakeInfo()
        self._events = list(events or [])
        self._read_error = read_error
        self._grab_error = grab_error
        self.grabbed = False
        self.closed = False

    @property
    def fd(self):
        return self._rfd

    def grab(self):
        if self._grab_error:
            raise OSError("grab failed")
        self.grabbed = True

    def read_loop(self):
        if self._read_error:
            raise OSError("device gone")
        evs = self._events
        self._events = []
        return evs

    def close(self):
        self.closed = True
        os.close(self._rfd)
        os.close(self._wfd)

    # test helpers
    def push(self, *events):
        self._events.extend(events)

    def write_ready(self):
        """Make the fd readable so select() returns without a 0.5 s block."""
        os.write(self._wfd, b"\x00")

    def drain(self):
        with contextlib.suppress(OSError):
            os.read(self._rfd, 4096)


class FakeClock:
    # Starts at 100.0 to match FakeEvent's default timestamp, so ordinary
    # (non-stale) test events are not >1.0 s old relative to the wall clock.
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class LogCapture:
    """A handler that records log messages so tests can inspect them incrementally.

    Unlike assertLogs (whose output is only available on exit), this lets a test
    assert on the log after each individual call.
    """

    def __init__(self, name="test.evdevd", level=logging.INFO):
        self.records = []
        self.logger = logging.getLogger(name)
        self._old_level = self.logger.level

        def emit(record):
            self.records.append(record)

        self._handler = logging.Handler()
        self._handler.emit = emit
        self.logger.setLevel(level)
        self.logger.addHandler(self._handler)

    def close(self):
        self.logger.removeHandler(self._handler)
        self.logger.setLevel(self._old_level)

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


class DaemonTestBase(unittest.TestCase):
    """Build a RemoteDaemon against a real FIFO, with injected fakes."""

    def _make(self, discover, rescan_interval=3.0):
        self._fifo_r, self._fifo_w = os.pipe()
        # The daemon writes to the FIFO; we read it back on the _fifo_r end.
        # os.pipe gives a real fd the daemon can os.write() to.
        self.clock = FakeClock()
        self.sleeps = []
        # NOTE: do not touch log.handlers here — a test may call _make inside an
        # assertLogs(...) block, and replacing the handlers list would drop the
        # capture handler assertLogs just attached.
        log = logging.getLogger("test.evdevd")

        daemon = evdevd.RemoteDaemon(
            fifo=self._fifo_w,
            vendor=0x0627,
            rescan_interval=rescan_interval,
            discover=discover,
            wall=lambda: self.clock.t,
            monotonic=lambda: self.clock.t,
            sleep=self.sleeps.append,
            log=log,
        )
        self.addCleanup(self._close_fifo)
        self.addCleanup(daemon.close)
        return daemon

    def _close_fifo(self):
        for fd in (self._fifo_r, self._fifo_w):
            with contextlib.suppress(OSError):
                os.close(fd)

    def _fifo_lines(self):
        """Read all currently-available FIFO lines without blocking.

        The daemon holds the write end open, so a plain os.read would block
        forever once the buffer drains; a zero-timeout select guards against that.
        """
        out = []
        while True:
            r, _, _ = select.select([self._fifo_r], [], [], 0)
            if not r:
                break
            chunk = os.read(self._fifo_r, 4096)
            if not chunk:
                break
            out.extend(chunk.decode().split())
        return out


class RescanTest(DaemonTestBase):
    def test_opens_only_matching_devices_all_of_them(self):
        # discover returns two devices (the dongle splits across interfaces).
        d1, d2 = FakeDev(), FakeDev()
        calls = []

        def discover(vendor):
            calls.append(vendor)
            return [d1, d2]

        daemon = self._make(discover)
        daemon.rescan()
        self.assertEqual(calls, [0x0627])  # vendor passed through
        self.assertEqual(set(daemon._devices), {d1.fd, d2.fd})
        self.assertTrue(d1.grabbed and d2.grabbed)

    def test_grab_failure_logged_and_reading_continues(self):
        d = FakeDev(grab_error=True)
        d.push(FakeEvent(1, 103, 1))  # channel-up press
        d.write_ready()
        cap = LogCapture(level=logging.WARNING)
        self.addCleanup(cap.close)

        daemon = self._make(lambda v: [d])
        daemon.rescan()
        self.assertIn("grab failed", cap.text)
        # The device is still tracked and its event is read.
        self.assertIn(d.fd, daemon._devices)
        daemon.tick()
        self.assertEqual(self._fifo_lines(), ["channel-up"])

    def test_rescan_picks_up_replugged_device(self):
        d = FakeDev()
        state = {"devs": []}

        def discover(v):
            return state["devs"]

        daemon = self._make(discover)
        daemon.rescan()
        self.assertEqual(daemon._devices, {})  # nothing plugged yet

        state["devs"] = [d]
        daemon.rescan()
        self.assertEqual(set(daemon._devices), {d.fd})
        self.assertTrue(d.grabbed)

    def test_rescan_drops_unplugged_device(self):
        d = FakeDev()
        state = {"devs": [d]}
        daemon = self._make(lambda v: state["devs"])
        daemon.rescan()
        self.assertIn(d.fd, daemon._devices)

        state["devs"] = []  # dongle yanked
        daemon.rescan()
        self.assertNotIn(d.fd, daemon._devices)
        self.assertTrue(d.closed)


class TransitionLogTest(DaemonTestBase):
    def test_no_remote_found_logged_once_per_transition(self):
        d = FakeDev()
        state = {"devs": []}
        cap = LogCapture()
        self.addCleanup(cap.close)
        daemon = self._make(lambda v: state["devs"])

        daemon.rescan()  # absent -> absent: no transition log
        self.assertNotIn("no remote found", cap.text)
        self.assertNotIn("remote present", cap.text)

        state["devs"] = [d]
        daemon.rescan()  # absent -> present
        self.assertIn("remote present", cap.text)

        state["devs"] = [d]
        daemon.rescan()  # present -> present: no repeat
        self.assertEqual(cap.text.count("remote present"), 1)

        state["devs"] = []
        daemon.rescan()  # present -> absent
        self.assertIn("no remote found", cap.text)

    def test_opened_device_logged_with_vendor(self):
        d = FakeDev(info=FakeInfo(vendor=0x0627, product=0x697d, name="Adomax"))
        with self.assertLogs("test.evdevd", level="INFO") as cm:
            daemon = self._make(lambda v: [d])
            daemon.rescan()
        joined = "\n".join(cm.output)
        self.assertIn("opened remote device", joined)
        self.assertIn("0627:697d", joined)


class ReadLoopTest(DaemonTestBase):
    def test_press_and_release_written(self):
        d = FakeDev()
        d.push(FakeEvent(1, 103, 1))
        d.write_ready()
        daemon = self._make(lambda v: [d])
        daemon.rescan()
        daemon.tick()
        self.assertEqual(self._fifo_lines(), ["channel-up"])

        d.push(FakeEvent(1, 103, 0))
        d.write_ready()
        daemon.tick()
        self.assertEqual(self._fifo_lines(), ["channel-up~"])

    def test_unmapped_and_non_key_events_ignored(self):
        d = FakeDev()
        d.push(FakeEvent(1, 999, 1))   # unknown code
        d.push(FakeEvent(0, 103, 1))   # EV_SYN, not a key
        d.push(FakeEvent(1, 103, 2))   # autorepeat, dropped
        d.write_ready()
        daemon = self._make(lambda v: [d])
        daemon.rescan()
        daemon.tick()
        self.assertEqual(self._fifo_lines(), [])

    def test_stale_press_dropped(self):
        d = FakeDev()
        # wall is 100.0; a press timestamped 98.0 is >1.0 s old.
        d.push(FakeEvent(1, 103, 1, ts=98.0))
        d.write_ready()
        with self.assertLogs("test.evdevd", level="INFO") as cm:
            daemon = self._make(lambda v: [d])
            daemon.rescan()
            daemon.tick()
        self.assertEqual(self._fifo_lines(), [])  # dropped, not written
        self.assertIn("stale", "\n".join(cm.output))

    def test_device_oserror_drops_and_releases_held(self):
        d = FakeDev()
        daemon = self._make(lambda v: [d])
        daemon.rescan()
        # Hold a button: press channel-up.
        d.push(FakeEvent(1, 103, 1))
        d.write_ready()
        daemon.tick()
        self.assertEqual(self._fifo_lines(), ["channel-up"])
        self.assertEqual(daemon._held.get(d.fd), {"channel-up"})

        # Now the device vanishes mid-read.
        d._read_error = True
        d.write_ready()
        daemon.tick()
        self.assertNotIn(d.fd, daemon._devices)
        self.assertTrue(d.closed)
        # The held button was released so the app's repeater stops.
        self.assertEqual(self._fifo_lines(), ["channel-up~"])


if __name__ == "__main__":
    unittest.main()
