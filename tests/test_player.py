"""Unit tests for rabbitears.player.

Only the parts that don't need a running mpv are tested for real:
PlayerError behavior, _send on a dead socket, and start() timeout.
The rest are integration stubs (skipped) that need a real mpv — see
each stub's comment for how to run it manually in the VM.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rabbitears.player import Player, PlayerError


class FakeClock:
    """Monotonic clock that advances only when sleep() is called."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class PlayerErrorTest(unittest.TestCase):
    def test_player_error_is_raised_and_caught(self):
        with self.assertRaises(PlayerError):
            raise PlayerError("boom")

    def test_player_error_carrying_message(self):
        try:
            raise PlayerError("socket gone")
        except PlayerError as e:
            self.assertEqual(str(e), "socket gone")


class SendTest(unittest.TestCase):
    def test_send_raises_player_error_on_closed_socket(self):
        # No mpv is listening (socket file doesn't exist at all), so
        # connect() fails and _send must raise PlayerError.
        with tempfile.TemporaryDirectory() as tmp:
            player = Player(socket_path=Path(tmp) / "mpv.sock")
            with self.assertRaises(PlayerError):
                player._send(["get_property", "volume"])

    def test_send_raises_player_error_on_refused_socket(self):
        # Socket file exists but nothing is listening → ECONNREFUSED.
        import socket as socket_mod

        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            listener = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            listener.bind(str(sock_path))
            # No listen()/accept() — connections are refused.
            player = Player(socket_path=sock_path)
            with self.assertRaises(PlayerError):
                player._send(["quit"])
            listener.close()


class StartTest(unittest.TestCase):
    def test_start_raises_timeout_if_socket_never_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            player = Player(socket_path=Path(tmp) / "mpv.sock")
            fake = FakeClock()
            with (
                mock.patch("rabbitears.player.time.monotonic", fake.monotonic),
                mock.patch("rabbitears.player.time.sleep", fake.sleep),
                mock.patch("subprocess.Popen") as popen,
            ):
                popen.return_value.poll.return_value = None  # mpv "still alive"
                with self.assertRaises(TimeoutError):
                    player.start()
            # It launched mpv with the IPC socket flag.
            args = popen.call_args[0][0]
            self.assertIn(f"--input-ipc-server={player.socket_path}", args)

    def test_start_raises_player_error_if_mpv_exits_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            player = Player(socket_path=Path(tmp) / "mpv.sock")
            fake = FakeClock()
            with (
                mock.patch("rabbitears.player.time.monotonic", fake.monotonic),
                mock.patch("rabbitears.player.time.sleep", fake.sleep),
                mock.patch("subprocess.Popen") as popen,
            ):
                popen.return_value.poll.return_value = 1  # mpv died
                with self.assertRaises(PlayerError):
                    player.start()

    def test_start_returns_when_socket_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            sock_path.touch()  # pretend mpv already created it
            player = Player(socket_path=sock_path)
            fake = FakeClock()
            with (
                mock.patch("rabbitears.player.time.monotonic", fake.monotonic),
                mock.patch("rabbitears.player.time.sleep", fake.sleep),
                mock.patch("subprocess.Popen") as popen,
            ):
                popen.return_value.poll.return_value = None
                player.start()  # must not raise


class IntegrationStubs(unittest.TestCase):
    """These need a real mpv; run manually in the VM (see comments)."""

    @unittest.skip("requires mpv")
    def test_load(self) -> None:
        # Tests: player.load(path, offset) sends loadfile with
        # start=offset and mpv begins playing at that position.
        # Manual: in the VM, start mpv with --input-ipc-server=/tmp/t.sock
        # --idle=yes, then: Player("/tmp/t.sock").load(Path("some.mp4"), 10.0)
        # and confirm playback starts ~10s in.
        self.skipTest("requires a running mpv; see comment")

    @unittest.skip("requires mpv")
    def test_set_volume(self) -> None:
        # Tests: player.set_volume(30) changes the audible volume and
        # clamps out-of-range values.
        # Manual: with mpv running, call set_volume(30), then
        # `mpv --input-ipc-server=... get_property volume` (via
        # socat) and confirm 30.
        self.skipTest("requires a running mpv; see comment")

    @unittest.skip("requires mpv")
    def test_set_mute(self) -> None:
        # Tests: player.set_mute(True/False) toggles mute.
        # Manual: with mpv playing audio, toggle and check the
        # "mute" property via the IPC socket.
        self.skipTest("requires a running mpv; see comment")

    @unittest.skip("requires mpv")
    def test_hide_show_video(self) -> None:
        # Tests: hide_video() blacks out the screen (vid=no) while
        # audio/ playback continue; show_video() restores it (vid=1).
        # Manual: on the 3040 with DRM output, call hide_video() and
        # confirm the screen goes black, then show_video().
        self.skipTest("requires a running mpv; see comment")


if __name__ == "__main__":
    unittest.main()
