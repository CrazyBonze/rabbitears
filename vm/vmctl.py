#!/usr/bin/env python3
"""Rabbit Ears — dev VM control (vmctl).

Boots the Rabbit Ears dev VM in raw QEMU (4 vCPU, 2 GB, virtio-gpu) for
developing and validating the stack before touching a real Wyse 3040.
The disk defaults to the libvirt image at
/var/lib/libvirt/images/rabbitears-vm.qcow2; no --disk argument needed.

Subcommands:

  vmctl.py run [--monitor SOCK] [--display {none,spice}]
      Boot the VM and stream its serial console to stdout. Ctrl+C shuts the
      VM down cleanly (SIGTERM, then SIGKILL) and exits 0. A QMP monitor
      socket is opened at /tmp/rabbitears-qmp.sock by default (override with
      --monitor, disable with --monitor "") so `vmctl.py screenshot` works
      out of the box. Preflight checks that the libvirt domain is not
      running and the disk is readable/writable.

  vmctl.py serial [--host H --port P]
      Attach to an already-running VM's serial console and forward bytes to
      stdout until the connection closes.

  vmctl.py screenshot [--monitor SOCK] [--out FILE.png]
      Capture a PNG from an already-running VM via the QMP `screendump`
      command on its monitor socket (default /tmp/rabbitears-qmp.sock).
      Works headless (no VNC/SPICE needed). On success prints the saved path
      and pixel dimensions; on failure prints a warning to stderr and exits 1.

Stdlib only: socket, subprocess, signal, argparse, time, json. The QMP
screendump PPM->PNG conversion uses Pillow if installed, else ImageMagick's
`convert`, else copies the PPM as-is. See vm/VAGRANT.md.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

SERIAL_HOST = "127.0.0.1"
SERIAL_PORT = 4321
SSH_HOSTFWD = "tcp:127.0.0.1:2222-:22"
QEMU_BIN = "qemu-system-x86_64"
_RECV_SIZE = 4096
_SERIAL_TIMEOUT = 0.1  # seconds between serial polls
DEFAULT_MONITOR_SOCK = "/tmp/rabbitears-qmp.sock"
DEFAULT_SCREENSHOT_OUT = "vm/screenshot.png"
DEFAULT_DISK = "/var/lib/libvirt/images/rabbitears-vm.qcow2"


def qemu_args(
    disk: Path | str,
    *,
    mem: str = "2G",
    smp: str = "4",
    monitor_sock: Path | str | None = DEFAULT_MONITOR_SOCK,
    display: str = "none",
) -> list[str]:
    """Return the exact qemu-system-x86_64 argv for a validation VM.

    The flag list is fixed (see vm/VAGRANT.md) so the validation is
    reproducible: virtio-gpu display, one user-mode netdev with an SSH
    host-forward, and a TCP serial server on 127.0.0.1:4321.

    ``monitor_sock``: exposes a QMP monitor on that Unix socket
    (``-qmp unix:<path>,server,nowait``) for headless `screendump`.
    Defaults to ``/tmp/rabbitears-qmp.sock``; pass ``None`` explicitly to
    omit the monitor entirely.
    ``display``: "none" (default) keeps ``-display none``; "spice" replaces
    it with a SPICE server on port 5910 plus the vdagent virtio-serial
    channel.
    """
    args = [
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
    ]
    if display == "spice":
        args += [
            "-spice", "port=5910,disable-ticketing=on",
            "-device", "virtio-serial-pci",
            "-chardev", "spicevmc,id=spicechannel0,name=vdagent",
            "-device", "virtserialport,chardev=spicechannel0,name=com.redhat.spice.0",
        ]
    else:
        args += ["-display", "none"]
    if monitor_sock is not None:
        args += ["-qmp", f"unix:{monitor_sock},server,nowait"]
    return args


@dataclass
class Vm:
    """A QEMU validation VM, started and torn down as a child process.

    Usable as a context manager: ``with Vm(disk) as vm: ...`` boots on
    enter and shuts down (SIGTERM, escalating to SIGKILL) on exit.
    """

    disk: Path
    proc: subprocess.Popen | None = field(default=None, repr=False, compare=False)
    monitor_sock: Path | None = field(default=None, repr=False, compare=False)
    display: str = "none"

    def run(self) -> None:
        """Spawn qemu-system-x86_64 with the fixed validation flag list."""
        self.proc = subprocess.Popen(
            qemu_args(self.disk, monitor_sock=self.monitor_sock, display=self.display)
        )

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


# --- Screenshot via QMP `screendump` (headless; no VNC/SPICE needed) ------

def _qmp_read_message(sock: socket.socket) -> dict:
    """Read one newline-delimited QMP JSON message from the monitor socket."""
    line = b""
    while not line.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("QMP connection closed before end of message")
        line += chunk
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"QMP: malformed JSON message: {e}") from e
    if not isinstance(msg, dict):
        raise RuntimeError(f"QMP: expected a JSON object, got {type(msg).__name__}")
    return msg


def _qmp_exchange(sock: socket.socket, command: str, arguments: dict | None = None) -> None:
    """Send one QMP execute command and validate the return acknowledgement.

    Raises RuntimeError on a non-empty ``error`` key or a reply without a
    ``return`` key.
    """
    payload: dict = {"execute": command}
    if arguments:
        payload["arguments"] = arguments
    sock.sendall((json.dumps(payload) + "\n").encode())
    msg = _qmp_read_message(sock)
    if msg.get("error"):
        raise RuntimeError(f"QMP error for {command}: {msg['error']}")
    if "return" not in msg:
        raise RuntimeError(f"QMP: no 'return' in reply to {command}: {msg}")


def screendump(monitor_sock: Path | str, output_path: Path | str) -> None:
    """Capture the VM framebuffer to ``output_path`` via QMP ``screendump``.

    Connects to the QMP socket at ``monitor_sock``, performs the handshake
    (read greeting, ``qmp_capabilities``), then sends ``screendump`` with
    ``filename``. QEMU writes the PPM file itself, so ``output_path`` must
    be an absolute path the QEMU process can write.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    try:
        sock.connect(str(monitor_sock))
        greeting = _qmp_read_message(sock)
        if "QMP" not in greeting:
            raise RuntimeError(f"QMP: bad server greeting: {greeting}")
        _qmp_exchange(sock, "qmp_capabilities")
        _qmp_exchange(sock, "screendump", {"filename": str(output_path)})
    finally:
        sock.close()


