"""Unit tests for rabbitears.app.

Uses a fake Player (records calls, scripted is_idle/is_running), a fake
clock, and an injected media_present. A temp dir holds empty .mp4 files plus
a pre-written .rabbitears/index.json so get_durations reads from cache and no
ffprobe is needed.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rabbitears import app as app_mod
from rabbitears import state as state_mod
from rabbitears.channels import Channel
from rabbitears.commands import Command
from rabbitears.config import Config


class FakePlayer:
    """Records calls; is_idle/is_running are scripted by the test."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.loaded: list[tuple[Path, float]] = []
        self.shown_images: list[Path] = []
        self.volumes: list[int] = []
        self._idle = False
        self._running = True
        self.stopped = False

    def start(self) -> None:
        self.calls.append(("start",))

    def set_volume(self, volume: int) -> None:
        self.volumes.append(volume)
        self.calls.append(("set_volume", volume))

    def set_mute(self, muted: bool) -> None:
        self.calls.append(("set_mute", muted))

    def set_pause(self, paused: bool) -> None:
        self.calls.append(("set_pause", paused))

    def show_image(self, path: Path) -> None:
        self.shown_images.append(path)
        self.calls.append(("show_image", path))

    def hide_video(self) -> None:
        self.calls.append(("hide_video",))

    def show_video(self) -> None:
        self.calls.append(("show_video",))

    def load(self, path: Path, offset: float = 0.0) -> None:
        self.loaded.append((path, offset))
        self.calls.append(("load", path, offset))

    def is_idle(self) -> bool:
        return self._idle

    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self.stopped = True
        self.calls.append(("stop",))


