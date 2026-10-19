"""Unit tests for vm/vmctl.py (virtio-gpu validation VM control).

subprocess.Popen and the serial socket are mocked everywhere — no real QEMU
or network in tests.

vm/vmctl.py is a standalone script (not a package), so it is loaded by file
path rather than imported by name.
"""

import functools
import importlib.util
import io
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_VMCTL_PATH = Path(__file__).resolve().parent.parent / "vm" / "vmctl.py"
_spec = importlib.util.spec_from_file_location("vmctl", _VMCTL_PATH)
assert _spec is not None and _spec.loader is not None
vmctl = importlib.util.module_from_spec(_spec)
sys.modules["vmctl"] = vmctl  # register before exec so @dataclass can resolve it
_spec.loader.exec_module(vmctl)


def _decode_png(data: bytes) -> tuple[int, int, int, int, bytes]:
    """Decode an 8-bit RGB (colour type 2) PNG using filter type 0 only.

    Returns (width, height, bit_depth, colour_type, rgb_bytes). Checks the
    PNG signature, walks the chunks verifying each CRC, concatenates and
    decompresses the IDAT data, and raises AssertionError on any other
    filter type.
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("not a PNG: bad signature")
    pos = 8
    width: int | None = None
    height: int | None = None
    bit_depth: int | None = None
    colour_type: int | None = None
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack("!I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack("!I", data[pos + 8 + length:pos + 12 + length])
        if zlib.crc32(tag + body) & 0xFFFFFFFF != crc:
            raise AssertionError(f"bad CRC in chunk {tag!r}")
        if tag == b"IHDR":
            width, height, bit_depth, colour_type = struct.unpack("!IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length
    if width is None or height is None or bit_depth is None or colour_type is None:
        raise AssertionError("no IHDR chunk")
    if bit_depth != 8 or colour_type != 2:
        raise AssertionError(f"unsupported PNG: depth {bit_depth}, colour type {colour_type}")
    raw = zlib.decompress(idat)
    stride = width * 3
    if len(raw) != height * (stride + 1):
        raise AssertionError("decompressed IDAT length does not match dimensions")
    rgb = bytearray()
    for y in range(height):
        row = raw[y * (stride + 1):(y + 1) * (stride + 1)]
        if row[0] != 0:
            raise AssertionError(f"unsupported filter type {row[0]} on row {y}")
        rgb += row[1:]
    return width, height, bit_depth, colour_type, bytes(rgb)


class VmRunTest(unittest.TestCase):
    def test_run_spawns_qemu_with_exact_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "disk.qcow2"
            expected = [
                "qemu-system-x86_64",
                "-m", "2G",
                "-smp", "4",
                "-cpu", "max,kvm=on",
                "-enable-kvm",
                "-drive", f"file={disk},if=virtio,format=qcow2",
                "-device", "virtio-gpu-pci",
                "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22",
                "-device", "virtio-net-pci,netdev=net0",
                "-serial", "tcp:127.0.0.1:4321,server,nowait",
                "-display", "none",
            ]
            with mock.patch.object(vmctl.subprocess, "Popen") as popen:
                vm = vmctl.Vm(disk=disk)
                vm.run()
            popen.assert_called_once()
            self.assertEqual(popen.call_args[0][0], expected)

    def test_run_disk_defaults_to_libvirt_image(self):
        # The --disk argument defaults to the libvirt image path.
        self.assertEqual(vmctl.DEFAULT_DISK, "/var/lib/libvirt/images/rabbitears-vm.qcow2")
        parser = vmctl._build_parser()
        args = parser.parse_args(["run"])
        self.assertEqual(args.disk, vmctl.DEFAULT_DISK)


class PreflightTest(unittest.TestCase):
    def test_preflight_refuses_when_libvirt_domain_running(self):
        """preflight exits 1 when virsh reports the domain as running."""
        disk = Path("/var/lib/libvirt/images/rabbitears-vm.qcow2")
        fake_run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="running\n"))
        fake_access = mock.Mock(return_value=True)
        with self.assertRaises(SystemExit) as ctx:
            vmctl.preflight(disk, run=fake_run, access=fake_access)
        self.assertEqual(ctx.exception.code, 1)

    def test_preflight_skips_domain_check_when_virsh_missing(self):
        """preflight skips the domain check when virsh is not found."""
        disk = Path("/var/lib/libvirt/images/rabbitears-vm.qcow2")
        fake_run = mock.Mock(side_effect=FileNotFoundError("virsh not found"))
        fake_access = mock.Mock(return_value=True)
        # Should not raise — virsh missing is OK, disk is writable.
        vmctl.preflight(disk, run=fake_run, access=fake_access)

    def test_preflight_refuses_unwritable_disk(self):
        """preflight exits 1 when the disk is not readable/writable."""
        disk = Path("/var/lib/libvirt/images/rabbitears-vm.qcow2")
        fake_run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="shut off\n"))
        fake_access = mock.Mock(return_value=False)
        with self.assertRaises(SystemExit) as ctx:
            vmctl.preflight(disk, run=fake_run, access=fake_access)
        self.assertEqual(ctx.exception.code, 1)

    def test_preflight_passes_when_domain_off_and_disk_writable(self):
        """preflight passes when the domain is shut off and the disk is writable."""
        disk = Path("/var/lib/libvirt/images/rabbitears-vm.qcow2")
        fake_run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="shut off\n"))
        fake_access = mock.Mock(return_value=True)
        # Should not raise.
        vmctl.preflight(disk, run=fake_run, access=fake_access)


class VmWaitTest(unittest.TestCase):
    def test_wait_returns_zero_on_clean_exit(self):
        proc = mock.Mock()
        proc.wait.return_value = 0
        self.assertEqual(vmctl.Vm(disk=Path("x"), proc=proc).wait(), 0)

    def test_wait_returns_nonzero_exit_code(self):
        proc = mock.Mock()
        proc.wait.return_value = 3
        self.assertEqual(vmctl.Vm(disk=Path("x"), proc=proc).wait(), 3)

    def test_wait_returns_zero_when_never_started(self):
        self.assertEqual(vmctl.Vm(disk=Path("x")).wait(), 0)


class VmCloseTest(unittest.TestCase):
    def test_close_terminates_then_kills_on_timeout(self):
        proc = mock.Mock()
        proc.poll.return_value = None  # still running
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="qemu", timeout=5),
            0,
        ]
        vmctl.Vm(disk=Path("x"), proc=proc).close()
        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()

    def test_close_no_kill_when_terminates_cleanly(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        vmctl.Vm(disk=Path("x"), proc=proc).close()
        proc.terminate.assert_called_once_with()
        proc.kill.assert_not_called()

    def test_close_noop_when_already_exited(self):
        proc = mock.Mock()
        proc.poll.return_value = 0  # already exited
        vmctl.Vm(disk=Path("x"), proc=proc).close()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_close_noop_when_never_started(self):
        vmctl.Vm(disk=Path("x")).close()  # must not raise


class VmContextManagerTest(unittest.TestCase):
    def test_enter_runs_and_exit_closes(self):
        with mock.patch.object(vmctl.subprocess, "Popen") as popen:
            proc = popen.return_value
            proc.poll.return_value = 0  # already exited by the time we close
            with vmctl.Vm(disk=Path("x")) as vm:
                self.assertIs(vm.proc, proc)
            proc.terminate.assert_not_called()  # close() saw it already exited


class ReadSerialTest(unittest.TestCase):
    def test_read_serial_returns_line_when_available(self):
        sock = mock.Mock()
        sock.recv.return_value = b"CH 03\n"
        self.assertEqual(vmctl.read_serial(sock), b"CH 03\n")

    def test_read_serial_returns_empty_on_timeout(self):
        sock = mock.Mock()
        sock.recv.side_effect = TimeoutError
        self.assertEqual(vmctl.read_serial(sock), b"")


class SigintHandlerTest(unittest.TestCase):
    def test_handler_closes_vm_and_exits_zero(self):
        vm = mock.Mock()
        with self.assertRaises(SystemExit) as ctx:
            vmctl._on_sigint(vm, signal.SIGINT, None)
        self.assertEqual(ctx.exception.code, 0)
        vm.close.assert_called_once_with()

    def test_handler_as_registered_partial(self):
        # cmd_run registers functools.partial(_on_sigint, vm) as the SIGINT
        # handler; verify that partial behaves the same way.
        vm = mock.Mock()
        handler = functools.partial(vmctl._on_sigint, vm)
        with self.assertRaises(SystemExit) as ctx:
            handler(signal.SIGINT, None)
        self.assertEqual(ctx.exception.code, 0)
        vm.close.assert_called_once_with()


class CmdSerialTest(unittest.TestCase):
    def test_serial_forwards_bytes_until_eof(self):
        sock = mock.Mock()
        sock.recv.side_effect = [b"hello", b"world", b""]  # last b"" is EOF
        args = SimpleNamespace(host="127.0.0.1", port=4321)
        with mock.patch.object(vmctl.socket, "create_connection", return_value=sock) as cc, \
                mock.patch.object(vmctl.sys, "stdout") as stdout:
            rc = vmctl.cmd_serial(args)
        self.assertEqual(rc, 0)
        cc.assert_called_once_with(("127.0.0.1", 4321))
        stdout.buffer.write.assert_any_call(b"hello")
        stdout.buffer.write.assert_any_call(b"world")
        sock.close.assert_called_once_with()

    def test_serial_continues_past_timeout(self):
        sock = mock.Mock()
        sock.recv.side_effect = [TimeoutError, b"ok\n", b""]
        args = SimpleNamespace(host="127.0.0.1", port=4321)
        with mock.patch.object(vmctl.socket, "create_connection", return_value=sock), \
                mock.patch.object(vmctl.sys, "stdout") as stdout:
            vmctl.cmd_serial(args)
        stdout.buffer.write.assert_called_once_with(b"ok\n")


class QmpScreendumpTest(unittest.TestCase):
    def test_qemu_args_monitor_default(self):
        # No monitor_sock argument -> the default QMP socket is exposed.
        args = vmctl.qemu_args(Path("disk.qcow2"))
        self.assertIn("-qmp", args)
        self.assertIn("unix:/tmp/rabbitears-qmp.sock,server,nowait", args)

    def test_qemu_args_monitor_override(self):
        args = vmctl.qemu_args(Path("disk.qcow2"), monitor_sock=Path("/tmp/other.sock"))
        self.assertIn("-qmp", args)
        self.assertIn("unix:/tmp/other.sock,server,nowait", args)
        self.assertNotIn("unix:/tmp/rabbitears-qmp.sock,server,nowait", args)

    def test_qemu_args_monitor_disabled(self):
        # Explicit None is the escape hatch for `--monitor ""`: no -qmp at all.
        args = vmctl.qemu_args(Path("disk.qcow2"), monitor_sock=None)
        self.assertNotIn("-qmp", args)

    def test_qemu_args_display_none_default(self):
        args = vmctl.qemu_args(Path("disk.qcow2"))
        self.assertIn("-display", args)
        self.assertIn("none", args)

    def test_qemu_args_display_spice(self):
        args = vmctl.qemu_args(Path("disk.qcow2"), display="spice")
        self.assertIn("-spice", args)
        self.assertIn("port=5910,disable-ticketing=on", args)
        self.assertIn("-device", args)
        self.assertIn("virtio-serial-pci", args)
        self.assertIn("-chardev", args)
        self.assertIn("spicevmc,id=spicechannel0,name=vdagent", args)
        self.assertIn("virtserialport,chardev=spicechannel0,name=com.redhat.spice.0", args)
        # -display none must be absent in spice mode
        self.assertNotIn("-display", args)
        self.assertNotIn("none", args)

    def _mock_qmp_socket(self, messages: list[bytes]):
        """Build a mock socket whose recv() feeds one byte at a time from messages."""
        sock = mock.Mock()
        stream = b"".join(messages)
        pos = 0

        def recv(n: int) -> bytes:
            nonlocal pos
            chunk = stream[pos:pos + n]
            pos += n
            return chunk

        sock.recv.side_effect = recv
        return sock

    def test_screendump_sends_correct_qmp_sequence(self):
        greeting = b'{"QMP": {"version": {"qemu": {"micro": 0, "minor": 2, "major": 8}}, "capabilities": ["oob"]}}\n'
        ack = b'{"return": {}}\n'
        sock = self._mock_qmp_socket([greeting, ack, ack])
        with mock.patch.object(vmctl.socket, "socket", return_value=sock):
            vmctl.screendump("/tmp/qmp.sock", "/tmp/frame.ppm")  # noqa: B108
        sent = [c.args[0].decode() for c in sock.sendall.call_args_list]
        self.assertIn("qmp_capabilities", sent[0])
        self.assertIn("screendump", sent[1])
        self.assertIn("/tmp/frame.ppm", sent[1])  # noqa: B108
        sock.connect.assert_called_once_with("/tmp/qmp.sock")  # noqa: B108

    def test_screendump_raises_on_protocol_error(self):
        greeting = b'{"QMP": {"version": {}, "capabilities": []}}\n'
        ack = b'{"return": {}}\n'
        err = b'{"error": {"class": "CommandNotFound", "desc": "screendump"}}\n'
        sock = self._mock_qmp_socket([greeting, ack, err])
        with mock.patch.object(vmctl.socket, "socket", return_value=sock), \
                self.assertRaises(RuntimeError):
            vmctl.screendump("/tmp/qmp.sock", "/tmp/frame.ppm")  # noqa: B108


class PpmToPngTest(unittest.TestCase):
    def test_round_trip(self):
        w, h = 3, 2
        pixels = bytes(range(w * h * 3))  # every pixel distinct
        header = b"P6\n# a comment\n3 2\n255\n"
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "in.ppm"
            png = Path(tmp) / "out.png"
            ppm.write_bytes(header + pixels)
            self.assertEqual(vmctl.ppm_to_png(ppm, png), (w, h))
            dw, dh, depth, ctype, rgb = _decode_png(png.read_bytes())
            self.assertEqual((dw, dh), (w, h))
            self.assertEqual(depth, 8)
            self.assertEqual(ctype, 2)
            self.assertEqual(rgb, pixels)

    def test_bad_input(self):
        pixels = b"\x01\x02\x03\x04\x05\x06"  # 2x1 image
        cases = {
            "P3 magic": b"P3\n2 1\n255\n" + pixels,
            "maxval 65535": b"P6\n2 1\n65535\n" + pixels,
            "truncated pixel data": b"P6\n2 1\n255\n" + pixels[:4],
            "empty file": b"",
        }
        for name, data in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                ppm = Path(tmp) / "in.ppm"
                png = Path(tmp) / "out.png"
                ppm.write_bytes(data)
                sentinel = b"original png bytes"
                png.write_bytes(sentinel)
                with self.assertRaises(ValueError):
                    vmctl.ppm_to_png(ppm, png)
                # A pre-existing output file keeps its original bytes...
                self.assertEqual(png.read_bytes(), sentinel)
                # ...and no temp file is left in the output directory.
                leftovers = [p.name for p in Path(tmp).iterdir()
                             if p.name not in ("in.ppm", "out.png")]
                self.assertEqual(leftovers, [])


class CmdScreenshotTest(unittest.TestCase):
    """cmd_screenshot with a mocked screendump — no real QMP sockets."""

    def _args(self, out: str):
        return SimpleNamespace(monitor="/tmp/qmp.sock",  # noqa: B108 - mocked, never connected
                               out=out)

    def test_success(self):
        w, h = 4, 2
        pixels = bytes((i * 7) % 256 for i in range(w * h * 3))
        ppm_bytes = b"P6\n4 2\n255\n" + pixels

        def fake_screendump(monitor_sock, output_path):
            Path(output_path).write_bytes(ppm_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "shot.png")
            stdout = io.StringIO()
            with mock.patch.object(vmctl, "screendump", side_effect=fake_screendump), \
                    mock.patch.object(vmctl.sys, "stdout", stdout):
                rc = vmctl.cmd_screenshot(self._args(out))
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.getvalue(), f"screenshot saved: {out}  ({w}x{h})\n")
            dw, dh, depth, ctype, _ = _decode_png(Path(out).read_bytes())
            self.assertEqual((dw, dh), (w, h))
            self.assertEqual((depth, ctype), (8, 2))

    def test_qmp_failure_leaves_out_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "shot.png")
            sentinel = b"original png bytes"
            Path(out).write_bytes(sentinel)
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(vmctl, "screendump",
                                   side_effect=ConnectionError("no VM")), \
                    mock.patch.object(vmctl.sys, "stdout", stdout), \
                    mock.patch.object(vmctl.sys, "stderr", stderr):
                rc = vmctl.cmd_screenshot(self._args(out))
            self.assertEqual(rc, 1)
            self.assertEqual(Path(out).read_bytes(), sentinel)
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue().startswith("[vmctl] WARNING"))

    def test_bad_ppm_leaves_out_file_unchanged(self):
        def fake_screendump(monitor_sock, output_path):
            Path(output_path).write_bytes(b"this is not a ppm at all")

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "shot.png")
            sentinel = b"original png bytes"
            Path(out).write_bytes(sentinel)
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(vmctl, "screendump", side_effect=fake_screendump), \
                    mock.patch.object(vmctl.sys, "stdout", stdout), \
                    mock.patch.object(vmctl.sys, "stderr", stderr):
                rc = vmctl.cmd_screenshot(self._args(out))
            self.assertEqual(rc, 1)
            self.assertEqual(Path(out).read_bytes(), sentinel)
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue().startswith("[vmctl] WARNING"))


class TestPatternTest(unittest.TestCase):
    """The NO SIGNAL card: pixels checked against the spec, not the generator."""

    @classmethod
    def setUpClass(cls):
        gen_path = Path(__file__).resolve().parent.parent / "tools" / "generate_testpattern.py"
        spec = importlib.util.spec_from_file_location("generate_testpattern", gen_path)
        assert spec is not None and spec.loader is not None
        cls.gen = importlib.util.module_from_spec(spec)
        sys.modules["generate_testpattern"] = cls.gen  # register before exec
        spec.loader.exec_module(cls.gen)

    def _px(self, rgb: bytes, w: int, x: int, y: int) -> tuple[int, int, int]:
        i = (y * w + x) * 3
        return (rgb[i], rgb[i + 1], rgb[i + 2])

    def _check_card_pixels(self, rgb: bytes, w: int, h: int) -> None:
        # Bars: 8 bars of 160 px across rows 0-239; odd rows dimmed to 85%.
        bar_colors = [
            (255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
            (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0),
        ]
        self.assertEqual(w // len(bar_colors), 160)
        for bi, (r, g, b) in enumerate(bar_colors):
            cx = bi * 160 + 80  # horizontal centre of the bar
            self.assertEqual(self._px(rgb, w, cx, 10), (r, g, b),
                             f"bar {bi} even row")
            dimmed = (int(r * 0.85), int(g * 0.85), int(b * 0.85))
            self.assertEqual(self._px(rgb, w, cx, 11), dimmed,
                             f"bar {bi} odd row (scan line)")
        # Background: well below the bars and outside the text.
        self.assertEqual(self._px(rgb, w, 40, 650), (0, 0, 0))
        # Text band (rows 440-519, cols 280-999): at least one fully white
        # pixel on an even row.
        has_white = any(
            self._px(rgb, w, x, y) == (255, 255, 255)
            for y in range(440, 520, 2)
            for x in range(280, 1000)
        )
        self.assertTrue(has_white, "no white text pixel in the text band")
        # No non-black pixels above or below the text band.
        for y in list(range(240, 440)) + list(range(520, 720)):
            row = rgb[y * w * 3:(y + 1) * w * 3]
            self.assertEqual(row, b"\x00" * (w * 3), f"row {y} is not black")

    def test_generated_card_matches_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "testpattern.png"
            self.gen.generate(out)
            w, h, depth, ctype, rgb = _decode_png(out.read_bytes())
            self.assertEqual((w, h), (1280, 720))
            self.assertEqual(depth, 8)
            self.assertEqual(ctype, 2)
            self._check_card_pixels(rgb, w, h)

    def test_committed_asset_matches_generator(self):
        # Pixels, not bytes: zlib builds can compress identical images
        # differently.
        asset = Path(__file__).resolve().parent.parent / "tools" / "testpattern.png"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "testpattern.png"
            self.gen.generate(out)
            aw, ah, _, _, a_rgb = _decode_png(asset.read_bytes())
            gw, gh, _, _, g_rgb = _decode_png(out.read_bytes())
            self.assertEqual((aw, ah), (gw, gh))
            self.assertEqual(a_rgb, g_rgb)


if __name__ == "__main__":
    unittest.main()