def ppm_to_png(ppm_path: Path | str, png_path: Path | str) -> None:
    """Convert QEMU's PPM screendump to a PNG. Never raises.

    Tries Pillow, then ImageMagick's ``convert``; if neither is available
    (or both fail), copies the PPM to ``png_path`` unchanged and prints a
    warning to stderr.
    """
    ppm_path, png_path = Path(ppm_path), Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image  # optional dependency  # type: ignore[import-not-found]

        with Image.open(ppm_path) as img:
            img.save(png_path)
        return
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - fall through to convert
        print(f"[vmctl] WARNING: PIL PPM->PNG failed ({e}); trying convert", file=sys.stderr)
    try:
        subprocess.run(["convert", str(ppm_path), str(png_path)], check=True, capture_output=True)
        return
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(
            f"[vmctl] WARNING: convert failed ({e}); copying PPM as-is to {png_path}",
            file=sys.stderr,
        )
    png_path.write_bytes(ppm_path.read_bytes())


def _ppm_dimensions(ppm_path: Path | str) -> tuple[int, int]:
    """Read (width, height) from a binary PPM (P6) header: `P6\n<w> <h>\n<maxval>\n`."""
    with open(ppm_path, "rb") as f:
        header = f.read(64)
    if not header.startswith(b"P6"):
        raise ValueError(f"{ppm_path} is not a P6 PPM file")
    width, height = (int(part) for part in header.split(b"\n")[1].split())
    return width, height


