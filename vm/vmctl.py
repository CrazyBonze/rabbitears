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
      out of the box. Preflight checks that no QEMU already has the disk
      open, the libvirt domain is not running, and the disk is
      readable/writable. SIGTERM/SIGHUP also shut QEMU down, and QEMU gets
      PR_SET_PDEATHSIG so it can never outlive vmctl.

  vmctl.py stop [--disk DISK]
      Stop any vmctl/raw QEMU holding the disk (SIGTERM, then SIGKILL) and
      verify it died. libvirt's QEMU is left to `provision.sh stop`; the
      `vm-stop` task runs both.

  vmctl.py serial [--host H --port P]
      Attach to an already-running VM's serial console and forward bytes to
      stdout until the connection closes.

  vmctl.py screenshot [--monitor SOCK] [--out FILE.png]
      Capture a PNG from an already-running VM via the QMP `screendump`
      command on its monitor socket (default /tmp/rabbitears-qmp.sock).
      Works headless (no VNC/SPICE needed). On success prints the saved path
      and pixel dimensions; on failure prints a warning to stderr and exits 1.

Stdlib only: socket, subprocess, signal, argparse, time, json, struct,
zlib. The QMP screendump PPM->PNG conversion is a built-in stdlib encoder
(no Pillow, no ImageMagick). See vm/VAGRANT.md.
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
import struct
import subprocess
import sys
import tempfile
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
DEFAULT_MONITOR_SOCK = "/tmp/rabbitears-qmp.sock"
DEFAULT_SCREENSHOT_OUT = "vm/screenshot.png"
DEFAULT_DISK = "/var/lib/libvirt/images/rabbitears-vm.qcow2"

# Remote button name -> QEMU key qcode for `vmctl.py key` (Part 4, SCOPE §4.4).
# The qcodes mirror the evdev codes the real Adomax remote sends (docs/remote.md §2).
KEY_QCODES: dict[str, str] = {
    "up": "up",
    "down": "down",
    "ok": "ret",
    "back": "ac_back",
    "volup": "volumeup",
    "voldown": "volumedown",
    "mute": "audiomute",
    "power": "power",
    "left": "left",  # an ignored key, for the negative test
}


def qemu_args(
    disk: Path | str,
    *,
    mem: str = "2G",
    smp: str = "4",
    monitor_sock: Path | str | None = DEFAULT_MONITOR_SOCK,
    display: str = "none",
    usb_image: Path | str | None = None,
    remote: bool = True,
    remote_usb: str | None = None,
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
    channel; "vnc" replaces it with a VNC server on 127.0.0.1:5901. VNC
    mirrors QEMU's primary display surface (the same one `screendump`
    captures), so it reliably shows mpv's DRM scanout; SPICE can present a
    multi-head view (one head per guest GPU) that hides it.
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
    elif display == "vnc":
        args += ["-vnc", "127.0.0.1:1"]
    else:
        args += ["-display", "none"]
    if monitor_sock is not None:
        args += ["-qmp", f"unix:{monitor_sock},server,nowait"]
    # One qemu-xhci controller serves both the stick and the emulated remote,
    # so it's emitted exactly once when either needs it (and not at all when
    # neither does).
    if usb_image is not None or remote or remote_usb is not None:
        args += ["-device", "qemu-xhci,id=xhci"]
    if remote_usb is not None:
        # Pass the real Adomax dongle through (usb-host): the guest sees the
        # actual 0627:697d composite device — keyboard + system-control
        # interfaces — so the app's vendor scan, grab, and the physical
        # Back/hold-to-repeat all run the real hardware path (like the 3040).
        # It replaces the emulated usb-kbd; the host can't use the dongle
        # while the VM holds it. QEMU wants separate integer vendorid/productid.
        vid, pid = remote_usb.split(":")
        args += [
            "-device",
            f"usb-host,vendorid=0x{vid},productid=0x{pid},bus=xhci.0,id=remote0",
        ]
    elif remote:
        # A QEMU usb-kbd reports vendor 0x0627 — the Adomax dongle's ID — so
        # the app's real vendor scan finds it with no VM-specific branch.
        args += ["-device", "usb-kbd,bus=xhci.0,id=remote0"]
    if usb_image is not None:
        # A real USB mass-storage device (not a 9p share): the guest sees sda1
        # and the box's mdev rule mounts it by label, exactly like the 3040.
        # -blockdev (not -drive): a -drive backend is auto-deleted when its
        # device is unplugged, which would make re-plugging impossible.
        args += [
            "-blockdev", f"driver=file,filename={usb_image},node-name=usbfile",
            "-blockdev", "driver=raw,file=usbfile,node-name=usbstick",
            "-device", "usb-storage,bus=xhci.0,drive=usbstick,id=usbstick0,removable=on",
        ]
    return args


