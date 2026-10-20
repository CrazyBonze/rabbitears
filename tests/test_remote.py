"""Unit tests for rabbitears.remote.

Covers the wire format (parse_line), the code->name map, the KeyRepeater
(repeat delay/interval, single-held, newer-takes-over), the ButtonReader (real
pipe, partial-line handling), and the Remote button->Command mapping (with a
fake handle callback). No I/O beyond a throwaway os.pipe for the ButtonReader.
"""

import os
import unittest

from rabbitears import remote as R
from rabbitears.commands import Command


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# --- parse_line / name_for_code -------------------------------------------

class ParseLineTest(unittest.TestCase):
    def test_every_button_name_round_trips(self):
        for name in R.NAMES:
            self.assertEqual(R.parse_line(name), (name, True))

    def test_release_suffix(self):
        for name in R.NAMES:
            self.assertEqual(R.parse_line(name + "~"), (name, False))

    def test_strips_whitespace(self):
        self.assertEqual(R.parse_line("  power  "), ("power", True))
        self.assertEqual(R.parse_line(" mute~ "), ("mute", False))

    def test_unknown_and_garbage(self):
        for bad in ("", "   ", "garbage", "999", "channel", "power~extra",
                    "POWER", "channel-up~x", None):
            self.assertIsNone(R.parse_line(bad), bad)

    def test_name_for_code(self):
        for code, name in R.BUTTONS.items():
            self.assertEqual(R.name_for_code(code), name)
        self.assertIsNone(R.name_for_code(999))
        self.assertIsNone(R.name_for_code(0))

    def test_button_to_command_map(self):
        self.assertIs(R.COMMANDS["channel-up"], Command.CHANNEL_UP)
        self.assertIs(R.COMMANDS["channel-down"], Command.CHANNEL_DOWN)
        self.assertIs(R.COMMANDS["vol-up"], Command.VOLUME_UP)
        self.assertIs(R.COMMANDS["vol-down"], Command.VOLUME_DOWN)
        self.assertIs(R.COMMANDS["mute"], Command.MUTE)
        self.assertIs(R.COMMANDS["power"], Command.POWER)
        self.assertIs(R.COMMANDS["ok"], Command.INFO)
        self.assertIs(R.COMMANDS["back"], Command.BACK)
        for name in ("arrow-left", "arrow-right", "menu", "home", "sun", "screen-arrow"):
            self.assertIsNone(R.COMMANDS[name])


# --- KeyRepeater -----------------------------------------------------------

class KeyRepeaterTest(unittest.TestCase):
    def _kr(self, delay=0.5, interval=0.4, clock=None):
        return R.KeyRepeater(delay, interval, clock or FakeClock())

    def test_press_emits_immediately(self):
        clock = FakeClock()
        kr = self._kr(clock=clock)
        self.assertEqual(kr.press(Command.CHANNEL_UP, 0.0), [Command.CHANNEL_UP])

    def test_no_repeat_for_non_repeatable(self):
        clock = FakeClock()
        kr = self._kr(clock=clock)
        kr.press(Command.MUTE, 0.0)
        self.assertEqual(kr.poll(10.0), [])

    def test_repeat_after_delay_then_every_interval(self):
        clock = FakeClock()
        kr = self._kr(delay=0.5, interval=0.4, clock=clock)
        kr.press(Command.CHANNEL_UP, 0.0)
        self.assertEqual(kr.poll(0.49), [])       # before the delay
        self.assertEqual(kr.poll(0.5), [Command.CHANNEL_UP])   # first repeat
        self.assertEqual(kr.poll(0.5), [])        # resync: not again yet
        self.assertEqual(kr.poll(0.9), [Command.CHANNEL_UP])   # +interval
        self.assertEqual(kr.poll(1.3), [Command.CHANNEL_UP])

    def test_release_stops(self):
        clock = FakeClock()
        kr = self._kr(clock=clock)
        kr.press(Command.CHANNEL_UP, 0.0)
        kr.release(Command.CHANNEL_UP, 1.0)
        self.assertEqual(kr.poll(2.0), [])

    def test_newer_held_key_takes_over(self):
        clock = FakeClock()
        kr = self._kr(clock=clock)
        kr.press(Command.CHANNEL_UP, 0.0)
        kr.press(Command.CHANNEL_DOWN, 0.1)  # newer takes over
        self.assertEqual(kr.poll(10.0), [Command.CHANNEL_DOWN])

    def test_late_poll_emits_at_most_one(self):
        clock = FakeClock()
        kr = self._kr(clock=clock)
        kr.press(Command.CHANNEL_UP, 0.0)
        # The loop is very late: 10s past, but it must not burst.
        self.assertEqual(kr.poll(10.0), [Command.CHANNEL_UP])

    def test_clear_stops_held_key(self):
        clock = FakeClock()
        kr = self._kr(clock=clock)
        kr.press(Command.CHANNEL_UP, 0.0)
        kr.clear()
        self.assertEqual(kr.poll(10.0), [])
        self.assertIsNone(kr.next_deadline())

    def test_next_deadline(self):
        clock = FakeClock()
        kr = self._kr(delay=0.5, interval=0.4, clock=clock)
        self.assertIsNone(kr.next_deadline())
        kr.press(Command.CHANNEL_UP, 0.0)
        self.assertAlmostEqual(kr.next_deadline(), 0.5)


