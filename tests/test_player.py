"""Unit tests for rabbitears.player.

Uses a real AF_UNIX listener in a thread acting as a fake mpv, so the
IPC protocol (request_id matching, event skipping, get_property) is
tested end-to-end without a real mpv binary.
"""

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from rabbitears.player import Player, PlayerError


class FakeMpv:
    """A fake mpv IPC server on a real AF_UNIX socket.

    Runs in a background thread. Each connection is handled by reading
    a command line and writing back configurable responses.
    """

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._responses: list[str] = []
        self._received: list[dict] = []
        self._lock = threading.Lock()

    def set_responses(self, responses: list[dict | str]):
        """Set the lines to send back for the next connection."""
        with self._lock:
            self._responses = [
                r if isinstance(r, str) else json.dumps(r) + "\n"
                for r in responses
            ]

    def start(self):
        with contextlib_suppress(OSError):
            self.socket_path.unlink()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        assert self._server is not None
        self._server.settimeout(5.0)
        try:
            conn, _ = self._server.accept()
        except TimeoutError:
            return
        with conn:
            conn.settimeout(5.0)
            try:
                line = conn.makefile("r", encoding="utf-8").readline()
                if line:
                    with self._lock:
                        self._received.append(json.loads(line))
                    responses = list(self._responses)
                    for r in responses:
                        conn.sendall(r.encode("utf-8"))
            except (TimeoutError, OSError):
                pass

    def stop(self):
        if self._server:
            self._server.close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        with contextlib_suppress(OSError):
            self.socket_path.unlink()

    @property
    def received(self) -> list[dict]:
        with self._lock:
            return list(self._received)


def contextlib_suppress(*exceptions):
    import contextlib
    return contextlib.suppress(*exceptions)


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
        with tempfile.TemporaryDirectory() as tmp:
            player = Player(socket_path=Path(tmp) / "mpv.sock")
            with self.assertRaises(PlayerError):
                player._send(["get_property", "volume"])

    def test_send_raises_player_error_on_refused_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(sock_path))
            player = Player(socket_path=sock_path)
            with self.assertRaises(PlayerError):
                player._send(["quit"])
            listener.close()

    def test_send_skips_event_lines_before_reply(self):
        """An async event line arriving before the reply is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([
                {"event": "file-loaded", "id": 1},
                {"error": "success", "data": 42, "request_id": 1},
            ])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                resp = player._send(["get_property", "volume"])
                self.assertEqual(resp["data"], 42)
            finally:
                fake.stop()

    def test_send_skips_mismatched_request_id(self):
        """A reply with a different request_id is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([
                {"error": "success", "data": "wrong", "request_id": 999},
                {"error": "success", "data": "right", "request_id": 1},
            ])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                resp = player._send(["get_property", "volume"])
                self.assertEqual(resp["data"], "right")
            finally:
                fake.stop()

    def test_send_increments_request_id(self):
        """Each command gets a unique, incrementing request_id."""
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([{"error": "success", "request_id": 1}])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                player._send(["quit"])
                self.assertEqual(fake.received[0]["request_id"], 1)
            finally:
                fake.stop()
            # Second connection would get request_id 2
            fake.set_responses([{"error": "success", "request_id": 2}])
            fake.start()
            try:
                player._send(["quit"])
                self.assertEqual(fake.received[1]["request_id"], 2)
            finally:
                fake.stop()


