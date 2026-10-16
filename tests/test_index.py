"""Unit tests for rabbitears.index (duration cache).

probe_duration is mocked everywhere — no real ffprobe in tests.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rabbitears import index as index_mod
from rabbitears.index import get_durations, load_index, probe_duration, save_index


class LoadIndexTest(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_index(Path(tmp) / "nope" / "index.json"), {})

    def test_corrupt_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            path.write_text("{ this is not valid json [[[")
            self.assertEqual(load_index(path), {})

    def test_non_dict_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            path.write_text("[1, 2, 3]")
            self.assertEqual(load_index(path), {})

    def test_malformed_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            path.write_text(json.dumps({"a.mp4": 100.0, "b.mp4": "bad", "c.mp4": None}))
            self.assertEqual(load_index(path), {"a.mp4": 100.0})


class SaveIndexTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            save_index(path, {"a.mp4": 100.0, "b.mp4": 200.5})
            self.assertEqual(load_index(path), {"a.mp4": 100.0, "b.mp4": 200.5})

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rabbitears" / "index.json"
            save_index(path, {"a.mp4": 1.0})
            self.assertTrue(path.is_file())

    def test_save_is_atomic_writes_temp_then_renames(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            seen = {}

            real_replace = os.replace

            def fake_replace(src, dst):
                # At rename time the temp file must exist, live in the
                # same directory (same filesystem) as the target, and
                # hold the payload.
                seen["src"] = Path(src)
                seen["dst"] = Path(dst)
                seen["src_existed"] = Path(src).is_file()
                seen["src_content"] = Path(src).read_text()
                real_replace(src, dst)

            with mock.patch.object(os, "replace", fake_replace):
                save_index(path, {"a.mp4": 42.0})

            self.assertEqual(seen["dst"], path)
            self.assertEqual(seen["src"].parent, path.parent)
            self.assertTrue(seen["src_existed"])
            self.assertEqual(json.loads(seen["src_content"]), {"a.mp4": 42.0})
            self.assertFalse(seen["src"].exists(), "temp file must be gone after rename")


class ProbeDurationTest(unittest.TestCase):
    def test_missing_binary_returns_none(self):
        with mock.patch.object(index_mod.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(probe_duration(Path("/nonexistent/file.mp4")))

    def test_nonzero_exit_returns_none(self):
        proc = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(index_mod.subprocess, "run", return_value=proc):
            self.assertIsNone(probe_duration(Path("x.mp4")))

    def test_bad_output_returns_none(self):
        proc = mock.Mock(returncode=0, stdout="not-a-number")
        with mock.patch.object(index_mod.subprocess, "run", return_value=proc):
            self.assertIsNone(probe_duration(Path("x.mp4")))

    def test_good_output_returns_float(self):
        proc = mock.Mock(returncode=0, stdout="123.45\n")
        with mock.patch.object(index_mod.subprocess, "run", return_value=proc) as run:
            self.assertEqual(probe_duration(Path("x.mp4")), 123.45)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "ffprobe")
        self.assertIn("-show_entries", cmd)
        self.assertIn("format=duration", cmd)


class GetDurationsTest(unittest.TestCase):
    def _make_channel(self, tmp: str, names: list[str]) -> Path:
        channel = Path(tmp) / "channels" / "03-magic-school-bus"
        channel.mkdir(parents=True)
        for name in names:
            (channel / name).touch()
        return channel

    def test_uses_cache_and_does_not_call_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._make_channel(tmp, ["a.mp4", "b.mp4"])
            index_path = Path(tmp) / ".rabbitears" / "index.json"
            save_index(index_path, {"a.mp4": 100.0, "b.mp4": 200.0})
            with mock.patch.object(index_mod, "probe_duration") as probe:
                durations = get_durations(channel, index_path)
            probe.assert_not_called()
            self.assertEqual(durations, [(channel / "a.mp4", 100.0), (channel / "b.mp4", 200.0)])

    def test_probes_only_missing_files_and_saves_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._make_channel(tmp, ["a.mp4", "b.mp4"])
            index_path = Path(tmp) / ".rabbitears" / "index.json"
            save_index(index_path, {"a.mp4": 100.0})
            with mock.patch.object(
                index_mod, "probe_duration", side_effect=lambda p: 200.0 if p.name == "b.mp4" else None
            ) as probe:
                durations = get_durations(channel, index_path)
            # Only b.mp4 was probed.
            self.assertEqual([c.args[0].name for c in probe.call_args_list], ["b.mp4"])
            self.assertEqual(durations, [(channel / "a.mp4", 100.0), (channel / "b.mp4", 200.0)])
            # New duration was saved to the cache.
            self.assertEqual(load_index(index_path), {"a.mp4": 100.0, "b.mp4": 200.0})

    def test_skips_files_where_probe_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._make_channel(tmp, ["a.mp4", "broken.mp4"])
            index_path = Path(tmp) / ".rabbitears" / "index.json"
            with mock.patch.object(index_mod, "probe_duration", return_value=None):
                durations = get_durations(channel, index_path)
            self.assertEqual(durations, [])

    def test_index_keys_are_filename_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._make_channel(tmp, ["a.mp4"])
            index_path = Path(tmp) / ".rabbitears" / "index.json"
            with mock.patch.object(index_mod, "probe_duration", return_value=50.0):
                get_durations(channel, index_path)
            keys = list(load_index(index_path))
            self.assertEqual(keys, ["a.mp4"])
            self.assertNotIn(str(channel / "a.mp4"), keys)

    def test_files_are_sorted_by_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._make_channel(tmp, ["c.mp4", "a.mp4", "b.mp4"])
            index_path = Path(tmp) / ".rabbitears" / "index.json"
            with mock.patch.object(index_mod, "probe_duration", return_value=10.0):
                durations = get_durations(channel, index_path)
            self.assertEqual([p.name for p, _ in durations], ["a.mp4", "b.mp4", "c.mp4"])

    def test_missing_channel_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.json"
            self.assertEqual(get_durations(Path(tmp) / "nope", index_path), [])


if __name__ == "__main__":
    unittest.main()
