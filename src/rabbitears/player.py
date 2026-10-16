"""Single long-running mpv wrapper over the JSON IPC socket (SCOPE.md §4.3).

One mpv process runs for the life of the app, outputting over DRM
(`--vo=gpu --gpu-context=drm` with VAAPI hwdec).  All control goes
through mpv's JSON IPC protocol: newline-delimited JSON commands over a
Unix domain socket.  Channel changes use `loadfile ... start=<offset>`
on the *same* instance — never a process restart — targeting a channel
change under 1.5s on the Wyse 3040.

Stdlib only: `subprocess`, `socket`, `json`.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import time
from pathlib import Path

_SOCKET_WAIT_TIMEOUT = 5.0  # seconds for mpv to create the IPC socket
_STOP_TIMEOUT = 3.0  # seconds to wait for a clean quit before SIGKILL
_IPC_TIMEOUT = 5.0  # seconds per IPC request/response


class PlayerError(Exception):
    """Raised on IPC socket or protocol failures."""


class Player:
    """Controls one long-running mpv process via its JSON IPC socket."""

    def __init__(self, socket_path: Path, mpv_bin: str = "mpv") -> None:
        self.socket_path = socket_path
        self.mpv_bin = mpv_bin
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        """Launch mpv and wait (up to 5s) for the IPC socket to appear."""
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
        ]
        self._proc = subprocess.Popen(args)
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

    def _send(self, command: list) -> dict:
        """Send one JSON IPC command and return the parsed response.

        Raises PlayerError on socket or protocol errors, including an
        mpv response with an error other than "success".
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(_IPC_TIMEOUT)
                s.connect(str(self.socket_path))
                s.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
                line = s.makefile("r", encoding="utf-8").readline()
        except (OSError, TimeoutError) as e:
            raise PlayerError(f"IPC failure for {command!r}: {e}") from e
        if not line:
            raise PlayerError(f"no IPC response for {command!r}")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as e:
            raise PlayerError(f"bad IPC response for {command!r}: {e}") from e
        if not isinstance(response, dict) or response.get("error", "success") != "success":
            raise PlayerError(f"mpv error for {command!r}: {response!r}")
        return response

    def load(self, path: Path, offset: float = 0.0) -> None:
        """Tune to `path` at `offset` seconds into the file (no restart)."""
        self._send(["loadfile", str(path), "replace", {"start": offset}])

    def set_volume(self, volume: int) -> None:
        """Set mpv volume (0-100)."""
        self._send(["set_property", "volume", max(0, min(100, volume))])

    def set_mute(self, muted: bool) -> None:
        """Enable or disable mute."""
        self._send(["set_property", "mute", bool(muted)])

    def hide_video(self) -> None:
        """Hide video (standby): mpv keeps running, screen goes black."""
        self._send(["set_property", "vid", "no"])

    def show_video(self) -> None:
        """Restore video after standby."""
        self._send(["set_property", "vid", 1])
