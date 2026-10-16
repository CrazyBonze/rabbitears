#!/usr/bin/env python3
"""Rabbit Ears — virtio-gpu validation VM control (vmctl).

Boots a throwaway QEMU VM whose whole job is to prove the video path works
before we commit to it on the real Wyse 3040: **virtio-gpu** as the display
device, an SSH host-forward on 2222, and a TCP serial console on 4321.

Two subcommands:

  vmctl.py run --disk DISK.qcow2
      Boot the VM and stream its serial console to stdout. Ctrl+C shuts the
      VM down cleanly (SIGTERM, then SIGKILL) and exits 0.

  vmctl.py serial [--host H --port P]
      Attach to an already-running VM's serial console and forward bytes to
      stdout until the connection closes.

Stdlib only: socket, subprocess, signal, argparse, time. No pyserial /
paramiko — the serial console is a plain TCP socket.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

SERIAL_HOST = "127.0.0.1"
SERIAL_PORT = 4321
SSH_HOSTFWD = "tcp:127.0.0.1:2222-:22"
QEMU_BIN = "qemu-system-x86_64"
_RECV_SIZE = 4096
_SERIAL_TIMEOUT = 0.1  # seconds between serial polls


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


def cmd_run(args: argparse.Namespace) -> int:
    """Boot the VM and stream its serial console; Ctrl+C exits cleanly."""
    vm = Vm(disk=Path(args.disk))
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
    run_p.add_argument("--disk", required=True, help="Path to the qcow2 disk image")
    run_p.add_argument("--host", default=SERIAL_HOST, help="Serial console host")
    run_p.add_argument("--port", type=int, default=SERIAL_PORT, help="Serial console port")

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