def cmd_screenshot(args: argparse.Namespace) -> int:
    """QMP screendump against a running VM: PPM -> PNG. Exits 1 on failure."""
    tmpdir = Path(tempfile.mkdtemp(prefix="vmctl-shot-"))
    ppm = tmpdir / "frame.ppm"
    try:
        screendump(args.monitor, ppm)
        width, height = _ppm_dimensions(ppm)
        ppm_to_png(ppm, args.out)
        print(f"screenshot saved: {args.out}  ({width}x{height})")
    except Exception as e:  # noqa: BLE001 - diagnostic; report and fail
        print(f"[vmctl] WARNING: QMP screendump failed: {e}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


def preflight(
    disk: Path,
    *,
    run=subprocess.run,
    access=os.access,
) -> None:
    """Preflight checks before spawning QEMU. Exits 1 with an actionable message on failure.

    Checks:
      1. The libvirt domain is not running (would lock the disk).
      2. The disk file is readable and writable by the current user.

    ``run`` and ``access`` are injectable for unit testing.
    """
    import os as _os

    # Check 1: libvirt domain must not be running.
    # Primary: virsh domstate. Fallback: pgrep (works without libvirt group).
    domain_running = False
    try:
        r = run(
            ["virsh", "-c", "qemu:///system", "domstate", "rabbitears-vm"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and "running" in r.stdout:
            domain_running = True
        elif r.returncode != 0:
            # virsh failed (e.g. permission denied); fall back to pgrep.
            try:
                pg = run(
                    ["pgrep", "-f", "guest=rabbitears-vm"],
                    capture_output=True, text=True, timeout=5,
                )
                if pg.returncode == 0 and pg.stdout.strip():
                    domain_running = True
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass  # pgrep also unavailable; can't check.
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # virsh not found at all; try pgrep.
        try:
            pg = run(
                ["pgrep", "-f", "guest=rabbitears-vm"],
                capture_output=True, text=True, timeout=5,
            )
            if pg.returncode == 0 and pg.stdout.strip():
                domain_running = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass  # can't check either way.
    if domain_running:
        print(
            "[vmctl] ERROR: libvirt domain 'rabbitears-vm' is running. "
            "Run 'uv run task vm-stop' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check 2: disk must be readable and writable.
    if not access(str(disk), _os.R_OK | _os.W_OK):
        try:
            import grp
            import pwd

            st = _os.stat(str(disk))
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
        except (OSError, KeyError, ImportError):
            owner = group = "?"
        print(
            f"[vmctl] ERROR: disk {disk} is not readable/writable by the current user "
            f"(owner: {owner}, group: {group}). "
            f"Ensure you are in the 'kvm' group (provision.sh adds you; re-login needed).",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_run(args: argparse.Namespace) -> int:
    """Boot the VM and stream its serial console; Ctrl+C exits cleanly."""
    preflight(Path(args.disk))
    if args.display == "spice":
        print(
            "[vmctl] SPICE display on port 5910 — connect with: "
            "spicy --host=127.0.0.1 --port=5910",
            file=sys.stderr,
        )
    vm = Vm(
        disk=Path(args.disk),
        monitor_sock=Path(args.monitor) if args.monitor else None,
        display=args.display,
    )
    vm.run()
    signal.signal(signal.SIGINT, functools.partial(_on_sigint, vm))
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
    run_p.add_argument("--disk", default=DEFAULT_DISK, help=f"Path to the qcow2 disk image (default: {DEFAULT_DISK})")
    run_p.add_argument("--host", default=SERIAL_HOST, help="Serial console host")
    run_p.add_argument("--port", type=int, default=SERIAL_PORT, help="Serial console port")
    run_p.add_argument(
        "--monitor",
        default=DEFAULT_MONITOR_SOCK,
        help=(
            f"Path to the QMP Unix socket for headless screendump "
            f"(default: {DEFAULT_MONITOR_SOCK}; pass --monitor '' to disable)"
        ),
    )
    run_p.add_argument(
        "--display",
        choices=["none", "spice"],
        default="none",
        help="Display device: none (default) or spice (SPICE server on port 5910)",
    )

    ser_p = sub.add_parser("serial", help="Attach to a running VM's serial console")
    ser_p.add_argument("--host", default=SERIAL_HOST, help="Serial console host")
    ser_p.add_argument("--port", type=int, default=SERIAL_PORT, help="Serial console port")

    shot_p = sub.add_parser(
        "screenshot",
        help="Capture a PNG from a running VM via QMP screendump (headless)",
    )
    shot_p.add_argument(
        "--monitor",
        default=DEFAULT_MONITOR_SOCK,
        help=f"Path to the running VM's QMP Unix socket (default: {DEFAULT_MONITOR_SOCK})",
    )
    shot_p.add_argument(
        "--out",
        default=DEFAULT_SCREENSHOT_OUT,
        help=f"Output PNG path (default: {DEFAULT_SCREENSHOT_OUT})",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "serial":
        return cmd_serial(args)
    if args.command == "screenshot":
        return cmd_screenshot(args)
    return 2  # pragma: no cover - argparse enforces a valid subcommand


if __name__ == "__main__":
    sys.exit(main())
