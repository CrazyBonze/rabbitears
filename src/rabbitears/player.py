"""Single long-running mpv wrapper over the JSON IPC socket (SCOPE.md §4.3).

One mpv process runs for the life of the app, outputting over DRM
(`--vo=gpu --gpu-context=drm` with VAAPI hwdec).  All control goes
through mpv's JSON IPC protocol: newline-delimited JSON commands over a
Unix domain socket.  Channel changes use `loadfile ... start=<offset>`
on the *same* instance — never a process restart — targeting a channel
change under 1.5s on the Wyse 3040.

IPC protocol notes:
  mpv writes async ``{"event": ...}`` lines to every connected client,
  and they can arrive before the reply to a command.  Every command is
  tagged with an incrementing ``request_id``; the reader skips event
  lines and replies with other IDs until the matching reply arrives.

Stdlib only: `subprocess`, `socket`, `json`, `ctypes` (for prctl).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import signal
import socket
import subprocess
import time
from pathlib import Path

_SOCKET_WAIT_TIMEOUT = 5.0  # seconds for mpv to create the IPC socket
_STOP_TIMEOUT = 3.0  # seconds to wait for a clean quit before SIGKILL
_IPC_TIMEOUT = 5.0  # seconds per IPC request/response

# Linux prctl constants (for PR_SET_PDEATHSIG — no orphaned mpv).
_PR_SET_PDEATHSIG = 1


class PlayerError(Exception):
    """Raised on IPC socket or protocol failures."""


# Load libc once at import time. CDLL(None) works on both glibc and musl
# (Alpine). Loading inside preexec_fn would call dlopen between fork and
# exec, which can deadlock in a multithreaded parent.
_libc = None
with contextlib.suppress(OSError, AttributeError):
    _libc = ctypes.CDLL(None, use_errno=True)


def _set_pdeathsig() -> None:
    """Set PR_SET_PDEATHSIG so mpv gets SIGTERM when the parent dies.

    Falls back silently if prctl is unavailable (non-Linux, etc.).
    Must be called from the main thread (PDEATHSIG fires when the
    forking thread exits, not the process).
    """
    if _libc is None:
        return
    with contextlib.suppress(OSError, AttributeError):
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


class Player:
    """Controls one long-running mpv process via its JSON IPC socket."""

    def __init__(self, socket_path: Path, mpv_bin: str = "mpv") -> None:
        self.socket_path = socket_path
        self.mpv_bin = mpv_bin
        self._proc: subprocess.Popen | None = None
        self._request_id: int = 0
        # The osd-overlay is owned by the IPC connection that created it (mpv
        # removes it when that connection closes), so it lives on a socket kept
        # open for the app's lifetime — unlike _send, which reconnects per
        # command.
        self._osd_sock: socket.socket | None = None

    def start(self) -> None:
        """Launch mpv and wait (up to 5s) for the IPC socket to appear.

        Creates the socket's parent directory and removes any stale
        socket file left by a crashed previous run, so the wait loop
        doesn't return immediately before mpv is actually listening.
        """
        # Ensure the parent directory exists.
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove a stale socket from a crashed previous run.
        with contextlib.suppress(OSError):
            self.socket_path.unlink()

        args = [
            self.mpv_bin,
            "--vo=gpu",
            "--gpu-context=drm",
            "--hwdec=vaapi",
            f"--input-ipc-server={self.socket_path}",
            "--idle=yes",
            "--keep-open=no",
            "--no-input-default-bindings",
            "--really-quiet",
            "--image-display-duration=inf",
        ]
        self._proc = subprocess.Popen(
            args, preexec_fn=_set_pdeathsig
        )
        deadline = time.monotonic() + _SOCKET_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                return
            if self._proc.poll() is not None:
                raise PlayerError(
                    f"mpv exited (code {self._proc.returncode}) before the IPC socket appeared"
                )
            time.sleep(0.05)
        raise TimeoutError(f"mpv IPC socket {self.socket_path} did not appear within 5 seconds")

    def stop(self) -> None:
        """Quit mpv via IPC, then wait for exit (SIGKILL after a short timeout)."""
        if self._proc is None:
            return
        with contextlib.suppress(PlayerError):  # socket may already be gone
            self._send(["quit"])
        try:
            self._proc.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._proc = None
        self._close_osd_sock()

    def _close_osd_sock(self) -> None:
        """Close the persistent OSD connection, if any."""
        if self._osd_sock is not None:
            with contextlib.suppress(OSError):
                self._osd_sock.close()
            self._osd_sock = None

    def _send(self, command: list) -> dict:
        """Send one JSON IPC command and return the parsed response.

        Attaches an incrementing ``request_id`` to the command and reads
        lines until the reply with the matching ID arrives, skipping
        async event lines and replies with other IDs.  The IPC timeout
        applies to the whole exchange.

        Raises PlayerError on socket or protocol errors, including an
        mpv response with an error other than "success".
        """
        self._request_id += 1
        rid = self._request_id
        payload = {"command": command, "request_id": rid}
        deadline = time.monotonic() + _IPC_TIMEOUT
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(_IPC_TIMEOUT)
                s.connect(str(self.socket_path))
                s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                while True:
                    # Adjust the timeout so it applies to the whole exchange,
                    # not per recv. A steady stream of events can't extend it.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"IPC timeout for {command!r}")
                    s.settimeout(remaining)
                    line = s.recv(4096).decode("utf-8", errors="replace")
                    if not line:
                        raise PlayerError(f"connection closed while waiting for reply to {command!r}")
                    # Process all complete lines in the buffer.
                    for _line in line.split("\n"):
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            msg = json.loads(_line)
                        except json.JSONDecodeError:
                            continue  # skip unparseable lines
                        if not isinstance(msg, dict):
                            continue
                        # Skip async event lines.
                        if "event" in msg:
                            continue
                        # Skip replies with a different request_id.
                        msg_rid = msg.get("request_id")
                        if msg_rid is not None and msg_rid != rid:
                            continue
                        # This is our reply.
                        if not isinstance(msg, dict) or msg.get("error", "success") != "success":
                            raise PlayerError(f"mpv error for {command!r}: {msg!r}")
                        return msg
        except (OSError, TimeoutError, ValueError) as e:
            raise PlayerError(f"IPC failure for {command!r}: {e}") from e

    def get_property(self, name: str, default: object = None) -> object:
        """Return the value of an mpv property.

        Returns ``default`` when mpv answers "property unavailable"
        (e.g. ``time-pos`` while idle).  Other errors raise PlayerError.
        """
        try:
            resp = self._send(["get_property", name])
        except PlayerError as e:
            if "property unavailable" in str(e):
                return default
            raise
        return resp.get("data", default)

    def is_idle(self) -> bool:
        """Return True if mpv is idle (no file loaded / end-of-file)."""
        return bool(self.get_property("idle-active", False))

    def is_running(self) -> bool:
        """Return True if the mpv process is alive."""
        return self._proc is not None and self._proc.poll() is None

    def show_image(self, path: Path) -> None:
        """Display a still image (e.g. the NO SIGNAL test pattern)."""
        self._send(["loadfile", str(path), "replace"])

    def load(self, path: Path, offset: float = 0.0) -> None:
        """Tune to `path` at `offset` seconds into the file (no restart).

        mpv >= 0.38 (Alpine 3.24 ships 0.41) takes ``loadfile <url> <flags>
        <index> <options>``: the playlist ``index`` (-1 = n/a with "replace")
        comes before the options, and option values must be strings. Passing
        the options dict third, or a float value, gets "invalid parameter"
        (found by `uv run task vm-e2e`).
        """
        self._send(["loadfile", str(path), "replace", -1, {"start": f"{offset:.3f}"}])

    def set_volume(self, volume: int) -> None:
        """Set mpv volume (0-100)."""
        self._send(["set_property", "volume", max(0, min(100, volume))])

    def set_mute(self, muted: bool) -> None:
        """Enable or disable mute."""
        self._send(["set_property", "mute", bool(muted)])

    def show_osd_text(self, text: str) -> None:
        """Show ``text`` as an ASS overlay (mpv ``osd-overlay``, ``ass-events``).

        ``text`` is split on newlines into one ASS event per line, each with
        its own ``\\an`` anchor (top-right flash, bottom-left volume, centred
        overlay). The overlay is persistent until :meth:`clear_osd` (or the app
        re-sends it), so the app owns expiry.
        """
        self._osd_command(
            ["osd-overlay", 0, "ass-events", text, 0, 288, 0, False, False]
        )

    def clear_osd(self) -> None:
        """Remove the ASS overlay (``osd-overlay`` with the ``none`` format)."""
        self._osd_command(["osd-overlay", 0, "none", "", 0, 288, 0, False, False])

    def _osd_command(self, command: list) -> None:
        """Send an ``osd-overlay`` command over the persistent OSD socket.

        The overlay is owned by the IPC connection that created it (mpv removes
        it when that connection closes), so it must stay on one long-lived
        socket rather than the per-command connection :meth:`_send` uses.
        """
        self._request_id += 1
        rid = self._request_id
        payload = {"command": command, "request_id": rid}
        try:
            if self._osd_sock is None:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(_IPC_TIMEOUT)
                s.connect(str(self.socket_path))
                self._osd_sock = s
            s = self._osd_sock
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            while True:
                chunk = s.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    raise PlayerError(f"OSD connection closed for {command!r}")
                for _line in chunk.split("\n"):
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        msg = json.loads(_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if "event" in msg:
                        continue
                    if msg.get("request_id") != rid:
                        continue
                    if msg.get("error", "success") != "success":
                        raise PlayerError(f"mpv error for {command!r}: {msg!r}")
                    return
        except (OSError, TimeoutError, ValueError) as e:
            self._close_osd_sock()
            raise PlayerError(f"OSD IPC failure for {command!r}: {e}") from e

    def hide_video(self) -> None:
        """Hide video (standby): mpv keeps running, screen goes black."""
        self._send(["set_property", "vid", "no"])

    def show_video(self) -> None:
        """Restore video after standby."""
        self._send(["set_property", "vid", 1])
