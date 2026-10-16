"""Unit tests for rabbitears.channels (folder scanning, zapping)."""

import tempfile
import unittest
from pathlib import Path

from rabbitears.channels import (
    Channel,
    ChannelConfig,
    channel_down,
    channel_up,
    scan_channels,
)


def make_channel(number: int, name: str = "Test", shuffle: bool = False) -> Channel:
    return Channel(number=number, name=name, path=Path(f"/media/channels/{number:02d}-x"), shuffle=shuffle)


class ScanChannelsTest(unittest.TestCase):
    def _make_root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="rabbitears-test-"))
        (root / "03-magic-school-bus").mkdir()
        (root / "02-sesame-street").mkdir()
        (root / "not-a-channel").mkdir()
        (root / "3-no-leading-zero").mkdir()
        (root / "loose-file.mp4").touch()
        return root

    def test_parses_number_and_name_from_slug(self):
        root = self._make_root()
        channels = scan_channels(root)
        by_number = {c.number: c for c in channels}
        self.assertIn(3, by_number)
        self.assertEqual(by_number[3].name, "Magic School Bus")
        self.assertEqual(by_number[3].path, root / "03-magic-school-bus")
        self.assertFalse(by_number[3].shuffle)

    def test_returns_channels_sorted_by_number(self):
        root = self._make_root()
        channels = scan_channels(root)
        numbers = [c.number for c in channels]
        self.assertEqual(numbers, sorted(numbers))
        # 02 and 03 present; non-matching entries skipped.
        self.assertIn(2, numbers)
        self.assertNotIn(0, numbers)

    def test_non_matching_entries_are_skipped(self):
        root = self._make_root()
        channels = scan_channels(root)
        names = [c.path.name for c in channels]
        self.assertNotIn("not-a-channel", names)
        self.assertNotIn("loose-file.mp4", names)

    def test_missing_root_returns_empty_list(self):
        self.assertEqual(scan_channels(Path("/nonexistent/rabbitears")), [])

    def test_channel_toml_name_override(self):
        root = Path(tempfile.mkdtemp(prefix="rabbitears-test-"))
        ch = root / "03-magic-school-bus"
        ch.mkdir()
        (ch / "channel.toml").write_text('name = "The Magic School Bus"\n')
        channels = scan_channels(root)
        self.assertEqual(channels[0].name, "The Magic School Bus")

    def test_channel_toml_shuffle_true(self):
        root = Path(tempfile.mkdtemp(prefix="rabbitears-test-"))
        ch = root / "04-nature"
        ch.mkdir()
        (ch / "channel.toml").write_text("shuffle = true\n")
        channels = scan_channels(root)
        self.assertTrue(channels[0].shuffle)

    def test_channel_toml_absent_uses_defaults(self):
        root = Path(tempfile.mkdtemp(prefix="rabbitears-test-"))
        (root / "05-cartoons").mkdir()
        channels = scan_channels(root)
        self.assertEqual(channels[0].name, "Cartoons")
        self.assertFalse(channels[0].shuffle)

    def test_invalid_channel_toml_is_ignored(self):
        root = Path(tempfile.mkdtemp(prefix="rabbitears-test-"))
        ch = root / "06-broken"
        ch.mkdir()
        (ch / "channel.toml").write_text("this is not valid toml [[[\n")
        channels = scan_channels(root)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "Broken")
        self.assertFalse(channels[0].shuffle)

    def test_channel_config_defaults(self):
        config = ChannelConfig()
        self.assertIsNone(config.name)
        self.assertFalse(config.shuffle)


class ZapTest(unittest.TestCase):
    def setUp(self):
        # Gaps on purpose: 2, 3, 7, 9.
        self.channels = [
            make_channel(2, "Two"),
            make_channel(3, "Three"),
            make_channel(7, "Seven"),
            make_channel(9, "Nine"),
        ]

    def test_channel_up_skips_gaps(self):
        self.assertEqual(channel_up(self.channels, 3).number, 7)
        self.assertEqual(channel_up(self.channels, 7).number, 9)

    def test_channel_up_wraps_around(self):
        self.assertEqual(channel_up(self.channels, 9).number, 2)

    def test_channel_down_skips_gaps(self):
        self.assertEqual(channel_down(self.channels, 7).number, 3)
        self.assertEqual(channel_down(self.channels, 3).number, 2)

    def test_channel_down_wraps_around(self):
        self.assertEqual(channel_down(self.channels, 2).number, 9)

    def test_channel_up_unknown_current_returns_lowest(self):
        self.assertEqual(channel_up(self.channels, 5).number, 2)

    def test_channel_down_unknown_current_returns_highest(self):
        self.assertEqual(channel_down(self.channels, 5).number, 9)

    def test_channel_up_empty_raises(self):
        with self.assertRaises(ValueError):
            channel_up([], 1)

    def test_channel_down_empty_raises(self):
        with self.assertRaises(ValueError):
            channel_down([], 1)


if __name__ == "__main__":
    unittest.main()