class GetPropertyTest(unittest.TestCase):
    def _make_player_with_fake(self, tmp: str, responses: list) -> tuple[Player, FakeMpv]:
        sock_path = Path(tmp) / "mpv.sock"
        fake = FakeMpv(sock_path)
        fake.set_responses(responses)
        fake.start()
        return Player(socket_path=sock_path), fake

    def test_get_property_returns_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            player, fake = self._make_player_with_fake(
                tmp, [{"error": "success", "data": 30, "request_id": 1}]
            )
            try:
                self.assertEqual(player.get_property("volume"), 30)
            finally:
                fake.stop()

    def test_get_property_returns_default_for_unavailable(self):
        """get_property returns default when mpv says 'property unavailable'."""
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([
                {"error": "property unavailable", "request_id": 1},
            ])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                self.assertIsNone(player.get_property("time-pos"))
            finally:
                fake.stop()
        # Second call with an explicit default.
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([
                {"error": "property unavailable", "request_id": 1},
            ])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                self.assertEqual(
                    player.get_property("time-pos", default=-1.0), -1.0
                )
            finally:
                fake.stop()

    def test_get_property_raises_on_other_errors(self):
        """get_property raises PlayerError for errors other than 'property unavailable'."""
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([
                {"error": "some other error", "request_id": 1},
            ])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                with self.assertRaises(PlayerError):
                    player.get_property("volume")
            finally:
                fake.stop()


class IsIdleTest(unittest.TestCase):
    def test_is_idle_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([{"error": "success", "data": True, "request_id": 1}])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                self.assertTrue(player.is_idle())
            finally:
                fake.stop()

    def test_is_idle_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([{"error": "success", "data": False, "request_id": 1}])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                self.assertFalse(player.is_idle())
            finally:
                fake.stop()


class IsRunningTest(unittest.TestCase):
    def _make_player(self) -> Player:
        with tempfile.TemporaryDirectory() as tmp:
            return Player(socket_path=Path(tmp) / "mpv.sock")

    def test_is_running_true_when_proc_alive(self):
        player = self._make_player()
        player._proc = mock.Mock()
        player._proc.poll.return_value = None
        self.assertTrue(player.is_running())

    def test_is_running_false_when_proc_exited(self):
        player = self._make_player()
        player._proc = mock.Mock()
        player._proc.poll.return_value = 0
        self.assertFalse(player.is_running())

    def test_is_running_false_when_never_started(self):
        player = self._make_player()
        self.assertFalse(player.is_running())


class ShowImageTest(unittest.TestCase):
    def test_show_image_sends_loadfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([{"error": "success", "request_id": 1}])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                player.show_image(Path("/usr/share/rabbitears/testpattern.png"))
                cmd = fake.received[0]["command"]
                self.assertEqual(cmd[0], "loadfile")
                self.assertEqual(cmd[1], "/usr/share/rabbitears/testpattern.png")
            finally:
                fake.stop()


class LoadTest(unittest.TestCase):
    def test_load_uses_the_mpv_038_loadfile_signature(self):
        # mpv >= 0.38: loadfile <url> <flags> <index> <options>, with string
        # option values. The old form (options third, float value) is rejected
        # by mpv 0.41 with "invalid parameter" — verified on the VM.
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([{"error": "success", "request_id": 1}])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                player.load(Path("/media/rabbitears/channels/01-a/x.mp4"), 21.0039579)
                cmd = fake.received[0]["command"]
                self.assertEqual(cmd, ["loadfile", "/media/rabbitears/channels/01-a/x.mp4",
                                       "replace", -1, {"start": "21.004"}])
            finally:
                fake.stop()


