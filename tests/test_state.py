"""Unit tests for rabbitears.state (runtime state persistence)."""

from pathlib import Path
from unittest import mock
import json
import os
import tempfile
import unittest

from rabbitears.state import State, load_state, save_state


class LoadStateTest(unittest.TestCase):
    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = load_state(Path(tmp) / "nope" / "state.json")
            self.assertEqual(st, State())
            self.assertEqual(st.channel, -1)
            self.assertEqual(st.volume, 50)

    def test_corrupt_json_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{ this is not valid json [[[")
            self.assertEqual(load_state(path), State())

    def test_non_dict_json_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("[1, 2, 3]")
            self.assertEqual(load_state(path), State())

    def test_invalid_types_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"channel": "seven", "volume": 42}))
            st = load_state(path)
            self.assertEqual(st.channel, -1)  # "seven" is not an int -> default
            self.assertEqual(st.volume, 42)  # valid int preserved

    def test_bool_is_not_treated_as_int(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"channel": True, "volume": False}))
            st = load_state(path)
            self.assertEqual(st.channel, -1)
            self.assertEqual(st.volume, 50)


class SaveStateTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, State(channel=7, volume=63))
            self.assertEqual(load_state(path), State(channel=7, volume=63))

    def test_saved_and_reloaded_matches_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rabbitears" / "state.json"
            original = State(channel=4, volume=55)
            save_state(path, original)
            self.assertEqual(load_state(path), original)

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rabbitears" / "state.json"
            save_state(path, State())
            self.assertTrue(path.is_file())

    def test_readonly_path_swallows_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            # Simulate a read-only / absent drive: the atomic rename fails.
            with mock.patch.object(os, "replace", side_effect=PermissionError):
                save_state(path, State(channel=3, volume=10))  # must not raise
            # Nothing was atomically committed.
            self.assertFalse(path.exists())

    def test_absent_drive_swallows_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            # Simulate the drive being absent: mkdir raises OSError.
            with mock.patch.object(Path, "mkdir", side_effect=OSError("no device")):
                save_state(path, State())  # must not raise


if __name__ == "__main__":
    unittest.main()
