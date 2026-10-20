"""Unit tests for rabbitears.config (config loading + per-key defaults)."""

from pathlib import Path
import tempfile
import unittest

from rabbitears.config import Config, load_config


class LoadConfigTest(unittest.TestCase):
    def test_missing_file_returns_all_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "nope" / "config.toml")
            self.assertEqual(cfg, Config())
            self.assertEqual(cfg.epoch, 0.0)
            self.assertEqual(cfg.volume_cap, 80)
            self.assertEqual(cfg.osd_color, "#33CC33")
            self.assertEqual(cfg.osd_duration, 2.5)
            self.assertEqual(cfg.osd_font_size, 72)
            self.assertEqual(cfg.socket_path, Path("/run/rabbitears/mpv.sock"))
            self.assertEqual(cfg.mount_point, Path("/media/rabbitears"))
            self.assertEqual(cfg.testpattern_path, Path("/usr/share/rabbitears/testpattern.png"))

    def test_corrupt_toml_returns_all_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("this is not valid toml [[[ = = =")
            self.assertEqual(load_config(path), Config())

    def test_valid_file_overrides_all_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "epoch = 1700000000\n"
                "volume_cap = 65\n"
                'osd_color = "#FF0000"\n'
                "osd_duration = 4.0\n"
                "osd_font_size = 48\n"
                'socket_path = "/tmp/mpv.sock"\n'
                'mount_point = "/media/usb"\n'
                'testpattern_path = "/usr/share/rabbitears/testpattern.png"\n'
            )
            cfg = load_config(path)
            self.assertEqual(cfg.epoch, 1700000000.0)
            self.assertEqual(cfg.volume_cap, 65)
            self.assertEqual(cfg.osd_color, "#FF0000")
            self.assertEqual(cfg.osd_duration, 4.0)
            self.assertEqual(cfg.osd_font_size, 48)
            self.assertEqual(cfg.socket_path, Path("/tmp/mpv.sock"))
            self.assertEqual(cfg.mount_point, Path("/media/usb"))
            self.assertEqual(cfg.testpattern_path, Path("/usr/share/rabbitears/testpattern.png"))

    def test_one_invalid_value_falls_back_for_that_key_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'volume_cap = "loud"\n'  # invalid: string, not int
                "osd_font_size = 48\n"  # valid
                'osd_color = "#123456"\n'  # valid
            )
            cfg = load_config(path)
            self.assertEqual(cfg.volume_cap, 80)  # fell back to default
            self.assertEqual(cfg.osd_font_size, 48)  # preserved
            self.assertEqual(cfg.osd_color, "#123456")  # preserved
            # Untouched keys keep their defaults.
            self.assertEqual(cfg.epoch, 0.0)
            self.assertEqual(cfg.osd_duration, 2.5)

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "volume_cap = 60\n"
                "not_a_real_key = 123\n"
                "another_unknown = [1, 2, 3]\n"
            )
            cfg = load_config(path)
            self.assertEqual(cfg.volume_cap, 60)
            self.assertEqual(cfg.osd_font_size, 72)  # default, not polluted
            self.assertFalse(hasattr(cfg, "not_a_real_key"))

    def test_socket_path_and_mount_point_are_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / "mpv.sock"
            mount = Path(tmp) / "media"
            path = Path(tmp) / "config.toml"
            path.write_text(
                f'socket_path = "{sock.as_posix()}"\n'
                f'mount_point = "{mount.as_posix()}"\n'
            )
            cfg = load_config(path)
            self.assertIsInstance(cfg.socket_path, Path)
            self.assertIsInstance(cfg.mount_point, Path)
            self.assertEqual(cfg.socket_path, sock)
            self.assertEqual(cfg.mount_point, mount)

    def test_testpattern_path_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "nope" / "config.toml")
            self.assertEqual(cfg.testpattern_path, Path("/usr/share/rabbitears/testpattern.png"))

    def test_testpattern_path_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp) / "pattern.png"
            path = Path(tmp) / "config.toml"
            path.write_text(f'testpattern_path = "{tp.as_posix()}"\n')
            cfg = load_config(path)
            self.assertIsInstance(cfg.testpattern_path, Path)
            self.assertEqual(cfg.testpattern_path, tp)

    def test_testpattern_path_invalid_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("testpattern_path = 42\n")  # invalid: int, not string
            cfg = load_config(path)
            self.assertEqual(cfg.testpattern_path, Path("/usr/share/rabbitears/testpattern.png"))


class NewKeyDefaultsTest(unittest.TestCase):
    def test_new_key_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "nope" / "config.toml")
            self.assertEqual(cfg.volume_step, 5)
            self.assertEqual(cfg.repeat_delay, 0.5)
            self.assertEqual(cfg.repeat_interval, 0.4)
            self.assertEqual(cfg.remote_vendor, 0x0627)
            self.assertEqual(cfg.control_socket, Path("/run/rabbitears/control.sock"))

    def test_new_key_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "volume_step = 3\n"
                "repeat_delay = 1.0\n"
                "repeat_interval = 0.2\n"
                "remote_vendor = 4098\n"
                'control_socket = "/tmp/ctl.sock"\n'
            )
            cfg = load_config(path)
            self.assertEqual(cfg.volume_step, 3)
            self.assertEqual(cfg.repeat_delay, 1.0)
            self.assertEqual(cfg.repeat_interval, 0.2)
            self.assertEqual(cfg.remote_vendor, 4098)
            self.assertEqual(cfg.control_socket, Path("/tmp/ctl.sock"))

    def test_volume_step_below_one_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("volume_step = 0\n")
            self.assertEqual(load_config(path).volume_step, 5)

    def test_repeat_interval_below_minimum_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("repeat_interval = 0.05\n")
            self.assertEqual(load_config(path).repeat_interval, 0.4)

    def test_invalid_types_fall_back_per_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'volume_step = "loud"\n'      # invalid: string
                "repeat_interval = 0.2\n"     # valid, preserved
                'remote_vendor = "0627"\n'    # invalid: string
            )
            cfg = load_config(path)
            self.assertEqual(cfg.volume_step, 5)
            self.assertEqual(cfg.repeat_interval, 0.2)
            self.assertEqual(cfg.remote_vendor, 0x0627)


if __name__ == "__main__":
    unittest.main()
