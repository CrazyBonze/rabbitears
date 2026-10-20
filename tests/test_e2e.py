"""Unit tests for vm/e2e.py (the end-to-end VM pipeline check).

The real thing boots QEMU and deploys over SSH; these tests cover the pure
checks (schedule math, mount/mpv state, frame comparison) and the driver's
gating/reporting with the checks stubbed out. vm/e2e.py is a standalone
script, so it is loaded by file path.
"""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("rabbitears_vm_e2e", _REPO / "vm" / "e2e.py")
assert _spec is not None and _spec.loader is not None
e2e = importlib.util.module_from_spec(_spec)
sys.modules["rabbitears_vm_e2e"] = e2e
_spec.loader.exec_module(e2e)

MOUNT = "/media/rabbitears"
CH = f"{MOUNT}/channels/01-cartoons"
FILES = [["S01E01 - Opening.mp4", 100.0], ["S01E02 - Adventure.mp4", 50.0],
         ["S01E03 - Journey.mp4", 200.0]]  # loop = 350s


def probe(name="S01E02 - Adventure.mp4", time_pos=10.0, now=None, epoch=0.0, **extra):
    # Default: position 110s into the loop = file 2 at +10s.
    p = {
        "epoch": epoch,
        "mount_point": MOUNT,
        "testpattern": "/usr/share/rabbitears/testpattern.png",
        "mpv": {"path": f"{CH}/{name}", "time-pos": time_pos, "idle-active": False},
        "now": 350.0 * 1000 + 110.0 if now is None else now,
        "mounts": [["/dev/sda1", MOUNT, "exfat"]],
        "files": FILES,
    }
    p.update(extra)
    return p


class ScheduleErrorTest(unittest.TestCase):
    def test_exact_match(self):
        diff, detail = e2e.schedule_error(probe())
        self.assertAlmostEqual(diff, 0.0)
        self.assertIn("expected position=110.0s", detail)

    def test_small_lag_is_signed(self):
        diff, _ = e2e.schedule_error(probe(time_pos=8.0))
        self.assertAlmostEqual(diff, -2.0)

    def test_wrong_file_is_far_off(self):
        diff, _ = e2e.schedule_error(probe(name="S01E03 - Journey.mp4", time_pos=10.0))
        self.assertAlmostEqual(diff, 50.0)  # file 3 starts at 150 -> 160 vs 110

    def test_wraparound_at_loop_boundary(self):
        # Clock says 1s into the loop; mpv is 1s before the end of the last file.
        diff, _ = e2e.schedule_error(probe(name="S01E03 - Journey.mp4", time_pos=199.0,
                                           now=350.0 * 7 + 1.0))
        self.assertAlmostEqual(diff, -2.0)

    def test_epoch_is_honoured(self):
        diff, _ = e2e.schedule_error(probe(epoch=1000.0, now=1000.0 + 110.0))
        self.assertAlmostEqual(diff, 0.0)

    def test_missing_data_raises(self):
        for bad in (probe(files=[]), probe(mpv={"path": None}),
                    probe(mpv={"path": f"{CH}/x.mp4", "time-pos": None}),
                    probe(name="not-indexed.mp4")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                e2e.schedule_error(bad)


class StateTest(unittest.TestCase):
    def test_stick_mount(self):
        self.assertEqual(e2e.stick_mount(probe()), ("/dev/sda1", "exfat"))
        self.assertIsNone(e2e.stick_mount(probe(mounts=[])))

    def test_playing_vs_no_signal(self):
        self.assertTrue(e2e.is_playing_media(probe()))
        self.assertFalse(e2e.is_no_signal(probe()))
        ns = probe(mpv={"path": "/usr/share/rabbitears/testpattern.png"})
        self.assertFalse(e2e.is_playing_media(ns))
        self.assertTrue(e2e.is_no_signal(ns))
        self.assertFalse(e2e.is_playing_media(probe(mpv={})))


class FrameTest(unittest.TestCase):
    def test_identical_frames(self):
        a = bytes(range(256)) * 300
        self.assertEqual(e2e.frame_difference(a, a), 0.0)

    def test_different_frames(self):
        a = bytes(30000)
        b = bytes([255]) * 30000
        self.assertEqual(e2e.frame_difference(a, b), 1.0)

    def test_size_mismatch_counts_as_different(self):
        self.assertEqual(e2e.frame_difference(b"\x00" * 9, b"\x00" * 12), 1.0)

    def test_blank(self):
        self.assertTrue(e2e.is_blank(bytes(30000)))
        self.assertFalse(e2e.is_blank(bytes([200]) * 30000))


class TargetTest(unittest.TestCase):
    def test_probe_runs_the_script_over_ssh_and_parses_json(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, stdout='noise\n{"now": 1.5}\n', stderr=""))
        result = e2e.Target(run=run).probe()
        self.assertEqual(result, {"now": 1.5})
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("root@127.0.0.1", argv)
        self.assertEqual(argv[-1], "python3 -B -")
        self.assertEqual(run.call_args.kwargs["input"], e2e.PROBE_SCRIPT)

    def test_probe_failure_raises(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "refused"))
        with self.assertRaises(e2e.CheckFailed):
            e2e.Target(run=run).probe()

    def test_probe_script_never_writes_bytecode(self):
        # verify.sh flags unmanaged files under /opt/rabbitears.
        self.assertIn("dont_write_bytecode = True", e2e.PROBE_SCRIPT)

    def test_probe_script_compiles(self):
        compile(e2e.PROBE_SCRIPT, "probe", "exec")


