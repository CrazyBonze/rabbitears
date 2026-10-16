#!/usr/bin/env python3
"""Rabbit Ears — virtio-gpu validation VM control (vmctl).

Boots a throwaway QEMU VM whose whole job is to prove the video path works
before we commit to it on the real Wyse 3040: **virtio-gpu** as the display
device, an SSH host-forward on 2222, and a TCP serial console on 4321.

Subcommands:

  vmctl.py run --disk DISK.qcow2 [--screenshot]
      Boot the VM and stream its serial console to stdout. Ctrl+C shuts the
      VM down cleanly (SIGTERM, then SIGKILL) and exits 0. With --screenshot,
      a background thread grabs a VNC framebuffer and saves it to a PNG
      (best-effort — a VNC failure never crashes the run).

  vmctl.py serial [--host H --port P]
      Attach to an already-running VM's serial console and forward bytes to
      stdout until the connection closes.

Stdlib only: socket, subprocess, signal, argparse, time, struct, zlib,
threading. The optional screenshot uses `pyvnc` if installed, else a minimal
raw RFB framebuffer grab — see vm/VAGRANT.md.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

SERIAL_HOST = "127.0.0.1"
SERIAL_PORT = 4321
SSH_HOSTFWD = "tcp:127.0.0.1:2222-:22"
QEMU_BIN = "qemu-system-x86_64"
_RECV_SIZE = 4096
_SERIAL_TIMEOUT = 0.1  # seconds between serial polls
VNC_HOST = "127.0.0.1"
VNC_PORT = 5900
DEFAULT_SCREENSHOT_OUT = "vm/screenshot.png"
DEFAULT_SCREENSHOT_DELAY = 15.0  # seconds to let the VM render before the grab


def qemu_args(disk: Path | str, *, mem: str = "2G", smp: str = "2") -> list[str]:
    """Return the exact qemu-system-x86_64 argv for a validation VM.

    The flag list is fixed (see vm/VAGRANT.md) so the validation is
    reproducible: virtio-gpu display, one user-mode netdev with an SSH
    host-forward, and a TCP serial server on 127.0.0.1:4321.
    """
    return [
        QEMU_BIN,
        "-m", mem,
        "-smp", smp,
        "-cpu", "max,kvm=on",
        "-enable-kvm",
        "-drive", f"file={disk},if=virtio,format=qcow2",
        "-device", "virtio-gpu-pci",
        "-netdev", f"user,id=net0,hostfwd={SSH_HOSTFWD}",
        "-device", "virtio-net-pci,netdev=net0",
        "-serial", f"tcp:{SERIAL_HOST}:{SERIAL_PORT},server,nowait",
        "-display", "none",
    ]


@dataclass
class Vm:
    """A QEMU validation VM, started and torn down as a child process.

    Usable as a context manager: ``with Vm(disk) as vm: ...`` boots on
    enter and shuts down (SIGTERM, escalating to SIGKILL) on exit.
    """

    disk: Path
    proc: subprocess.Popen | None = field(default=None, repr=False, compare=False)

    def run(self) -> None:
        """Spawn qemu-system-x86_64 with the fixed validation flag list."""
        self.proc = subprocess.Popen(qemu_args(self.disk))

    def wait(self, timeout: float | None = None) -> int:
        """Block until the VM exits; return its exit code (0 if never started)."""
        if self.proc is None:
            return 0
        return self.proc.wait(timeout=timeout)

    def close(self, term_timeout: float = 5.0, kill_timeout: float = 5.0) -> None:
        """Shut the VM down: SIGTERM, wait, escalate to SIGKILL on timeout.

        Idempotent — a no-op if the VM was never started or already exited.
        """
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()  # SIGTERM
        try:
            self.proc.wait(timeout=term_timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        self.proc.kill()  # SIGKILL
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=kill_timeout)

    def __enter__(self) -> Vm:
        self.run()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def read_serial(sock: socket.socket, timeout: float = _SERIAL_TIMEOUT) -> bytes:
    """Read available bytes (up to one line) from the serial socket.

    Returns ``b""`` on timeout (no data yet) so the caller can keep polling.
    """
    try:
        sock.settimeout(timeout)
        return sock.recv(_RECV_SIZE)
    except TimeoutError:
        return b""


def _write_stdout(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _connect_with_retry(addr: tuple[str, int], timeout: float) -> socket.socket:
    """Connect to the serial console, retrying while qemu comes up."""
    deadline = time.monotonic() + timeout
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        try:
            return socket.create_connection(addr, timeout=1.0)
        except OSError as e:
            last_err = e
            time.sleep(0.2)
    raise ConnectionError(
        f"could not connect to serial console at {addr[0]}:{addr[1]}: {last_err}"
    )


def _stream_serial(host: str, port: int, vm: Vm, connect_timeout: float = 15.0) -> None:
    """Forward the VM's serial console to stdout until the VM exits."""
    sock = _connect_with_retry((host, port), connect_timeout)
    try:
        while vm.proc is not None and vm.proc.poll() is None:
            data = read_serial(sock)
            if data:
                _write_stdout(data)
    finally:
        sock.close()