def _set_pdeathsig() -> None:
    """preexec_fn for QEMU: have the kernel SIGTERM it when vmctl exits.

    Best effort (Linux prctl via ctypes); never raises in the child.
    """
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        pr_set_pdeathsig = 1
        libc.prctl(pr_set_pdeathsig, int(signal.SIGTERM), 0, 0, 0)
    except Exception:  # noqa: BLE001, S110 - best effort; QEMU must still start
        pass


# --- Who holds the disk? ------------------------------------------------------

@dataclass
class Holder:
    """A QEMU process that has the VM disk open."""

    pid: int
    argv: list[str]
    libvirt: bool  # started by libvirt (provision.sh), not by vmctl

    def describe(self) -> str:
        who = "libvirt domain" if self.libvirt else "vmctl/raw QEMU"
        return f"pid {self.pid} ({who}): {' '.join(self.argv)[:160]}"


def find_disk_holders(
    disk: Path | str, proc_root: Path | str = "/proc", self_pid: int | None = None
) -> list[Holder]:
    """Return the QEMU processes using ``disk``.

    Matches QEMU processes whose command line mentions the disk path, or (for
    processes we may inspect) that have it open. Reads /proc directly, so it
    can't match its own command line the way ``pkill -f`` does.
    """
    disk = str(disk)
    self_pid = os.getpid() if self_pid is None else self_pid
    holders: list[Holder] = []
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return holders
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == self_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue  # exited, or not ours to read
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv or "qemu" not in os.path.basename(argv[0]):
            continue
        uses_disk = any(disk in a for a in argv)
        if not uses_disk:
            with contextlib.suppress(OSError):
                for fd in (entry / "fd").iterdir():
                    with contextlib.suppress(OSError):
                        if os.readlink(fd) == disk:
                            uses_disk = True
                            break
        if uses_disk:
            libvirt = any(a.startswith("guest=") for a in argv)
            holders.append(Holder(pid=int(entry.name), argv=argv, libvirt=libvirt))
    return sorted(holders, key=lambda h: h.pid)


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` exists and isn't a zombie (a zombie holds no files)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, but not ours
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def stop_holders(
    holders: list[Holder],
    *,
    term_timeout: float = 10.0,
    kill_timeout: float = 5.0,
    kill=os.kill,
    alive=_pid_alive,
    sleep=time.sleep,
    clock=time.monotonic,
) -> list[Holder]:
    """SIGTERM each holder, escalate to SIGKILL, and verify it died.

    Returns the holders that are still alive (empty on success). Injectable
    ``kill``/``alive``/``sleep``/``clock`` for unit tests.
    """
    pending = list(holders)
    for sig, timeout in ((signal.SIGTERM, term_timeout), (signal.SIGKILL, kill_timeout)):
        for h in pending:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                kill(h.pid, sig)
        deadline = clock() + timeout
        while pending and clock() < deadline:
            pending = [h for h in pending if alive(h.pid)]
            if pending:
                sleep(0.2)
        pending = [h for h in pending if alive(h.pid)]
        if not pending:
            return []
    return pending


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
    usb_image: Path | str | None = field(default=None, repr=False, compare=False)
    remote: bool = True
    remote_usb: str | None = field(default=None, repr=False, compare=False)

    def run(self) -> None:
        """Spawn qemu-system-x86_64 with the fixed validation flag list.

        The child gets PR_SET_PDEATHSIG, so the kernel stops QEMU if vmctl
        dies for any reason (even SIGKILL): a QEMU must never outlive the
        vmctl that started it, still holding the disk.
        """
        self.proc = subprocess.Popen(
            qemu_args(
                self.disk,
                monitor_sock=self.monitor_sock,
                remote_usb=self.remote_usb,
                display=self.display,
                usb_image=self.usb_image,
                remote=self.remote,
            ),
            preexec_fn=_set_pdeathsig,
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


def _qmp_exchange(
    sock: socket.socket, command: str, arguments: dict | None = None
) -> object:
    """Send one QMP execute command and return its ``return`` value.

    QMP also sends async ``{"event": ...}`` messages on the same socket, and
    they can arrive before (or interleaved with) the reply. This skips event
    messages and keeps reading until the reply with a ``return`` or ``error``
    key arrives. Raises RuntimeError on a non-empty ``error`` key.
    """
    payload: dict = {"execute": command}
    if arguments:
        payload["arguments"] = arguments
    sock.sendall((json.dumps(payload) + "\n").encode())
    while True:
        msg = _qmp_read_message(sock)
        if "event" in msg:
            continue  # async event, not the reply to our command
        if msg.get("error"):
            raise RuntimeError(f"QMP error for {command}: {msg['error']}")
        if "return" in msg:
            return msg["return"]
        # Any other shape is unexpected; keep waiting for the real reply.


def _qmp_connect(monitor_sock: Path | str) -> socket.socket:
    """Connect to the QMP monitor and complete the handshake.

    Creates the Unix socket, reads the server greeting, and sends
    ``qmp_capabilities``. Returns the connected socket (the caller closes it).
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    sock.connect(str(monitor_sock))
    greeting = _qmp_read_message(sock)
    if "QMP" not in greeting:
        sock.close()
        raise RuntimeError(f"QMP: bad server greeting: {greeting}")
    _qmp_exchange(sock, "qmp_capabilities")
    return sock


def _qmp_wait_event(
    sock: socket.socket, event_name: str, timeout: float = 10.0
) -> dict:
    """Read QMP messages until the named event arrives; return it.

    Skips other events and stray replies. Polls in small increments so the
    overall ``timeout`` is respected. Raises TimeoutError on timeout.
    """
    deadline = time.monotonic() + timeout
    sock.settimeout(1.0)
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"QMP: timed out waiting for {event_name}")
        try:
            msg = _qmp_read_message(sock)
        except TimeoutError:
            continue  # no data yet (socket.timeout is TimeoutError); poll again
        if msg.get("event") == event_name:
            return msg


def screendump(monitor_sock: Path | str, output_path: Path | str) -> None:
    """Capture the VM framebuffer to ``output_path`` via QMP ``screendump``.

    Connects to the QMP socket at ``monitor_sock``, performs the handshake,
    then sends ``screendump`` with ``filename``. QEMU writes the PPM file
    itself, so ``output_path`` must be an absolute path the QEMU process can
    write.
    """
    sock = _qmp_connect(monitor_sock)
    try:
        _qmp_exchange(sock, "screendump", {"filename": str(output_path)})
    finally:
        sock.close()


def _read_ppm(path: Path | str) -> tuple[int, int, bytes]:
    """Parse a binary PPM (P6) file; return (width, height, RGB bytes).

    The header is parsed as whitespace-separated tokens (magic, width,
    height, maxval); ``#`` comment lines are skipped. Raises ValueError on
    any violation: wrong magic, maxval != 255, non-positive dimensions, or
    pixel data that is not exactly width * height * 3 bytes after the single
    whitespace byte that ends the header.
    """
    data = Path(path).read_bytes()
    pos = 0
    tokens: list[bytes] = []
    for _ in range(4):
        # Skip whitespace; a ``#`` starts a comment running to end of line.
        while pos < len(data):
            byte = data[pos:pos + 1]
            if byte.isspace():
                pos += 1
            elif byte == b"#":
                while pos < len(data) and data[pos:pos + 1] != b"\n":
                    pos += 1
            else:
                break
        start = pos
        while pos < len(data) and not data[pos:pos + 1].isspace():
            pos += 1
        tokens.append(data[start:pos])
    if pos >= len(data) or not data[pos:pos + 1].isspace():
        raise ValueError(f"{path}: PPM header is truncated")
    pos += 1  # the single whitespace byte that ends the header

    magic, w_tok, h_tok, maxval_tok = tokens
    if magic != b"P6":
        raise ValueError(f"{path}: not a P6 PPM file (magic {magic!r})")
    try:
        width, height, maxval = int(w_tok), int(h_tok), int(maxval_tok)
    except ValueError as e:
        raise ValueError(f"{path}: non-numeric PPM header field") from e
    if width <= 0 or height <= 0:
        raise ValueError(f"{path}: non-positive PPM dimensions {width}x{height}")
    if maxval != 255:
        raise ValueError(f"{path}: unsupported PPM maxval {maxval} (need 255)")
    expected = width * height * 3
    pixels = data[pos:]
    if len(pixels) != expected:
        raise ValueError(
            f"{path}: expected {expected} pixel bytes for {width}x{height}, "
            f"got {len(pixels)}"
        )
    return width, height, pixels


def _encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode 8-bit RGB pixels as a PNG (colour type 2, filter 0 per row).

    Same pattern as _save_png in tools/generate_testpattern.py; duplicated
    on purpose — both files are standalone scripts loaded by path.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack("!I", len(data)) + body + struct.pack("!I", zlib.crc32(body))

    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # per-scanline filter type: None
        raw += rgb[y * stride:(y + 1) * stride]
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def ppm_to_png(ppm_path: Path | str, png_path: Path | str) -> tuple[int, int]:
    """Convert a PPM screendump to an 8-bit RGB PNG. Returns (width, height).

    Writes ``png_path`` atomically (temp file in the same directory, then
    ``os.replace``). Raises on any failure; a pre-existing ``png_path`` is
    left untouched.
    """
    ppm_path, png_path = Path(ppm_path), Path(png_path)
    width, height, rgb = _read_ppm(ppm_path)
    png = _encode_png(width, height, rgb)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(png_path.parent), prefix=".ppm2png-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        os.chmod(tmp, 0o644)
        os.replace(tmp, png_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return width, height


def cmd_screenshot(args: argparse.Namespace) -> int:
    """QMP screendump against a running VM: PPM -> PNG. Exits 1 on failure."""
    tmpdir = Path(tempfile.mkdtemp(prefix="vmctl-shot-"))
    ppm = tmpdir / "frame.ppm"
    try:
        screendump(args.monitor, ppm)
        width, height = ppm_to_png(ppm, args.out)
        print(f"screenshot saved: {args.out}  ({width}x{height})")
    except Exception as e:  # noqa: BLE001 - diagnostic; report and fail
        print(f"[vmctl] WARNING: QMP screendump failed: {e}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


def _open_monitor(monitor_sock: Path | str, hint: str) -> socket.socket | None:
    """Connect to the QMP monitor; return the socket or None (after an error)."""
    try:
        return _qmp_connect(monitor_sock)
    except (OSError, ConnectionError, RuntimeError) as e:
        print(
            f"[vmctl] ERROR: could not reach the QMP monitor at {monitor_sock}: {e}. {hint}",
            file=sys.stderr,
        )
        return None


def _device_del(sock: socket.socket, device_id: str) -> None:
    """QMP ``device_del`` and wait for the DEVICE_DELETED event."""
    _qmp_exchange(sock, "device_del", {"id": device_id})
    _qmp_wait_event(sock, "DEVICE_DELETED", timeout=10.0)


def _device_add(sock: socket.socket, props: dict) -> None:
    """QMP ``device_add``."""
    _qmp_exchange(sock, "device_add", props)


def _usb_error(action: str, msg: str) -> None:
    """Translate a QMP error on usb plug/unplug into a clear message."""
    low = msg.lower()
    if "not found" in low:
        if action == "unplug":
            print(
                "[vmctl] ERROR: nothing to unplug — the stick is not attached "
                "(was the VM started with --usb, or is it already unplugged?)",
                file=sys.stderr,
            )
        else:
            print(
                "[vmctl] ERROR: cannot plug — the VM was not started with --usb "
                "(no usbstick blockdev / xhci bus).",
                file=sys.stderr,
            )
    elif "in use" in low or "already" in low:
        print("[vmctl] ERROR: the stick is already plugged.", file=sys.stderr)
    else:
        print(f"[vmctl] ERROR: usb {action} failed: {msg}", file=sys.stderr)


def cmd_usb(args: argparse.Namespace) -> int:
    """Attach/detach the mock USB stick on a running VM via QMP.

    unplug: ``device_del`` usbstick0, then wait for the DEVICE_DELETED event.
    plug:   ``device_add`` a usb-storage device on the xhci bus.
    """
    sock = _open_monitor(args.monitor, "Is the VM running (uv run task vm-run-usb)?")
    if sock is None:
        return 1
    try:
        if args.action == "unplug":
            _device_del(sock, "usbstick0")
            print("usb stick unplugged (usbstick0 removed)")
        else:  # plug
            _device_add(sock, {
                "driver": "usb-storage",
                "bus": "xhci.0",
                "drive": "usbstick",
                "id": "usbstick0",
                "removable": True,
            })
            print("usb stick plugged (usbstick0 added)")
    except RuntimeError as e:
        _usb_error(args.action, str(e))
        return 1
    except TimeoutError as e:
        print(f"[vmctl] ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


def cmd_remote(args: argparse.Namespace) -> int:
    """Attach/detach the emulated remote (usb-kbd, id=remote0) via QMP."""
    sock = _open_monitor(args.monitor, "Is the VM running (uv run task vm-run)?")
    if sock is None:
        return 1
    try:
        if args.action == "unplug":
            _device_del(sock, "remote0")
            print("remote unplugged (remote0 removed)")
        else:  # plug
            _device_add(sock, {
                "driver": "usb-kbd",
                "bus": "xhci.0",
                "id": "remote0",
            })
            print("remote plugged (remote0 added)")
    except RuntimeError as e:
        print(f"[vmctl] ERROR: remote {args.action} failed: {e}", file=sys.stderr)
        return 1
    except TimeoutError as e:
        print(f"[vmctl] ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


def _key_event(qcode: str, down: bool) -> dict:
    """One QMP ``input-send-event`` key event (qcode, down/up)."""
    return {
        "type": "key",
        "data": {"key": {"type": "qcode", "data": qcode}, "down": down},
    }


def cmd_key(args: argparse.Namespace) -> int:
    """Press a remote button: QMP ``input-send-event`` key down, hold, key up.

    Unknown button names exit 2 with the list of valid names.
    """
    qcode = KEY_QCODES.get(args.button)
    if qcode is None:
        print(
            f"[vmctl] ERROR: unknown button {args.button!r}. Valid: "
            f"{', '.join(KEY_QCODES)}",
            file=sys.stderr,
        )
        return 2
    sock = _open_monitor(args.monitor, "Is the VM running (uv run task vm-run)?")
    if sock is None:
        return 1
    try:
        _qmp_exchange(sock, "input-send-event", {"events": [_key_event(qcode, True)]})
        time.sleep(args.hold)
        _qmp_exchange(sock, "input-send-event", {"events": [_key_event(qcode, False)]})
        print(f"pressed {args.button} (qcode {qcode}, hold {args.hold:.2f}s)")
    except (RuntimeError, TimeoutError) as e:
        print(f"[vmctl] ERROR: key {args.button} failed: {e}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


def cmd_powerbutton(args: argparse.Namespace) -> int:
    """Press the ACPI power button via QMP ``system_powerdown`` (Part 3 test)."""
    sock = _open_monitor(args.monitor, "Is the VM running (uv run task vm-run)?")
    if sock is None:
        return 1
    try:
        _qmp_exchange(sock, "system_powerdown")
        print("ACPI power button pressed (system_powerdown)")
    except (RuntimeError, TimeoutError) as e:
        print(f"[vmctl] ERROR: powerbutton failed: {e}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


def preflight(
    disk: Path,
    *,
    run=subprocess.run,
    access=os.access,
    find_holders=find_disk_holders,
) -> None:
    """Preflight checks before spawning QEMU. Exits 1 with an actionable message on failure.

    Checks:
      0. No QEMU (a stale `vm-run`, or libvirt's) already has the disk open.
      1. The libvirt domain is not running (would lock the disk).
      2. The disk file is readable and writable by the current user.

    ``run``, ``access`` and ``find_holders`` are injectable for unit testing.
    """
    import os as _os

    # Check 0: two QEMUs on one qcow2 corrupt it (docs/vm-e2e-blockers.md).
    holders = find_holders(disk)
    if holders:
        print(f"[vmctl] ERROR: the disk {disk} is already in use:", file=sys.stderr)
        for h in holders:
            print(f"  {h.describe()}", file=sys.stderr)
        print("Stop it first: uv run task vm-stop", file=sys.stderr)
        sys.exit(1)

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
    elif args.display == "vnc":
        print(
            "[vmctl] VNC display on 127.0.0.1:5901 — connect with: "
            "vinagre vnc://127.0.0.1:5901",
            file=sys.stderr,
        )
    vm = Vm(
        disk=Path(args.disk),
        monitor_sock=Path(args.monitor) if args.monitor else None,
        display=args.display,
        usb_image=Path(args.usb) if args.usb else None,
        remote=args.remote,
        remote_usb=args.remote_usb,
    )
    vm.run()
    # Ctrl+C, `kill`, and a closed terminal all shut QEMU down cleanly.
    # (If vmctl is SIGKILLed, PDEATHSIG stops QEMU instead.)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, functools.partial(_on_sigint, vm))
    try:
        _stream_serial(args.host, args.port, vm)
    finally:
        vm.close()
    return vm.wait()


def cmd_stop(args: argparse.Namespace, *, find_holders=find_disk_holders,
             stop=stop_holders) -> int:
    """Stop every vmctl/raw QEMU holding the disk, and verify each one died.

    libvirt's QEMU is left to `provision.sh stop` (the vm-stop task runs both).
    Exits 1 if a QEMU survives SIGKILL or can't be signalled.
    """
    holders = find_holders(args.disk)
    raw = [h for h in holders if not h.libvirt]
    for h in holders:
        if h.libvirt:
            print(f"[vmctl] {h.describe()} — left to provision.sh stop")
    if not raw:
        print(f"[vmctl] no vmctl QEMU is using {args.disk}")
        return 0
    for h in raw:
        print(f"[vmctl] stopping {h.describe()}")
    survivors = stop(raw)
    if survivors:
        for h in survivors:
            print(f"[vmctl] ERROR: still running after SIGKILL (or not ours to kill): "
                  f"{h.describe()}", file=sys.stderr)
        return 1
    print(f"[vmctl] stopped {len(raw)} QEMU process(es); the disk is free")
    return 0


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
        choices=["none", "spice", "vnc"],
        default="none",
        help=(
            "Display device: none (default), spice (SPICE server on port 5910), "
            "or vnc (VNC server on 127.0.0.1:5901)"
        ),
    )
    run_p.add_argument(
        "--usb",
        default=None,
        help=(
            "Path to a USB stick image to attach as USB mass storage "
            "(e.g. /var/lib/rabbitears-vm/usb.img)"
        ),
    )
    run_p.add_argument(
        "--remote",
        dest="remote",
        action="store_true",
        default=True,
        help="Attach the emulated remote (usb-kbd, vendor 0x0627). Default.",
    )
    run_p.add_argument(
        "--no-remote",
        dest="remote",
        action="store_false",
        help="Do not attach the emulated remote.",
    )
    run_p.add_argument(
        "--remote-usb",
        default=None,
        metavar="VID:PID",
        help=(
            "Pass a real USB remote dongle through (usb-host) instead of the "
            "emulated usb-kbd, e.g. 0627:697d for the Adomax"
        ),
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

    usb_p = sub.add_parser(
        "usb",
        help="Attach/detach the mock USB stick on a running VM (unplug|plug)",
    )
    usb_p.add_argument("action", choices=["unplug", "plug"])
    usb_p.add_argument(
        "--monitor",
        default=DEFAULT_MONITOR_SOCK,
        help=f"Path to the running VM's QMP Unix socket (default: {DEFAULT_MONITOR_SOCK})",
    )

    key_p = sub.add_parser(
        "key",
        help="Press a remote button on the running VM (e.g. up, down, volup, power)",
    )
    key_p.add_argument("button", help=f"Button name: {', '.join(KEY_QCODES)}")
    key_p.add_argument("--hold", type=float, default=0.1, help="Seconds to hold the key (default 0.1)")
    key_p.add_argument(
        "--monitor",
        default=DEFAULT_MONITOR_SOCK,
        help=f"Path to the running VM's QMP Unix socket (default: {DEFAULT_MONITOR_SOCK})",
    )

    remote_p = sub.add_parser(
        "remote",
        help="Attach/detach the emulated remote (usb-kbd) on a running VM (unplug|plug)",
    )
    remote_p.add_argument("action", choices=["unplug", "plug"])
    remote_p.add_argument(
        "--monitor",
        default=DEFAULT_MONITOR_SOCK,
        help=f"Path to the running VM's QMP Unix socket (default: {DEFAULT_MONITOR_SOCK})",
    )

    power_p = sub.add_parser(
        "powerbutton",
        help="Press the ACPI power button (QMP system_powerdown) to test the override",
    )
    power_p.add_argument(
        "--monitor",
        default=DEFAULT_MONITOR_SOCK,
        help=f"Path to the running VM's QMP Unix socket (default: {DEFAULT_MONITOR_SOCK})",
    )

    stop_p = sub.add_parser(
        "stop",
        help="Stop any vmctl QEMU using the disk (SIGTERM, then SIGKILL) and verify it died",
    )
    stop_p.add_argument("--disk", default=DEFAULT_DISK, help=f"Disk image (default: {DEFAULT_DISK})")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "serial":
        return cmd_serial(args)
    if args.command == "screenshot":
        return cmd_screenshot(args)
    if args.command == "usb":
        return cmd_usb(args)
    if args.command == "key":
        return cmd_key(args)
    if args.command == "remote":
        return cmd_remote(args)
    if args.command == "powerbutton":
        return cmd_powerbutton(args)
    return 2  # pragma: no cover - argparse enforces a valid subcommand


if __name__ == "__main__":
    sys.exit(main())