# --- ButtonReader ----------------------------------------------------------

class ButtonReaderTest(unittest.TestCase):
    def _pipe(self):
        rfd, wfd = os.pipe()
        os.set_blocking(rfd, False)
        return rfd, wfd

    def test_reads_complete_lines(self):
        rfd, wfd = self._pipe()
        try:
            reader = R.ButtonReader(rfd)
            os.write(wfd, b"channel-up\npower\n")
            self.assertEqual(reader.read_available(),
                             [("channel-up", True), ("power", True)])
        finally:
            os.close(rfd)
            os.close(wfd)

    def test_buffers_partial_line(self):
        rfd, wfd = self._pipe()
        try:
            reader = R.ButtonReader(rfd)
            os.write(wfd, b"channel-do")      # no newline yet
            self.assertEqual(reader.read_available(), [])
            os.write(wfd, b"wn\n")            # completes "channel-down"
            self.assertEqual(reader.read_available(), [("channel-down", True)])
        finally:
            os.close(rfd)
            os.close(wfd)

    def test_drops_unrecognized_lines(self):
        rfd, wfd = self._pipe()
        try:
            reader = R.ButtonReader(rfd)
            os.write(wfd, b"garbage\npower\nnonsense~\n")
            self.assertEqual(reader.read_available(), [("power", True)])
        finally:
            os.close(rfd)
            os.close(wfd)

    def test_no_data_returns_empty(self):
        rfd, wfd = self._pipe()
        try:
            self.assertEqual(R.ButtonReader(rfd).read_available(), [])
        finally:
            os.close(rfd)
            os.close(wfd)


# --- The Remote (button -> Command) ---------------------------------------

class RemoteTest(unittest.TestCase):
    def _remote(self, clock=None):
        calls = []

        def handle(cmd):
            calls.append(cmd)

        return R.Remote(handle, clock or FakeClock(), delay=0.5, interval=0.4), calls

    def test_press_maps_to_command(self):
        remote, calls = self._remote()
        remote.handle("channel-up")
        remote.handle("vol-down")
        remote.handle("mute")
        remote.handle("power")
        remote.handle("ok")
        remote.handle("back")
        self.assertEqual(calls, [
            Command.CHANNEL_UP, Command.VOLUME_DOWN, Command.MUTE,
            Command.POWER, Command.INFO, Command.BACK,
        ])

    def test_reserved_buttons_not_forwarded(self):
        remote, calls = self._remote()
        for name in ("arrow-left", "arrow-right", "menu", "home", "sun", "screen-arrow"):
            remote.handle(name)
        self.assertEqual(calls, [])

    def test_unknown_name_not_forwarded(self):
        remote, calls = self._remote()
        remote.handle(None)
        remote.handle("bogus")
        self.assertEqual(calls, [])

    def test_held_channel_repeats(self):
        clock = FakeClock()
        remote, calls = self._remote(clock)
        remote.handle("channel-up")               # immediate
        self.assertEqual(calls, [Command.CHANNEL_UP])
        clock.advance(0.5)
        remote.step()
        self.assertEqual(calls, [Command.CHANNEL_UP, Command.CHANNEL_UP])
        clock.advance(0.4)
        remote.step()
        self.assertEqual(len(calls), 3)
        remote.handle("channel-up", is_press=False)  # release
        clock.advance(1.0)
        remote.step()
        self.assertEqual(len(calls), 3)               # no more after release

    def test_tap_does_not_repeat(self):
        clock = FakeClock()
        remote, calls = self._remote(clock)
        remote.handle("channel-up")
        remote.handle("channel-up", is_press=False)
        clock.advance(1.0)
        remote.step()
        self.assertEqual(calls, [Command.CHANNEL_UP])

    def test_non_repeatable_never_repeats(self):
        clock = FakeClock()
        remote, calls = self._remote(clock)
        remote.handle("mute")
        clock.advance(1.0)
        remote.step()
        self.assertEqual(calls, [Command.MUTE])


if __name__ == "__main__":
    unittest.main()