def _on_sigint(vm: Vm, signum, frame) -> None:
    """SIGINT (Ctrl+C) handler: shut the VM down cleanly, then exit 0."""
    vm.close()
    raise SystemExit(0)


# --- Screenshot (best-effort VNC framebuffer grab) -------------------------

def _rfb_recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from sock, raising if the connection closes short."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("VNC connection closed")
        buf += chunk
    return bytes(buf)


def _grab_vnc_frame_pyvnc(host: str, port: int, timeout: float):
    """Grab a framebuffer via the optional `pyvnc` package; None on any failure."""
    try:
        import pyvnc  # optional dependency  # type: ignore[import-not-found]
    except ImportError:
        return None
    client = None
    try:
        client = pyvnc.VNCClient(host, port, timeout=timeout)
        client.connect()
        screen = client.read_screen()  # best-effort; API varies by version
        rgb = bytes(screen.rgb) if hasattr(screen, "rgb") else None
        if rgb is None:
            return None
        return (int(screen.width), int(screen.height), rgb)
    except Exception:  # noqa: BLE001 - optional path; fall back to raw RFB
        return None
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def _grab_vnc_frame_raw(host: str, port: int, timeout: float):
    """Minimal raw RFB framebuffer grab (no-auth / empty-password VNCAuth).

    Returns (width, height, rgb_bytes) or None on any failure. Deliberately
    small — this is a diagnostic, not a VNC client.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        # 1. Version negotiation
        if not _rfb_recv_exact(sock, 12).startswith(b"RFB"):
            return None
        sock.sendall(b"RFB 003.008\n")
        # 2. Security handshake
        n_types = _rfb_recv_exact(sock, 1)[0]
        if n_types == 0:
            return None
        offered = _rfb_recv_exact(sock, n_types)
        if 1 in offered:  # "none"
            sock.sendall(b"\x01")
        elif 2 in offered:  # VNCAuth with an empty password
            sock.sendall(b"\x02")
            _rfb_recv_exact(sock, 16)  # challenge
            sock.sendall(bytes(16))  # empty password -> zero response
        else:
            return None
        if _rfb_recv_exact(sock, 4)[0] != 0:  # security result
            return None
        # 3. ClientInit
        _rfb_recv_exact(sock, 1)  # server shared-flag
        sock.sendall(b"\x00")  # client: don't share
        # 4. Request a 32-bit true-color format we can interpret (BGRX LE)
        sock.sendall(
            b"\x00\x00\x00\x00"
            b"\x20\x18\x00\x01"
            b"\x00\xff\x00\xff\x00\xff"
            b"\x10\x08\x00"
            b"\x00\x00\x00"
        )
        # 5. Raw encoding only
        sock.sendall(b"\x02\x00\x00\x01\x00\x00\x00\x00")
        # 6. FramebufferUpdateRequest for the whole screen
        sock.sendall(b"\x03\x00\x00\x00" + struct.pack("!HHHH", 0, 0, 0xFFFF, 0xFFFF))
        # 7. Read the update: type + padding + n_rects
        header = _rfb_recv_exact(sock, 4)
        if header[0] != 0:
            return None
        n_rects = struct.unpack("!H", header[2:4])[0]
        if n_rects == 0:
            return None
        # First rectangle header
        rect = _rfb_recv_exact(sock, 12)
        x, y, w, h = struct.unpack("!HHHH", rect[:8])
        encoding = struct.unpack("!I", rect[8:12])[0]
        if encoding != 0 or w == 0 or h == 0:
            return None
        pixels = _rfb_recv_exact(sock, w * h * 4)
    except (OSError, ConnectionError, struct.error):
        return None
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    # Convert BGRX (little-endian 32-bit) to RGB
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        rgb[i * 3] = pixels[i * 4 + 2]      # R
        rgb[i * 3 + 1] = pixels[i * 4 + 1]  # G
        rgb[i * 3 + 2] = pixels[i * 4]      # B
    return (w, h, bytes(rgb))


def _grab_vnc_frame(host: str, port: int, timeout: float = 5.0):
    """Grab one VNC framebuffer: pyvnc if available, else raw RFB. Best-effort."""
    frame = _grab_vnc_frame_pyvnc(host, port, timeout)
    if frame is not None:
        return frame
    return _grab_vnc_frame_raw(host, port, timeout)


def _save_png(path: Path | str, width: int, height: int, rgb_bytes: bytes) -> None:
    """Write an RGB image to a PNG using only stdlib (zlib + struct)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack("!I", len(data)) + body + struct.pack("!I", zlib.crc32(body))

    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # per-scanline filter type: None
        raw += rgb_bytes[y * stride:(y + 1) * stride]
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)