class FakeClock:
    """A wall-clock stand-in the test advances by hand."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_mount(tmp: str, index: dict | None = None) -> Path:
    """Build <tmp>/channels/01-cartoons/{a,b}.mp4 + <tmp>/.rabbitears/index.json."""
    mount = Path(tmp)
    chdir = mount / "channels" / "01-cartoons"
    chdir.mkdir(parents=True)
    for name in ("a.mp4", "b.mp4"):
        (chdir / name).write_bytes(b"")
    rb = mount / ".rabbitears"
    rb.mkdir()
    (rb / "index.json").write_text(json.dumps(index or {"a.mp4": 100.0, "b.mp4": 200.0}))
    return mount


class AppTestBase(unittest.TestCase):
    def _config(self, mount: Path) -> Config:
        return Config(
            mount_point=mount,
            testpattern_path=mount / "testpattern.png",
            epoch=0.0,
            volume_cap=80,
        )

    def _app(self, mount: Path, clock: FakeClock, present: dict) -> tuple[app_mod.App, FakePlayer]:
        player = FakePlayer()
        app = app_mod.App(
            player, clock, self._config(mount),
            media_present_fn=lambda mp: present["present"],
        )
        return app, player


class PureHelpersTest(unittest.TestCase):
    def test_media_present_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            (mount / "channels").mkdir()
            with mock.patch("rabbitears.app.os.path.ismount", return_value=True):
                self.assertTrue(app_mod.media_present(mount))

    def test_media_present_false_no_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            with mock.patch("rabbitears.app.os.path.ismount", return_value=True):
                self.assertFalse(app_mod.media_present(mount))

    def test_media_present_false_not_mounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            (mount / "channels").mkdir()
            with mock.patch("rabbitears.app.os.path.ismount", return_value=False):
                self.assertFalse(app_mod.media_present(mount))

    def test_select_channel_saved_present(self):
        chs = [Channel(1, "One", Path("1")), Channel(3, "Three", Path("3"))]
        ch = app_mod.select_channel(chs, 3)
        assert ch is not None
        self.assertEqual(ch.number, 3)

    def test_select_channel_saved_missing_uses_lowest(self):
        chs = [Channel(1, "One", Path("1")), Channel(3, "Three", Path("3"))]
        for saved in (99, -1):
            ch = app_mod.select_channel(chs, saved)
            assert ch is not None
            self.assertEqual(ch.number, 1)

    def test_select_channel_none_when_empty(self):
        self.assertIsNone(app_mod.select_channel([], 1))

    def test_tune_target(self):
        files = [(Path("a"), 100.0), (Path("b"), 200.0)]
        # loop=300, epoch=0, now=150 -> position 150 -> b at offset 50
        path, offset = app_mod.tune_target(files, 0.0, 150.0)
        self.assertEqual(path, Path("b"))
        self.assertAlmostEqual(offset, 50.0)

    def test_channel_label(self):
        ch = Channel(3, "Magic School Bus", Path("03-magic-school-bus"))
        self.assertEqual(app_mod.channel_label(ch), "CH 03 · Magic School Bus")


class AppBehaviorTest(AppTestBase):
    def test_boot_no_media_shows_pattern_no_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            (mount / "channels").mkdir()
            config = self._config(mount)
            player = FakePlayer()
            clock = FakeClock()
            present = {"present": False}
            app = app_mod.App(player, clock, config, media_present_fn=lambda mp: present["present"])
            app.start()
            self.assertEqual(player.shown_images, [config.testpattern_path])
            self.assertEqual(player.loaded, [])
            for _ in range(10):
                app.step()
                clock.advance(0.1)
            self.assertEqual(player.loaded, [])  # still no load

    def test_media_appears_tunes_correct_file_and_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)  # a=100, b=200, loop=300
            clock = FakeClock(150.0)  # position 150 -> b at offset 50
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()
            self.assertEqual(app._mode, "PLAYING")
            self.assertEqual(player.loaded, [(mount / "channels/01-cartoons/b.mp4", 50.0)])

    def test_eof_recomputes_from_clock_not_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock(150.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()  # tune: b@50 (position 150)
            # Jump the clock so position wraps to 50 -> a@50 (NOT a@0 as a
            # sequence-advance would give).
            clock.t = 350.0
            player._idle = True  # mpv hit EOF
            app.step()
            self.assertEqual(player.loaded[-1], (mount / "channels/01-cartoons/a.mp4", 50.0))

    def test_saved_channel_used_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            for num, files in (("01-cartoons", ["a.mp4"]), ("02-anime", ["c.mp4"])):
                d = mount / "channels" / num
                d.mkdir(parents=True)
                for f in files:
                    (d / f).write_bytes(b"")
            rb = mount / ".rabbitears"
            rb.mkdir()
            (rb / "index.json").write_text(json.dumps({"a.mp4": 100.0, "c.mp4": 100.0}))
            (rb / "state.json").write_text(json.dumps({"channel": 2, "volume": 42}))
            clock = FakeClock(10.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()
            self.assertEqual(player.loaded[-1][0], mount / "channels/02-anime/c.mp4")
            self.assertEqual(player.volumes[-1], 42)

    def test_saved_channel_missing_uses_lowest(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            for num, files in (("01-cartoons", ["a.mp4"]), ("02-anime", ["c.mp4"])):
                d = mount / "channels" / num
                d.mkdir(parents=True)
                for f in files:
                    (d / f).write_bytes(b"")
            rb = mount / ".rabbitears"
            rb.mkdir()
            (rb / "index.json").write_text(json.dumps({"a.mp4": 100.0, "c.mp4": 100.0}))
            (rb / "state.json").write_text(json.dumps({"channel": 99, "volume": 50}))
            clock = FakeClock(10.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()
            self.assertEqual(player.loaded[-1][0], mount / "channels/01-cartoons/a.mp4")

    def test_drive_pulled_then_reinserted_retunes_at_current_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock(150.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()  # tune b@50
            self.assertEqual(app._mode, "PLAYING")
            # Pull the drive.
            present["present"] = False
            app.step()
            self.assertEqual(app._mode, "NO_SIGNAL")
            self.assertEqual(player.shown_images[-1], self._config(mount).testpattern_path)
            # Re-insert 10s later; retune at the current position (160 % 300 = 160 -> b@60).
            present["present"] = True
            clock.t = 160.0
            app.step()
            self.assertEqual(app._mode, "PLAYING")
            self.assertEqual(player.loaded[-1], (mount / "channels/01-cartoons/b.mp4", 60.0))

    def test_power_standby_hides_video_and_wake_retunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)  # a=100, b=200, loop=300
            clock = FakeClock(150.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()
            self.assertEqual(app._mode, "PLAYING")
            # Standby: video hidden + muted, but still PLAYING (mpv keeps running).
            app.handle(Command.POWER)
            self.assertTrue(app._standby)
            self.assertEqual(app._mode, "PLAYING")
            self.assertIn(("hide_video",), player.calls)
            self.assertIn(("set_mute", True), player.calls)
            # Wake: video shown, mute restored, re-tuned at the *current* position.
            clock.t = 350.0  # position 350 % 300 = 50 -> a@50
            app.handle(Command.POWER)
            self.assertFalse(app._standby)
            self.assertIn(("show_video",), player.calls)
            self.assertEqual(player.loaded[-1], (mount / "channels/01-cartoons/a.mp4", 50.0))

    def test_unplayable_file_skipped_once_no_reload_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)  # a=100, b=200
            clock = FakeClock(10.0)  # position 10 -> a@10
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()  # tune a@10
            # mpv goes idle 1s later at an offset not near a's end (10 < 98).
            clock.advance(1.0)  # now=11, since_load=1 < 2
            player._idle = True
            app.step()  # unplayable: skip a, retune
            self.assertIn(mount / "channels/01-cartoons/a.mp4", app._skip)
            # playlist now [b=200], loop=200; position 11 % 200 = 11 -> b@11
            self.assertEqual(player.loaded[-1], (mount / "channels/01-cartoons/b.mp4", 11.0))
            # A later normal EOF near b's end recomputes (does not skip b).
            clock.t = 210.0  # position 210 % 200 = 10 -> b@10
            player._idle = True
            app._last_loaded_offset = 199.0  # near b's end (200)
            app.step()
            self.assertNotIn(mount / "channels/01-cartoons/b.mp4", app._skip)
            self.assertEqual(player.loaded[-1], (mount / "channels/01-cartoons/b.mp4", 10.0))

    def test_mpv_dies_raises_app_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock()
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            player._running = False
            with self.assertRaises(app_mod.AppCrash):
                app.step()

    def test_run_returns_1_on_mpv_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock()
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            player._running = False  # mpv dies before the first step
            rc = app.run()
            self.assertEqual(rc, 1)
            self.assertTrue(player.stopped)

    def test_sigterm_saves_state_stops_and_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock(150.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            with mock.patch("rabbitears.app.time.sleep") as slp:
                slp.side_effect = lambda s: setattr(app, "_stop", True)
                rc = app.run()
            self.assertEqual(rc, 0)
            self.assertTrue(player.stopped)
            state = state_mod.load_state(mount / ".rabbitears" / "state.json")
            self.assertEqual(state.channel, 1)


class HandleTest(AppTestBase):
    """The Part 1b state table: handle(Command) across states."""

    def _gap_mount(self, tmp, files):
        """Build channels/ with the given dir -> {filename: duration} mapping."""
        mount = Path(tmp)
        index = {}
        for dirname, mapping in files.items():
            d = mount / "channels" / dirname
            d.mkdir(parents=True)
            for fname, dur in mapping.items():
                (d / fname).write_bytes(b"")
                index[fname] = dur
        rb = mount / ".rabbitears"
        rb.mkdir()
        (rb / "index.json").write_text(json.dumps(index))
        return mount

    def test_channel_up_wraps_and_skips_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = self._gap_mount(tmp, {
                "01-cartoons": {"a.mp4": 100.0},
                "03-sports": {"b.mp4": 100.0},
                "07-news": {"c.mp4": 100.0},
            })
            clock = FakeClock(10.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()
            self.assertEqual(app._mode, "PLAYING")
            self.assertEqual(app.status()["channel"], 1)
            # up -> 3 (gap skipped), up -> 7, up -> wrap to 1.
            app.handle(Command.CHANNEL_UP)
            self.assertEqual(app.status()["channel"], 3)
            app.handle(Command.CHANNEL_UP)
            self.assertEqual(app.status()["channel"], 7)
            app.handle(Command.CHANNEL_UP)
            self.assertEqual(app.status()["channel"], 1)
            # down from 1 wraps to 7.
            app.handle(Command.CHANNEL_DOWN)
            self.assertEqual(app.status()["channel"], 7)

    def test_channel_tune_lands_at_current_position_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = self._gap_mount(tmp, {
                "01-cartoons": {"a.mp4": 100.0},
                "03-sports": {"b.mp4": 100.0},
            })
            clock = FakeClock(10.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()  # channel 1, a.mp4 @10
            clock.t = 150.0  # channel 3's b.mp4 loop 100 -> position 50
            app.handle(Command.CHANNEL_UP)
            self.assertEqual(player.loaded[-1], (mount / "channels/03-sports/b.mp4", 50.0))
            saved = state_mod.load_state(mount / ".rabbitears" / "state.json")
            self.assertEqual(saved.channel, 3)

    def test_empty_channel_skipped_in_direction_of_travel(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = self._gap_mount(tmp, {
                "01-cartoons": {"a.mp4": 100.0},
                "03-sports": {"b.mp4": 100.0},
                "05-empty": {},
                "07-news": {"c.mp4": 100.0},
            })
            clock = FakeClock(10.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()  # channel 1
            app.handle(Command.CHANNEL_UP)   # 3 (skips nothing empty yet)
            self.assertEqual(app.status()["channel"], 3)
            app.handle(Command.CHANNEL_UP)   # skips empty 5 -> 7
            self.assertEqual(app.status()["channel"], 7)

    def test_all_channels_empty_goes_no_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = self._gap_mount(tmp, {
                "01-cartoons": {"a.mp4": 100.0},
                "03-sports": {"b.mp4": 100.0},
            })
            clock = FakeClock(10.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()
            self.assertEqual(app._mode, "PLAYING")
            # Remove every playable file and the index; zap finds nothing.
            for d in (mount / "channels").iterdir():
                for f in d.glob("*.mp4"):
                    f.unlink()
            (mount / ".rabbitears" / "index.json").write_text("{}")
            app.handle(Command.CHANNEL_UP)
            self.assertEqual(app._mode, "NO_SIGNAL")
            self.assertEqual(player.shown_images[-1], self._config(mount).testpattern_path)

    def test_volume_step_clamp_and_unmute(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock()
            present = {"present": False}  # NO_SIGNAL: volume still works
            app, player = self._app(mount, clock, present)
            app.start()  # default volume 50
            app.handle(Command.VOLUME_UP)
            self.assertEqual(app.status()["volume"], 55)  # step 5
            app.handle(Command.MUTE)
            self.assertTrue(app.status()["muted"])
            app.handle(Command.VOLUME_DOWN)  # unmutes, 55 -> 50
            self.assertFalse(app.status()["muted"])
            self.assertIn(("set_mute", False), player.calls)
            for _ in range(30):
                app.handle(Command.VOLUME_UP)
            self.assertEqual(app.status()["volume"], self._config(mount).volume_cap)
            for _ in range(30):
                app.handle(Command.VOLUME_DOWN)
            self.assertEqual(app.status()["volume"], 0)

    def test_volume_flush_throttled_to_once_per_2s(self):
        """A burst of volume presses flushes state.json at most once per 2 s."""
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock()
            present = {"present": False}  # NO_SIGNAL: volume still works
            app, player = self._app(mount, clock, present)
            app.start()

            saves = []
            real_save = state_mod.save_state

            def counting_save(path, state):
                saves.append(clock.t)
                return real_save(path, state)

            with mock.patch.object(state_mod, "save_state", counting_save):
                # First press: _last_state_flush starts at 0, so this flushes now.
                app.handle(Command.VOLUME_UP)
                app.step()
                self.assertEqual(len(saves), 1)

                # A burst of presses within 2 s: dirty stays set, no re-flush.
                for _ in range(10):
                    app.handle(Command.VOLUME_UP)
                    clock.advance(0.1)
                    app.step()
                self.assertEqual(len(saves), 1, "burst within 2 s must not re-flush")

                # Cross the 2 s boundary: exactly one more flush.
                clock.advance(2.0)
                app.step()
                self.assertEqual(len(saves), 2)

                # Another burst right after: throttled again (volume at cap, no change).
                for _ in range(5):
                    app.handle(Command.VOLUME_UP)
                    clock.advance(0.1)
                    app.step()
                self.assertEqual(len(saves), 2)

    def test_mute_toggles(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock()
            present = {"present": False}
            app, player = self._app(mount, clock, present)
            app.start()
            app.handle(Command.MUTE)
            self.assertTrue(app.status()["muted"])
            self.assertIn(("set_mute", True), player.calls)
            app.handle(Command.MUTE)
            self.assertFalse(app.status()["muted"])
            self.assertIn(("set_mute", False), player.calls)

    def test_standby_ignores_everything_but_power(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock(150.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()  # PLAYING
            app.handle(Command.POWER)  # standby
            player.calls.clear()
            for cmd in (Command.CHANNEL_UP, Command.VOLUME_UP, Command.MUTE,
                        Command.INFO, Command.BACK):
                app.handle(cmd)
            self.assertEqual(player.calls, [])  # all ignored in standby

    def test_wake_restores_pre_standby_mute_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock(150.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()
            app.handle(Command.MUTE)  # user muted before standby
            app.handle(Command.POWER)  # standby forces mute on
            player.calls.clear()
            app.handle(Command.POWER)  # wake: restore user mute (still muted)
            self.assertTrue(app.status()["muted"])
            self.assertIn(("show_video",), player.calls)
            self.assertIn(("set_mute", True), player.calls)  # restored, not cleared

    def test_wake_in_no_signal_shows_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            (mount / "channels").mkdir()
            clock = FakeClock()
            present = {"present": False}
            app, player = self._app(mount, clock, present)
            app.start()
            self.assertEqual(app._mode, "NO_SIGNAL")
            app.handle(Command.POWER)  # standby
            app.handle(Command.POWER)  # wake in NO_SIGNAL
            self.assertEqual(player.shown_images[-1], self._config(mount).testpattern_path)
            self.assertIn(("show_video",), player.calls)

    def test_info_back_no_player_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock(150.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            app.step()
            player.calls.clear()
            app.handle(Command.INFO)
            app.handle(Command.BACK)
            self.assertEqual(player.calls, [])

    def test_no_signal_channel_ignored_volume_mute_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            (mount / "channels").mkdir()
            clock = FakeClock()
            present = {"present": False}
            app, player = self._app(mount, clock, present)
            app.start()
            player.calls.clear()
            app.handle(Command.CHANNEL_UP)
            self.assertEqual(player.calls, [])  # channel ignored in NO_SIGNAL
            app.handle(Command.VOLUME_UP)
            self.assertIn(("set_volume", 55), player.calls)
            app.handle(Command.MUTE)
            self.assertIn(("set_mute", True), player.calls)

    def test_status_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = _make_mount(tmp)
            clock = FakeClock(150.0)
            present = {"present": True}
            app, player = self._app(mount, clock, present)
            app.start()
            self.assertEqual(app.status(), {
                "state": "no_signal", "standby": False, "channel": None,
                "channel_name": None, "file": None, "volume": 50, "muted": False,
            })
            app.step()
            self.assertEqual(app.status()["state"], "playing")
            self.assertEqual(app.status()["channel"], 1)
            self.assertEqual(app.status()["file"], "b.mp4")


if __name__ == "__main__":
    unittest.main()
