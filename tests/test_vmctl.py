"""Unit tests for vm/vmctl.py (virtio-gpu validation VM control).

subprocess.Popen and the serial socket are mocked everywhere — no real QEMU
or network in tests.

vm/vmctl.py is a standalone script (not a package), so it is loaded by file
path rather than imported by name.
"""

import functools
import importlib.util
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_VMCTL_PATH = Path(__file__).resolve().parent.parent / "vm" / "vmctl.py"
_spec = importlib.util.spec_from_file_location("vmctl", _VMCTL_PATH)
assert _spec is not None and _spec.loader is not None
vmctl = importlib.util.module_from_spec(_spec)
sys.modules["vmctl"] = vmctl  # register before exec so @dataclass can resolve it
_spec.loader.exec_module(vmctl)


class VmRunTest(unittest.TestCase):
    def test_run_spawns_qemu_with_exact_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "disk.qcow2"
            expected = [
                "qemu-system-x86_64",
                "-m", "2G",
                "-smp", "2",
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


class ScreenshotTest(unittest.TestCase):
    def test_run_screenshot_spawns_thread(self):
        # The required case: mock the VNC connection, assert the thread was
        # started (ran to completion) and the PNG path is correct.
        frame = (10, 8, b"\x00" * (10 * 8 * 3))
        with mock.patch.object(vmctl, "_grab_vnc_frame", return_value=frame) as grab, \
                mock.patch.object(vmctl, "_save_png") as save_png:
            t = vmctl.start_screenshot_thread("127.0.0.1", 5900, "vm/screenshot.png", 0.0)
            t.join(timeout=5)
        self.assertFalse(t.is_alive())  # thread started and finished
        grab.assert_called_once_with("127.0.0.1", 5900)
        save_png.assert_called_once_with("vm/screenshot.png", 10, 8, b"\x00" * (10 * 8 * 3))

    def test_screenshot_worker_saves_png(self):
        frame = (8, 6, b"\xab" * (8 * 6 * 3))
        with mock.patch.object(vmctl, "_grab_vnc_frame", return_value=frame), \
                mock.patch.object(vmctl, "_save_png") as save_png:
            vmctl._screenshot_worker("127.0.0.1", 5900, "out.png", 0.0)
        save_png.assert_called_once_with("out.png", 8, 6, b"\xab" * (8 * 6 * 3))

    def test_screenshot_worker_warns_on_vnc_failure(self):
        # Best-effort: a failed VNC grab logs a warning and never raises.
        with mock.patch.object(vmctl, "_grab_vnc_frame", return_value=None), \
                mock.patch.object(vmctl, "_save_png") as save_png, \
                mock.patch.object(vmctl.sys, "stderr") as stderr:
            vmctl._screenshot_worker("127.0.0.1", 5900, "out.png", 0.0)
        save_png.assert_not_called()
        stderr.write.assert_called()

    def test_cmd_run_starts_screenshot_when_flagged(self):
        args = SimpleNamespace(
            disk="x.qcow2", host="127.0.0.1", port=4321,
            screenshot=True, screenshot_out="vm/screenshot.png",
            screenshot_delay=0.0, vnc_host="127.0.0.1", vnc_port=5900,
            monitor=None, display="none",
        )
        with mock.patch.object(vmctl, "Vm") as vm_cls, \
                mock.patch.object(vmctl.signal, "signal"), \
                mock.patch.object(vmctl, "_stream_serial"), \
                mock.patch.object(vmctl, "start_screenshot_thread") as start_shot:
            vm_cls.return_value.wait.return_value = 0
            vmctl.cmd_run(args)
        start_shot.assert_called_once_with("127.0.0.1", 5900, "vm/screenshot.png", 0.0)

    def test_cmd_run_no_screenshot_when_unflagged(self):
        args = SimpleNamespace(
            disk="x.qcow2", host="127.0.0.1", port=4321,
            screenshot=False, screenshot_out="vm/screenshot.png",
            screenshot_delay=0.0, vnc_host="127.0.0.1", vnc_port=5900,
            monitor=None, display="none",
        )
        with mock.patch.object(vmctl, "Vm") as vm_cls, \
                mock.patch.object(vmctl.signal, "signal"), \
                mock.patch.object(vmctl, "_stream_serial"), \
                mock.patch.object(vmctl, "start_screenshot_thread") as start_shot:
            vm_cls.return_value.wait.return_value = 0
            vmctl.cmd_run(args)
        start_shot.assert_not_called()


class QmpScreendumpTest(unittest.TestCase):
    def test_qemu_args_includes_qmp_socket(self):
        args = vmctl.qemu_args(Path("disk.qcow2"), monitor_sock=Path("/tmp/qmp.sock"))
        self.assertIn("-qmp", args)
        self.assertIn("unix:/tmp/qmp.sock,server,nowait", args)

    def test_qemu_args_no_monitor_by_default(self):
        args = vmctl.qemu_args(Path("disk.qcow2"))
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


if __name__ == "__main__":
    unittest.main()