class OsdTest(unittest.TestCase):
    def test_show_osd_text_sends_osd_overlay_ass_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([{"error": "success", "request_id": 1}])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                player.show_osd_text("{\\an9}CH 03")
                self.assertEqual(
                    fake.received[0]["command"],
                    ["osd-overlay", 0, "ass-events", "{\\an9}CH 03",
                     0, 288, 0, False, False],
                )
            finally:
                fake.stop()

    def test_clear_osd_sends_osd_overlay_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([{"error": "success", "request_id": 1}])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                player.clear_osd()
                self.assertEqual(
                    fake.received[0]["command"],
                    ["osd-overlay", 0, "none", "", 0, 288, 0, False, False],
                )
            finally:
                fake.stop()

    def test_show_osd_text_raises_player_error_on_ipc_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            fake = FakeMpv(sock_path)
            fake.set_responses([{"error": "some other error", "request_id": 1}])
            fake.start()
            try:
                player = Player(socket_path=sock_path)
                with self.assertRaises(PlayerError):
                    player.show_osd_text("CH 03")
            finally:
                fake.stop()

    def test_clear_osd_raises_player_error_on_closed_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            player = Player(socket_path=Path(tmp) / "mpv.sock")
            with self.assertRaises(PlayerError):
                player.clear_osd()


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
                popen.return_value.poll.return_value = None
                with self.assertRaises(TimeoutError):
                    player.start()
            args = popen.call_args[0][0]
            self.assertIn(f"--input-ipc-server={player.socket_path}", args)
            self.assertIn("--image-display-duration=inf", args)

    def test_start_raises_player_error_if_mpv_exits_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            player = Player(socket_path=Path(tmp) / "mpv.sock")
            fake = FakeClock()
            with (
                mock.patch("rabbitears.player.time.monotonic", fake.monotonic),
                mock.patch("rabbitears.player.time.sleep", fake.sleep),
                mock.patch("subprocess.Popen") as popen,
            ):
                popen.return_value.poll.return_value = 1
                with self.assertRaises(PlayerError):
                    player.start()

    def test_start_returns_when_socket_appears(self):
        """start() returns when the socket file appears (created by mpv)."""
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            player = Player(socket_path=sock_path)
            fake = FakeClock()

            def fake_popen(args, **kw):
                # Simulate mpv creating the socket file.
                sock_path.touch()
                proc = mock.Mock()
                proc.poll.return_value = None
                return proc

            with (
                mock.patch("rabbitears.player.time.monotonic", fake.monotonic),
                mock.patch("rabbitears.player.time.sleep", fake.sleep),
                mock.patch("subprocess.Popen", side_effect=fake_popen),
            ):
                player.start()  # must not raise

    def test_start_removes_stale_socket(self):
        """start() unlinks a stale socket file before launching mpv."""
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "mpv.sock"
            # Create a stale socket file.
            sock_path.touch()
            player = Player(socket_path=sock_path)
            fake = FakeClock()
            with (
                mock.patch("rabbitears.player.time.monotonic", fake.monotonic),
                mock.patch("rabbitears.player.time.sleep", fake.sleep),
                mock.patch("subprocess.Popen") as popen,
            ):
                popen.return_value.poll.return_value = None
                # After start(), the stale socket should have been unlinked.
                # The fake clock means the loop exits immediately on first check.
                # Since we unlinked the socket, it won't exist, so the loop
                # will time out. But the key assertion is that unlink was called.
                with self.assertRaises(TimeoutError):
                    player.start()
            # The stale socket file should be gone (unlinked by start()).
            self.assertFalse(sock_path.exists())

    def test_start_args_include_image_display_duration(self):
        """--image-display-duration=inf is in the mpv args."""
        with tempfile.TemporaryDirectory() as tmp:
            player = Player(socket_path=Path(tmp) / "mpv.sock")
            fake = FakeClock()
            with (
                mock.patch("rabbitears.player.time.monotonic", fake.monotonic),
                mock.patch("rabbitears.player.time.sleep", fake.sleep),
                mock.patch("subprocess.Popen") as popen,
            ):
                popen.return_value.poll.return_value = None
                with self.assertRaises(TimeoutError):
                    player.start()
            args = popen.call_args[0][0]
            self.assertIn("--image-display-duration=inf", args)


class IntegrationStubs(unittest.TestCase):
    """These need a real mpv; run manually in the VM (see comments)."""

    @unittest.skip("requires mpv")
    def test_load(self) -> None:
        self.skipTest("requires a running mpv; see comment")

    @unittest.skip("requires mpv")
    def test_set_volume(self) -> None:
        self.skipTest("requires a running mpv; see comment")

    @unittest.skip("requires mpv")
    def test_set_mute(self) -> None:
        self.skipTest("requires a running mpv; see comment")

    @unittest.skip("requires mpv")
    def test_hide_show_video(self) -> None:
        self.skipTest("requires a running mpv; see comment")


if __name__ == "__main__":
    unittest.main()
