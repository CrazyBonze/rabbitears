"""Unit tests for rabbitears.commands."""

import unittest

from rabbitears.commands import Command, parse_command


class ParseCommandTest(unittest.TestCase):
    def test_every_value_round_trips(self):
        for cmd in Command:
            self.assertIs(parse_command(cmd.value), cmd)

    def test_case_insensitive(self):
        self.assertIs(parse_command("CHANNEL_UP"), Command.CHANNEL_UP)
        self.assertIs(parse_command("Channel_Up"), Command.CHANNEL_UP)
        self.assertIs(parse_command("MUTE"), Command.MUTE)

    def test_whitespace_stripped(self):
        self.assertIs(parse_command("  power  "), Command.POWER)
        self.assertIs(parse_command("\tvolume_up\n"), Command.VOLUME_UP)

    def test_unknown_and_empty_return_none(self):
        for bad in ("", "   ", "garbage", "channel up", "channel-up", "volume", "status", "42"):
            self.assertIsNone(parse_command(bad), bad)

    def test_non_string_returns_none(self):
        self.assertIsNone(parse_command(None))
        self.assertIsNone(parse_command(42))

    def test_never_raises(self):
        parse_command("anything at all")


class CommandValuesTest(unittest.TestCase):
    def test_semantic_values(self):
        # Values are the wire names used by the control socket (SCOPE §4.4).
        self.assertEqual(Command.CHANNEL_UP.value, "channel_up")
        self.assertEqual(Command.CHANNEL_DOWN.value, "channel_down")
        self.assertEqual(Command.VOLUME_UP.value, "volume_up")
        self.assertEqual(Command.VOLUME_DOWN.value, "volume_down")
        self.assertEqual(Command.MUTE.value, "mute")
        self.assertEqual(Command.POWER.value, "power")
        self.assertEqual(Command.INFO.value, "info")
        self.assertEqual(Command.BACK.value, "back")
        self.assertEqual(Command.SLEEP.value, "sleep")
        self.assertEqual(Command.LEFT.value, "left")
        self.assertEqual(Command.RIGHT.value, "right")


if __name__ == "__main__":
    unittest.main()
