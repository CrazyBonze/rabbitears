"""Unit tests for rabbitears.control (the local control socket) and ctl.py.

Uses a real AF_UNIX socket in a temp dir, with a fake clock so the silent-client
timeout is deterministic. No mpv, no evdev, no real app.
"""

import io
import json
import os
import socket
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from rabbitears import ctl as ctl_mod
from rabbitears.commands import Command
from rabbitears.control import ControlServer


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _noop_handle(command, source) -> None:
    pass


class ControlTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "control.sock"
        self.calls = []
        self.clock = FakeClock()
        self.server = ControlServer(
            self.path,
            handle=lambda cmd, src: self.calls.append((cmd, src)),
            status=lambda: {"state": "playing", "channel": 3},
            clock=self.clock,
        )
        self.server.start()

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def _request(self, data: bytes) -> bytes:
        """Send one request line; serve once; return everything the server sent."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(self.path))
            s.sendall(data)
            self.server.serve()
            return s.recv(4096)


class ControlServerTest(ControlTestBase):
    def test_command_returns_ok_and_reaches_handler(self):
        reply = self._request(b"channel_up\n")
        self.assertEqual(reply, b"ok\n")
        self.assertEqual(self.calls, [(Command.CHANNEL_UP, "control")])

    def test_status_returns_json(self):
        reply = self._request(b"status\n")
        self.assertEqual(json.loads(reply), {"state": "playing", "channel": 3})

    def test_unknown_returns_error(self):
        self.assertEqual(self._request(b"bogus\n"), b"error: unknown command\n")

    def test_case_and_whitespace_tolerated(self):
        reply = self._request(b"  MUTE \n")
        self.assertEqual(reply, b"ok\n")
        self.assertEqual(self.calls, [(Command.MUTE, "control")])

    def test_overlong_line_is_dropped(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(self.path))
            s.sendall(b"x" * 100)  # no newline, over 64 bytes
            self.server.serve()
            self.assertEqual(s.recv(4096), b"")  # dropped: closed, no reply

    def test_silent_client_dropped_after_one_second(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(self.path))
            self.server.serve()          # accept; client sends nothing
            self.clock.advance(0.9)
            self.server.serve()          # not yet
            self.clock.advance(0.2)      # 1.1 s total
            self.server.serve()          # dropped now
            self.assertEqual(s.recv(4096), b"")

    def test_socket_mode_is_0600(self):
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_stale_socket_file_is_replaced(self):
        # Close, drop a plain file where the socket would be, restart.
        self.server.close()
        self.path.write_text("stale")
        self.server.start()
        self.assertTrue(stat.S_ISSOCK(os.stat(self.path).st_mode))


class CtlClientTest(unittest.TestCase):
    def _start(self, tmp, status=None):
        path = Path(tmp) / "control.sock"
        server = ControlServer(path, handle=_noop_handle, status=status or dict)
        server.start()
        return server, str(path)

    def _run_ctl(self, server, args, sock_path):
        """Run ctl.main in a thread while serving the socket; return (code, out)."""
        import threading
        import time

        result = {}

        def target():
            buf = io.StringIO()
            with redirect_stdout(buf):
                result["code"] = ctl_mod.main(args, socket_path=sock_path)
            result["out"] = buf.getvalue().strip()

        t = threading.Thread(target=target)
        t.start()
        deadline = time.monotonic() + 2.0
        while t.is_alive() and time.monotonic() < deadline:
            server.serve()
            time.sleep(0.005)
        t.join(timeout=1.0)
        return result.get("code"), result.get("out", "")

    def test_ctl_ok_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, path = self._start(tmp)
            code, out = self._run_ctl(server, ["channel_up"], path)
            self.assertEqual(code, 0)
            self.assertEqual(out, "ok")
            server.close()

    def test_ctl_status_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, path = self._start(tmp, status=lambda: {"state": "no_signal"})
            code, out = self._run_ctl(server, ["status"], path)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), {"state": "no_signal"})
            server.close()

    def test_ctl_unknown_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, path = self._start(tmp)
            code, out = self._run_ctl(server, ["bogus"], path)
            self.assertEqual(code, 1)
            self.assertEqual(out, "error: unknown command")
            server.close()

    def test_ctl_no_socket_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stderr(buf):
                code = ctl_mod.main(["status"], socket_path=str(Path(tmp) / "nope.sock"))
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