class WaitForTest(unittest.TestCase):
    def test_returns_first_truthy(self):
        t = [0.0]
        values = iter([0, 0, "yes"])
        out = e2e.wait_for(lambda: next(values), 10, clock=lambda: t[0],
                           sleep=lambda d: t.__setitem__(0, t[0] + d))
        self.assertEqual(out, "yes")

    def test_gives_up(self):
        t = [0.0]
        out = e2e.wait_for(lambda: None, 5, clock=lambda: t[0],
                           sleep=lambda d: t.__setitem__(0, t[0] + d))
        self.assertIsNone(out)


class DriverTest(unittest.TestCase):
    """E2E.run(): ordering, gating, cleanup, report — checks stubbed out."""

    STEPS = ["preflight", "boot", "ssh", "deploy", "service", "mount", "playing",
             "schedule", "motion", "remote-power-off", "remote-power-on", "remote-zap",
             "remote-autofire", "remote-volume", "unplug", "replug",
             "crash", "verify"]

    def _run(self, fail=(), **argv_overrides):
        with tempfile.TemporaryDirectory() as tmp:
            args = e2e.build_parser().parse_args(["--run-dir", str(Path(tmp) / "run")])
            for k, v in argv_overrides.items():
                setattr(args, k, v)
            runner = e2e.E2E(args, target=mock.Mock())
            called = []

            def stub(name):
                def fn():
                    called.append(name)
                    if name in fail:
                        raise e2e.CheckFailed(f"{name} broke")
                    return f"{name} ok"
                return fn

            for name in self.STEPS + ["rebuild"]:
                setattr(runner, f"c_{name.replace('-', '_')}", stub(name))
            runner.shutdown = mock.Mock()
            with redirect_stdout(io.StringIO()) as out:
                rc = runner.run()
            report = json.loads((Path(tmp) / "run" / "report.json").read_text())
        return rc, called, report, runner, out.getvalue()

    def test_all_pass(self):
        rc, called, report, runner, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(called, self.STEPS)
        self.assertTrue(report["ok"])
        runner.shutdown.assert_called_once()
        self.assertIn("ALL CHECKS PASSED", out)

    def test_gating_failure_skips_the_rest_but_still_shuts_down(self):
        rc, called, report, runner, _ = self._run(fail={"ssh"})
        self.assertEqual(rc, 1)
        self.assertEqual(called, ["preflight", "boot", "ssh"])
        skipped = [r["name"] for r in report["results"] if r["detail"].startswith("skipped")]
        self.assertEqual(skipped, self.STEPS[3:])
        runner.shutdown.assert_called_once()

    def test_observation_failure_does_not_skip_later_checks(self):
        rc, called, report, _, _ = self._run(fail={"motion"})
        self.assertEqual(rc, 1)
        self.assertEqual(called, self.STEPS)
        self.assertFalse(report["ok"])

    def test_rebuild_and_skip_crash_flags(self):
        rc, called, _, _, _ = self._run(rebuild=True, skip_crash=True)
        self.assertEqual(rc, 0)
        self.assertEqual(called[:2], ["preflight", "rebuild"])
        self.assertNotIn("crash", called)

    def test_unexpected_exception_is_a_failed_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = e2e.build_parser().parse_args(["--run-dir", tmp])
            runner = e2e.E2E(args, target=mock.Mock())
            with redirect_stdout(io.StringIO()):
                ok = runner.check("x", lambda: 1 / 0)
        self.assertFalse(ok)
        self.assertIn("ZeroDivisionError", runner.report.results[0].detail)


if __name__ == "__main__":
    unittest.main()