def _screenshot_worker(vnc_host: str, vnc_port: int, out_path: Path | str, startup_delay: float) -> None:
    """Thread target: wait for the VM, grab a VNC frame, save a PNG.

    Best-effort — any failure logs a warning and returns; it never crashes the run.
    """
    time.sleep(startup_delay)
    try:
        frame = _grab_vnc_frame(vnc_host, vnc_port)
        if frame is None:
            print("[vmctl] WARNING: VNC grab failed; no screenshot", file=sys.stderr)
            return
        width, height, rgb = frame
        _save_png(out_path, width, height, rgb)
        print(f"[vmctl] screenshot saved to {out_path}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - best-effort diagnostic, never fatal
        print(f"[vmctl] WARNING: screenshot failed: {e}", file=sys.stderr)


def start_screenshot_thread(
    vnc_host: str, vnc_port: int, out_path: Path | str, startup_delay: float
) -> threading.Thread:
    """Start a daemon thread that grabs and saves a VNC screenshot. Returns it."""
    t = threading.Thread(
        target=_screenshot_worker,
        args=(vnc_host, vnc_port, out_path, startup_delay),
        name="vmctl-screenshot",
        daemon=True,
    )
    t.start()
    return t


def cmd_run(args: argparse.Namespace) -> int:
    """Boot the VM and stream its serial console; Ctrl+C exits cleanly."""
    vm = Vm(disk=Path(args.disk))
    vm.run()
    signal.signal(signal.SIGINT, functools.partial(_on_sigint, vm))
    if args.screenshot:
        start_screenshot_thread(
            args.vnc_host, args.vnc_port, args.screenshot_out, args.screenshot_delay
        )
    try:
        _stream_serial(args.host, args.port, vm)
    finally:
        vm.close()
    return vm.wait()


def cmd_serial(args: argparse.Namespace) -> int:
    """Attach to a running VM's serial console; forward bytes until EOF."""
    sock = socket.create_connection((args.host, args.port))
    try:
        sock.settimeout(_SERIAL_TIMEOUT)
        while True:
            try:
                data = sock.recv(_RECV_SIZE)
            except TimeoutError:
                continue
            if not data:  # EOF: the VM closed the serial connection
                break
            _write_stdout(data)
    finally:
        sock.close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmctl",
        description="Boot and attach to the Rabbit Ears virtio-gpu validation VM.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Boot the VM and stream its serial console")
    run_p.add_argument("--disk", required=True, help="Path to the qcow2 disk image")
    run_p.add_argument("--host", default=SERIAL_HOST, help="Serial console host")
    run_p.add_argument("--port", type=int, default=SERIAL_PORT, help="Serial console port")
    run_p.add_argument(
        "--screenshot",
        action="store_true",
        help="Grab a VNC framebuffer and save it as a PNG (best-effort)",
    )
    run_p.add_argument(
        "--screenshot-out",
        default=DEFAULT_SCREENSHOT_OUT,
        help=f"Output PNG path for --screenshot (default: {DEFAULT_SCREENSHOT_OUT})",
    )
    run_p.add_argument(
        "--screenshot-delay",
        type=float,
        default=DEFAULT_SCREENSHOT_DELAY,
        help="Seconds to wait for the VM to render before the screenshot",
    )
    run_p.add_argument("--vnc-host", default=VNC_HOST, help="VNC host for --screenshot")
    run_p.add_argument("--vnc-port", type=int, default=VNC_PORT, help="VNC port for --screenshot")

    ser_p = sub.add_parser("serial", help="Attach to a running VM's serial console")
    ser_p.add_argument("--host", default=SERIAL_HOST, help="Serial console host")
    ser_p.add_argument("--port", type=int, default=SERIAL_PORT, help="Serial console port")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "serial":
        return cmd_serial(args)
    return 2  # pragma: no cover - argparse enforces a valid subcommand


if __name__ == "__main__":
    sys.exit(main())
