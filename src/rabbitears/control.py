"""The local control socket (SCOPE §4.4).

A root-only Unix stream socket for tests and scripted checks over SSH. It is a
*closed* surface: the only things it accepts are a ``Command`` (dispatched to
the app through an injected callback) and a read-only ``status`` (a callback
that returns a JSON-serialisable dict). There is no "load this path", no shell,
and no network listener — kid-proof by construction (SCOPE §1 goal 3).

Protocol: one request per connection, a single line of at most 64 bytes.

  * a ``Command`` value (``channel_up``, ``mute``, …)  ->  ``ok\\n``
  * ``status``                                        ->  one line of JSON
  * anything else                                     ->  ``error: unknown command\\n``

The connection then closes. ``serve()`` is called once per main-loop step and
never blocks: it uses non-blocking accept/read and drops a client that sends
nothing within 1 s, or more than 64 bytes without a newline, in ``serve()``.

``control.py`` imports ``commands.py`` only — never ``app.py`` — so the app is
reached through the injected ``handle``/``status`` callbacks.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path

from .commands import Command, parse_command

_MAX_LINE = 64        # bytes; a longer line without a newline is dropped
_TIMEOUT = 1.0        # seconds a silent client gets before it's dropped
_OK = b"ok\n"
_ERROR = b"error: unknown command\n"


class ControlServer:
    """The control socket: parse one-line requests and dispatch via callbacks."""

    def __init__(
        self,
        socket_path: Path | str,
        handle: Callable[[Command, str], None],
        status: Callable[[], dict],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = Path(socket_path)
        self._handle = handle
        self._status = status
        self._clock = clock
        self._sock: socket.socket | None = None
        # conn -> (deadline, bytes buffered so far)
        self._clients: dict[socket.socket, tuple[float, bytes]] = {}

    def start(self) -> None:
        """Bind the socket (mode 0600), replacing any stale socket file."""
        with contextlib.suppress(OSError):
            self._path.unlink()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self._path))
        os.chmod(self._path, 0o600)
        sock.listen(8)
        sock.setblocking(False)
        self._sock = sock

    def serve(self) -> None:
        """Non-blocking accept/read/dispatch. Call once per main-loop step."""
        if self._sock is None:
            return
        now = self._clock()
        self._accept()
        for conn in list(self._clients):
            self._serve_client(conn, now)

    def _accept(self) -> None:
        assert self._sock is not None
        while True:
            try:
                conn, _ = self._sock.accept()
            except (BlockingIOError, OSError):
                return
            conn.setblocking(False)
            self._clients[conn] = (self._clock() + _TIMEOUT, b"")

    def _serve_client(self, conn: socket.socket, now: float) -> None:
        deadline, buf = self._clients[conn]
        if now >= deadline:
            self._close(conn)
            return
        try:
            data = conn.recv(4096)
        except BlockingIOError:
            data = None
        except OSError:
            self._close(conn)
            return
        if data is None:
            return  # nothing to read yet; the deadline is checked next serve
        if data == b"":
            self._close(conn)  # peer closed without a complete line
            return
        buf += data
        if b"\n" in buf:
            line = buf.split(b"\n", 1)[0]
            self._reply(conn, line)
            self._close(conn)
            return
        if len(buf) > _MAX_LINE:
            self._close(conn)  # too long without a newline: dropped
            return
        self._clients[conn] = (now + _TIMEOUT, buf)

    def _reply(self, conn: socket.socket, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        command = parse_command(text)
        if command is not None:
            self._handle(command, "control")
            reply = _OK
        elif text == "status":
            reply = (json.dumps(self._status()) + "\n").encode("utf-8")
        else:
            reply = _ERROR
        with contextlib.suppress(OSError):
            conn.sendall(reply)

    def _close(self, conn: socket.socket) -> None:
        self._clients.pop(conn, None)
        with contextlib.suppress(OSError):
            conn.close()

    def close(self) -> None:
        for conn in list(self._clients):
            self._close(conn)
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
            with contextlib.suppress(OSError):
                self._path.unlink()
