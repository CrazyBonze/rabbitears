#!/usr/bin/env python3
"""Rabbit Ears — end-to-end check of the VM pipeline (host side).

    uv run task vm-e2e [--rebuild] [--usb IMG] [--keep-running] [--skip-crash]

Proves that the pipeline the box depends on works, from the repo, with no
manual step inside the VM:

   1. preflight     the disk and mock stick exist; no QEMU holds the disk
  (2. rebuild       --rebuild only: provision.sh destroy + create, i.e. vm-rebuild)
   3. boot          vmctl.py run --usb IMG (serial console -> run dir)
   4. ssh           the VM answers on 127.0.0.1:2222 with the clone's key
   5. deploy        alpine/deploy.sh --vm --reboot, ending in "verify: OK"
   6. service       rc-service rabbitears status -> started
   7. mount         /media/rabbitears is the exFAT stick, mounted by mdev
   8. playing       mpv plays a file from the stick
   9. schedule      mpv's file + time-pos match (now - epoch) mod loop (±3s)
  10. motion        two screenshots 3s apart differ (real video on screen)
  11. osd-flash     zap -> green CH NN top-right, gone after osd_duration
  12. osd-overlay   ok -> overlay on (ctl status + screenshot), back -> off
  13. osd-zap       zap with the overlay up -> refreshed, not dismissed
  14. osd-standby   power with the overlay up -> dismissed, wake doesn't restore
  15. osd-remaining ctl status 'remaining' matches mpv's time-pos (±3s)
  16. unplug        usb unplug -> lazy unmount, mpv shows the NO SIGNAL card
  17. replug        usb plug -> remount, playback at the schedule position
  18. crash         pkill -9 mpv -> supervise-daemon respawns, playback returns
  19. verify        alpine/verify.sh --vm still reports OK (tests drifted nothing)
  20. shutdown      the VM stops and no QEMU is left holding the disk

Every check prints PASS/FAIL with evidence; a JSON report, serial log, deploy
log and screenshots go to the run directory (default /tmp/rabbitears-e2e).
Exits 0 only if every check passed. The VM is always shut down at the end
(unless --keep-running), even when a check fails.

SSH into the VM is used only for read-only inspection and the deliberate test
actions above (killing mpv). The only thing that changes the target is
alpine/deploy.sh, as everywhere else.

Host side: stdlib only, run through uv (Python 3.14).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
VMCTL_PATH = REPO_ROOT / "vm" / "vmctl.py"
SSH_KEY = REPO_ROOT / "vm" / "keys" / "rabbitears-vm"
DEFAULT_USB = "/var/lib/rabbitears-vm/usb.img"
DEFAULT_RUN_DIR = "/tmp/rabbitears-e2e"  # noqa: S108 - host scratch dir, recreated per run
SSH_PORT = 2222
SCHEDULE_TOLERANCE = 3.0  # seconds; SCOPE §4.2 position vs mpv's time-pos


def _load_vmctl():
    spec = importlib.util.spec_from_file_location("vmctl", VMCTL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("vmctl", mod)
    spec.loader.exec_module(mod)
    return sys.modules["vmctl"]


vmctl = sys.modules.get("vmctl") or _load_vmctl()


# --- What runs inside the VM (read-only) ------------------------------------

# Run with the VM's system python3 (Alpine 3.14, stdlib only). Reads the app's
# own config (so paths/epoch match what the app uses), asks mpv for its state,
# and returns everything the host needs to check the schedule as one JSON line.
# -B / dont_write_bytecode: never leave __pycache__ in /opt (verify.sh flags it).
PROBE_SCRIPT = r'''
import json, os, socket, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, "/opt")
out = {}
try:
    from rabbitears.config import load_config
    cfg = load_config()
    epoch, mount = cfg.epoch, str(cfg.mount_point)
    sock_path, tp = str(cfg.socket_path), str(cfg.testpattern_path)
except Exception as e:
    out["error_config"] = repr(e)
    epoch, mount = 0.0, "/media/rabbitears"
    sock_path, tp = "/run/rabbitears/mpv.sock", "/usr/share/rabbitears/testpattern.png"
out.update(epoch=epoch, mount_point=mount, testpattern=tp)

def mpv(names):
    res = {}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(sock_path)
    f = s.makefile("rwb")
    for i, n in enumerate(names, 1):
        f.write((json.dumps({"command": ["get_property", n], "request_id": i}) + "\n").encode())
    f.flush()
    pending = set(range(1, len(names) + 1))
    while pending:
        line = f.readline()
        if not line:
            break
        msg = json.loads(line)
        rid = msg.get("request_id")
        if rid in pending:
            pending.discard(rid)
            res[names[rid - 1]] = msg.get("data") if msg.get("error") == "success" else None
    s.close()
    return res

try:
    out["mpv"] = mpv(["path", "time-pos", "idle-active", "pause", "volume", "vid", "mute"])
except Exception as e:
    out["error_mpv"] = repr(e)
out["now"] = time.time()
out["mounts"] = [l.split()[:3] for l in open("/proc/mounts").read().splitlines()
                 if len(l.split()) > 2 and l.split()[1] == mount]
path = (out.get("mpv") or {}).get("path")
if isinstance(path, str) and path.startswith(mount + "/"):
    ch = os.path.dirname(path)
    try:
        idx = json.load(open(os.path.join(mount, ".rabbitears", "index.json")))
    except Exception as e:
        idx = {}
        out["error_index"] = repr(e)
    names = sorted(n for n in os.listdir(ch) if n.endswith(".mp4") and not n.startswith("."))
    out["files"] = [[n, float(idx[n])] for n in names
                    if isinstance(idx.get(n), (int, float)) and idx[n] > 0]
print(json.dumps(out))
'''


# --- Pure checks (unit-tested) ------------------------------------------------

def stick_mount(probe: dict) -> tuple[str, str] | None:
    """(device, fstype) of the drive on the mount point, or None."""
    for entry in probe.get("mounts") or []:
        if len(entry) >= 3:
            return entry[0], entry[2]
    return None


def mpv_path(probe: dict) -> str | None:
    path = (probe.get("mpv") or {}).get("path")
    return path if isinstance(path, str) else None


def is_playing_media(probe: dict) -> bool:
    path = mpv_path(probe)
    mount = probe.get("mount_point", "/media/rabbitears")
    return bool(path) and path.startswith(f"{mount}/channels/")


def is_no_signal(probe: dict) -> bool:
    return mpv_path(probe) == probe.get("testpattern")


def is_standby(probe: dict) -> bool:
    """True if the app is in standby: video hidden and muted.

    mpv 0.41 reports `vid` as `false`/`0` while a file is playing with the
    video track deselected (`vid=no`), and as the string `"no"` when idle.
    (The old `== "no"`-only check passed only when the file hit EOF during
    the poll window — a race.)
    """
    mpv = probe.get("mpv") or {}
    return mpv.get("vid") in ("no", 0) and bool(mpv.get("mute"))


def mpv_volume(probe: dict) -> float | None:
    v = (probe.get("mpv") or {}).get("volume")
    return float(v) if isinstance(v, (int, float)) else None


def schedule_error(probe: dict) -> tuple[float, str]:
    """Signed distance (s) between mpv's loop position and the clock's.

    Expected (SCOPE §4.2): position = (now - epoch) mod loop, where loop is
    the sum of the channel's indexed durations in filename order. Actual:
    the start of mpv's current file within the loop + mpv's time-pos.
    Computed here independently of the app's code. Raises ValueError if the
    probe lacks the data.
    """
    files = probe.get("files") or []
    path = mpv_path(probe)
    time_pos = (probe.get("mpv") or {}).get("time-pos")
    if not files or not path or not isinstance(time_pos, (int, float)):
        raise ValueError(f"not enough data for the schedule check: path={path!r}, "
                         f"time-pos={time_pos!r}, {len(files)} indexed files")
    loop = sum(d for _, d in files)
    if loop <= 0:
        raise ValueError("loop length is 0")
    starts: dict[str, float] = {}
    acc = 0.0
    for name, dur in files:
        starts[name] = acc
        acc += dur
    name = os.path.basename(path)
    if name not in starts:
        raise ValueError(f"mpv plays {name!r}, which is not in the channel's index")
    expected = (probe["now"] - probe.get("epoch", 0.0)) % loop
    actual = starts[name] + float(time_pos)
    diff = (actual - expected + loop / 2) % loop - loop / 2  # signed, circular
    detail = (f"epoch={probe.get('epoch', 0.0):.0f} now={probe['now']:.1f} loop={loop:.1f}s "
              f"expected position={expected:.1f}s; mpv: {name} +{float(time_pos):.1f}s "
              f"= position {actual:.1f}s; diff {diff:+.1f}s")
    return diff, detail


def frame_difference(a: bytes, b: bytes, stride: int = 7, threshold: int = 24) -> float:
    """Fraction of sampled bytes that differ by more than ``threshold``."""
    if len(a) != len(b) or not a:
        return 1.0
    sa, sb = a[::stride], b[::stride]
    changed = sum(1 for x, y in zip(sa, sb, strict=True) if abs(x - y) > threshold)
    return changed / len(sa)


def is_blank(rgb: bytes, stride: int = 7, threshold: int = 16) -> bool:
    """True if (almost) every sampled byte is near black."""
    sample = rgb[::stride]
    return sum(1 for v in sample if v > threshold) < len(sample) * 0.002


def flash_green_count(rgb: bytes, w: int, h: int, tol: int = 15, stride: int = 2) -> int:
    """Count pixels near the OSD green #33CC33 in the top-right region.

    The channel flash is exactly #33CC33 (51, 204, 51). The mock video's
    colour bars sweep through green shades (including (51, 204, 51) itself),
    so a tight tolerance keeps the count on the text core, and the check
    compares against a baseline screenshot taken just before the zap.
    """
    n = 0
    for y in range(0, int(h * 0.35), stride):
        for x in range(int(w * 0.6), w, stride):
            i = (y * w + x) * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if abs(r - 51) < tol and abs(g - 204) < tol and abs(b - 51) < tol:
                n += 1
    return n


# --- Plumbing -------------------------------------------------------------------

@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    seconds: float = 0.0


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)


class CheckFailed(Exception):
    pass


def ssh_argv(*remote: str) -> list[str]:
    return [
        "ssh", "-i", str(SSH_KEY), "-p", str(SSH_PORT),
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "root@127.0.0.1", *remote,
    ]


class Target:
    """Read-only access to the VM over SSH (plus deliberate test actions)."""

    def __init__(self, run=subprocess.run) -> None:
        self._run = run

    def ssh(self, command: str, *, input: str | None = None,
            timeout: float = 30.0) -> subprocess.CompletedProcess:
        return self._run(ssh_argv(command), input=input, capture_output=True, text=True,
                         timeout=timeout)

    def up(self) -> bool:
        try:
            return self.ssh("true", timeout=15).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def probe(self) -> dict:
        r = self.ssh("python3 -B -", input=PROBE_SCRIPT, timeout=30)
        if r.returncode != 0:
            raise CheckFailed(f"probe failed (rc {r.returncode}): {r.stderr.strip()[-300:]}")
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            raise CheckFailed(f"probe returned bad JSON: {r.stdout[-300:]!r}") from e


def wait_for(predicate: Callable[[], object], timeout: float, interval: float = 1.0,
             clock=time.monotonic, sleep=time.sleep):
    """Poll ``predicate`` until it returns a truthy value; return it (or the last falsy)."""
    deadline = clock() + timeout
    value = predicate()
    while not value and clock() < deadline:
        sleep(interval)
        value = predicate()
    return value


# --- The run ----------------------------------------------------------------------

class E2E:
    def __init__(self, args: argparse.Namespace, target: Target | None = None) -> None:
        self.args = args
        self.target = target or Target()
        self.report = Report()
        self.run_dir = Path(args.run_dir)
        self.vm_proc: subprocess.Popen | None = None
        self._serial_log = None
        self.disk = Path(args.disk)

    # -- reporting --

    def check(self, name: str, fn: Callable[[], str]) -> bool:
        print(f"[e2e] --- {name} ---", flush=True)
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except CheckFailed as e:
            detail, ok = str(e), False
        except Exception as e:  # noqa: BLE001 - a broken check is a failed check
            detail, ok = f"{type(e).__name__}: {e}", False
        res = Result(name, ok, detail, round(time.monotonic() - t0, 1))
        self.report.results.append(res)
        print(f"[e2e] {'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)
        return ok

    def skip_rest(self, names: list[str], why: str) -> None:
        for n in names:
            self.report.results.append(Result(n, False, f"skipped: {why}"))
            print(f"[e2e] SKIP {n}: {why}", flush=True)

    # -- helpers --

    def _host(self, argv: list[str], log_name: str | None = None,
              timeout: float = 1800) -> subprocess.CompletedProcess:
        """Run a host command, streaming its output (and saving it to a log)."""
        print(f"[e2e] $ {' '.join(argv)}", flush=True)
        proc = subprocess.Popen(argv, cwd=REPO_ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        lines: list[str] = []
        assert proc.stdout is not None
        deadline = time.monotonic() + timeout
        for line in proc.stdout:
            sys.stdout.write(f"    {line}")
            lines.append(line)
            if time.monotonic() > deadline:
                proc.kill()
                break
        rc = proc.wait()
        out = "".join(lines)
        if log_name:
            (self.run_dir / log_name).write_text(out)
        return subprocess.CompletedProcess(argv, rc, out, "")

    def _vmctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(VMCTL_PATH), *args], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=60)

    def _remote(self, *args: str) -> subprocess.CompletedProcess:
        """Run the remote test double on the VM (writes a button line to the FIFO)."""
        return self.target.ssh(
            f"python3 /usr/share/rabbitears/e2e-remote.py {' '.join(args)}", timeout=20
        )

    def _screenshot(self, name: str) -> tuple[int, int, bytes]:
        ppm = self.run_dir / f"{name}.ppm"
        vmctl.screendump(self.args.monitor, ppm)
        w, h, rgb = vmctl._read_ppm(ppm)
        vmctl.ppm_to_png(ppm, self.run_dir / f"{name}.png")
        ppm.unlink()
        return w, h, rgb

    def _probe_until(self, pred: Callable[[dict], bool], timeout: float) -> dict:
        last: dict = {}

        def attempt():
            nonlocal last
            try:
                last = self.target.probe()
            except CheckFailed as e:
                last = {"error": str(e)}
                return False
            return pred(last)

        wait_for(attempt, timeout, interval=1.0)
        return last

    def _log_tail(self, lines: int = 15) -> str:
        r = self.target.ssh(f"tail -n {lines} /var/log/rabbitears.log")
        return r.stdout

    def _ctl_status(self) -> dict:
        """The app's read-only status over the control socket (via SSH).

        -B: never write __pycache__ into /opt/rabbitears (verify.sh flags
        unmanaged files there; the service itself runs with
        PYTHONDONTWRITEBYTECODE=1).
        """
        r = self.target.ssh("PYTHONPATH=/opt python3 -B -m rabbitears.ctl status")
        if r.returncode != 0:
            raise CheckFailed(f"ctl status failed: {r.stderr.strip()}")
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            raise CheckFailed(f"ctl status returned bad JSON: {r.stdout[-200:]!r}") from e

    # -- checks --

    def c_preflight(self) -> str:
        usb = Path(self.args.usb)
        if not usb.exists():
            raise CheckFailed(f"mock stick {usb} missing: run `uv run task usb-build`")
        if not os.access(usb, os.R_OK | os.W_OK):
            raise CheckFailed(f"{usb} not readable/writable: are you in the 'kvm' group "
                              "(new terminal, or `sg kvm -c ...`)?")
        if not SSH_KEY.exists():
            raise CheckFailed(f"{SSH_KEY} missing: run `uv run task vm-build`")
        if not self.args.rebuild:
            if not self.disk.exists():
                raise CheckFailed(f"VM disk {self.disk} missing: run `uv run task vm-build` "
                                  "(or pass --rebuild)")
            holders = vmctl.find_disk_holders(self.disk)
            if holders:
                raise CheckFailed("the VM disk is in use: "
                                  + "; ".join(h.describe() for h in holders)
                                  + " — run `uv run task vm-stop`")
        return f"stick {usb}, disk {self.disk}"

    def c_rebuild(self) -> str:
        for argv in (["./vm/provision.sh", "destroy"], ["./vm/provision.sh"]):
            r = self._host(argv, log_name="rebuild.log" if len(argv) == 1 else None)
            if r.returncode != 0:
                raise CheckFailed(f"{' '.join(argv)} exited {r.returncode} "
                                  "(install transcript: /var/lib/rabbitears-vm/install-serial.log)")
        holders = vmctl.find_disk_holders(self.disk)
        if holders:
            raise CheckFailed("after the rebuild the disk is still in use: "
                              + "; ".join(h.describe() for h in holders))
        return "VM rebuilt from the pinned ISO; disk at rest"

    def c_boot(self) -> str:
        self._serial_log = open(self.run_dir / "serial.log", "wb")  # noqa: SIM115
        argv = [sys.executable, str(VMCTL_PATH), "run", "--usb", str(self.args.usb),
                "--monitor", self.args.monitor]
        self.vm_proc = subprocess.Popen(argv, cwd=REPO_ROOT, stdout=self._serial_log,
                                        stderr=subprocess.STDOUT)
        time.sleep(3)
        if self.vm_proc.poll() is not None:
            self._serial_log.flush()
            tail = (self.run_dir / "serial.log").read_text(errors="replace")[-500:]
            raise CheckFailed(f"vmctl run exited {self.vm_proc.returncode}: {tail}")
        return f"vmctl run pid {self.vm_proc.pid}; serial -> {self.run_dir / 'serial.log'}"

    def c_ssh(self) -> str:
        t0 = time.monotonic()
        if not wait_for(self.target.up, 240, interval=3):
            raise CheckFailed("no SSH on 127.0.0.1:2222 within 240s (see serial.log)")
        return f"SSH up after {time.monotonic() - t0:.0f}s"

    def c_deploy(self) -> str:
        r = self._host(["alpine/deploy.sh", "--vm", "--reboot"], log_name="deploy.log",
                       timeout=1200)
        if r.returncode != 0:
            raise CheckFailed(f"deploy.sh exited {r.returncode} (see deploy.log)")
        if "verify: OK" not in r.stdout:
            raise CheckFailed("deploy.sh did not end with 'verify: OK' (see deploy.log)")
        changed = [ln.strip() for ln in r.stdout.splitlines() if "changed" in ln.lower()]
        return "deploy + reboot + verify: OK" + (f" ({changed[-1]})" if changed else "")

    def c_service(self) -> str:
        r = self.target.ssh("rc-service rabbitears status")
        if "started" not in r.stdout + r.stderr:
            raise CheckFailed(f"rabbitears not started: {(r.stdout + r.stderr).strip()}")
        return r.stdout.strip()

    def c_mount(self) -> str:
        probe = self._probe_until(lambda p: stick_mount(p) is not None, 30)
        m = stick_mount(probe)
        if m is None:
            raise CheckFailed(f"nothing mounted on {probe.get('mount_point')}: {probe}")
        dev, fstype = m
        if fstype != "exfat" or not dev.startswith("/dev/sd"):
            raise CheckFailed(f"unexpected mount {dev} ({fstype}); want a /dev/sd* exFAT stick")
        log = self.target.ssh("grep rabbitears-usb /var/log/messages | tail -n 3").stdout.strip()
        return f"{dev} on {probe['mount_point']} ({fstype}); syslog: {log or '(none)'}"

    def c_playing(self) -> str:
        probe = self._probe_until(is_playing_media, 120)
        if not is_playing_media(probe):
            raise CheckFailed(f"mpv is not playing from the stick: path={mpv_path(probe)!r}; "
                              f"log:\n{self._log_tail()}")
        return f"mpv plays {mpv_path(probe)} at +{probe['mpv'].get('time-pos') or 0:.1f}s"

    def c_schedule(self) -> str:
        # The first seconds after a (re)load can report time-pos 0 before the
        # seek lands, so take the best of a few samples.
        best: tuple[float, str] | None = None
        for _ in range(5):
            probe = self.target.probe()
            try:
                diff, detail = schedule_error(probe)
            except ValueError as e:
                detail, diff = str(e), float("inf")
            if best is None or abs(diff) < abs(best[0]):
                best = (diff, detail)
            if abs(diff) <= SCHEDULE_TOLERANCE:
                return detail
            time.sleep(2)
        assert best is not None
        raise CheckFailed(f"schedule off by more than {SCHEDULE_TOLERANCE}s: {best[1]}")

    def c_motion(self) -> str:
        w, h, a = self._screenshot("video-1")
        time.sleep(3)
        w2, h2, b = self._screenshot("video-2")
        if (w, h) != (w2, h2):
            raise CheckFailed(f"screen size changed between frames: {w}x{h} -> {w2}x{h2}")
        if is_blank(a) and is_blank(b):
            raise CheckFailed("both frames are black")
        diff = frame_difference(a, b)
        if diff < 0.005:
            raise CheckFailed(f"frames 3s apart are identical ({diff:.2%} changed): not video")
        return f"{w}x{h}, {diff:.1%} of sampled bytes changed in 3s (video-1/2.png)"

    # -- remote-control checks (drive the input path via the test double) ----

    def c_remote_power_off(self) -> str:
        r = self._remote("power")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote power failed: {r.stderr.strip()}")
        probe = self._probe_until(is_standby, 10)
        if not is_standby(probe):
            raise CheckFailed(f"not in standby after power-off: {probe.get('mpv')!r}")
        return "power-off -> standby (video hidden + muted)"

    def c_remote_power_on(self) -> str:
        r = self._remote("power")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote power failed: {r.stderr.strip()}")
        probe = self._probe_until(
            lambda p: is_playing_media(p) and not is_standby(p), 30
        )
        if not is_playing_media(probe) or is_standby(probe):
            raise CheckFailed(f"not playing after power-on: path={mpv_path(probe)!r}; "
                              f"log:\n{self._log_tail()}")
        return f"power-on -> playing {mpv_path(probe)}"

    def c_remote_zap(self) -> str:
        r = self._remote("channel-up")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote channel-up failed: {r.stderr.strip()}")
        # With one channel, "up" wraps to the same channel; the check is that it
        # re-tuned (the position is recomputed from the clock) and is playing.
        probe = self._probe_until(is_playing_media, 30)
        if not is_playing_media(probe):
            raise CheckFailed(f"not playing after zap: path={mpv_path(probe)!r}; "
                              f"log:\n{self._log_tail()}")
        return f"zap -> playing {mpv_path(probe)}"

    def c_remote_autofire(self) -> str:
        r = self._remote("hold", "channel-up", "1500")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote hold failed: {r.stderr.strip()}")
        probe = self._probe_until(is_playing_media, 30)
        if not is_playing_media(probe):
            raise CheckFailed(f"not playing after autofire: path={mpv_path(probe)!r}")
        zaps = self.target.ssh("grep -c 'remote: ' /var/log/rabbitears.log").stdout.strip()
        return f"autofire (hold 1500ms) -> playing {mpv_path(probe)} ({zaps} remote log lines)"

    def c_remote_volume(self) -> str:
        # The volume persists on the stick's state.json across runs, so a prior
        # run can leave it at the cap (80), where vol-up can't increase it. Step
        # it down first (and wait for the drop) so the increase below is real.
        self._remote("vol-down")
        probe = self._probe_until(lambda p: (mpv_volume(p) or 0) < 80, 10)
        before = mpv_volume(probe)
        r = self._remote("vol-up")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote vol-up failed: {r.stderr.strip()}")
        probe = self._probe_until(lambda p: (mpv_volume(p) or 0) > (before or 0), 10)
        after = mpv_volume(probe)
        if before is None or after is None or after <= before:
            raise CheckFailed(f"volume did not increase: before={before} after={after}")
        return f"vol-up: {before} -> {after}"

    # -- on-screen display checks (M4: flash + info overlay) ---------------

    def _overlay_ensure(self, on: bool) -> None:
        """Drive the overlay to a known state (ok toggles it)."""
        if bool(self._ctl_status().get("overlay")) != on:
            self._remote("ok")

    def c_osd_flash(self) -> str:
        # Clean slate: a zap in an earlier check can leave its flash on screen
        # (osd_duration is 2.5s and the checks run back-to-back).
        time.sleep(3.0)
        # Baseline first: the mock video's colours cycle, so the top-right can
        # contain #33CC33-ish green on its own. The flash is detected as the
        # increase over this baseline.
        w, h, base = self._screenshot("osd-flash-base")
        base_n = flash_green_count(base, w, h)
        r = self._remote("channel-up")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote channel-up failed: {r.stderr.strip()}")
        # The flash is up for osd_duration (2.5s) from the tune. In the VM the
        # tune can land up to ~1.5s after the button: the app's step loop is
        # single-threaded, and a state.json fsync over the emulated exFAT USB
        # can block it for a second (real 3040 USB is fast). So take up to
        # five screenshots over 3.2s and keep the greenest.
        best = 0
        for i in range(5):
            w, h, rgb = self._screenshot(f"osd-flash-on-{i}")
            best = max(best, flash_green_count(rgb, w, h) - base_n)
            if best > 1000:
                break
            time.sleep(0.8)
        if best <= 1000:
            raise CheckFailed(f"no green flash in the top-right after a zap "
                              f"(best {best} px above baseline {base_n})")
        time.sleep(3.0)  # longer than osd_duration (2.5s)
        w2, h2, rgb2 = self._screenshot("osd-flash-off")
        off = flash_green_count(rgb2, w2, h2) - base_n
        if off > 500:
            raise CheckFailed(f"flash still visible 3s after the zap "
                              f"({off} px above baseline vs {best} on)")
        return f"flash on: {best} px near #33CC33 above baseline; off after 3s: " \
               f"{off} px (osd-flash-on-*.png, osd-flash-off.png)"

    def c_osd_overlay(self) -> str:
        r = self._remote("ok")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote ok failed: {r.stderr.strip()}")
        st = self._ctl_status()
        if not st.get("overlay"):
            raise CheckFailed(f"overlay not shown after ok: {st}")
        w, h, _ = self._screenshot("osd-overlay-on")
        r = self._remote("back")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote back failed: {r.stderr.strip()}")
        st = self._ctl_status()
        if st.get("overlay"):
            raise CheckFailed(f"overlay still shown after back: {st}")
        w2, h2, _ = self._screenshot("osd-overlay-off")
        # ok twice: toggles on then off.
        self._remote("ok")
        if not self._ctl_status().get("overlay"):
            raise CheckFailed("second ok did not show the overlay")
        self._remote("ok")
        if self._ctl_status().get("overlay"):
            raise CheckFailed("third ok did not dismiss the overlay")
        return f"ok -> overlay on, back -> off, ok ok -> on/off ({w}x{h} " \
               f"osd-overlay-on/off.png)"

    def c_osd_overlay_zap(self) -> str:
        self._overlay_ensure(True)
        r = self._remote("channel-up")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote channel-up failed: {r.stderr.strip()}")
        st = self._probe_until(lambda p: is_playing_media(p), 30)
        st = self._ctl_status()
        if not st.get("overlay"):
            raise CheckFailed(f"zap dismissed the overlay: {st}")
        if st.get("state") != "playing":
            raise CheckFailed(f"not playing after zap with overlay up: {st}")
        w, h, _ = self._screenshot("osd-overlay-zap")
        return f"zap with overlay up -> still on, playing {st.get('file')} " \
               f"(osd-overlay-zap.png)"

    def c_osd_overlay_standby(self) -> str:
        self._overlay_ensure(True)
        r = self._remote("power")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote power failed: {r.stderr.strip()}")
        probe = self._probe_until(is_standby, 10)
        st = self._ctl_status()
        if not is_standby(probe) or st.get("overlay"):
            raise CheckFailed(f"standby did not dismiss the overlay: "
                              f"{st}, mpv {probe.get('mpv')!r}")
        r = self._remote("power")
        if r.returncode != 0:
            raise CheckFailed(f"e2e-remote power failed: {r.stderr.strip()}")
        self._probe_until(lambda p: is_playing_media(p) and not is_standby(p), 30)
        st = self._ctl_status()
        if st.get("overlay") or st.get("standby"):
            raise CheckFailed(f"wake restored the overlay: {st}")
        return "power with overlay up -> standby + overlay off; wake -> stays off"

    def c_osd_remaining(self) -> str:
        st = self._ctl_status()
        remaining = st.get("remaining")
        if remaining is None:
            raise CheckFailed(f"no 'remaining' in ctl status: {st}")
        probe = self.target.probe()
        time_pos = (probe.get("mpv") or {}).get("time-pos")
        files = probe.get("files") or []
        name = os.path.basename(mpv_path(probe) or "")
        duration = next((d for n, d in files if n == name), None)
        if not isinstance(time_pos, (int, float)) or duration is None:
            raise CheckFailed(f"not enough probe data: time-pos={time_pos!r}, "
                              f"duration={duration!r}")
        expected = duration - time_pos
        if abs(remaining - expected) > SCHEDULE_TOLERANCE:
            raise CheckFailed(f"remaining {remaining:.1f}s vs expected "
                              f"{expected:.1f}s (duration {duration:.1f}s, "
                              f"time-pos {time_pos:.1f}s)")
        return f"remaining {remaining:.1f}s ≈ duration - time-pos ({expected:.1f}s)"

    def c_unplug(self) -> str:
        r = self._vmctl("usb", "unplug", "--monitor", self.args.monitor)
        if r.returncode != 0:
            raise CheckFailed(f"usb unplug failed: {r.stderr.strip()}")
        t0 = time.monotonic()
        probe = self._probe_until(lambda p: stick_mount(p) is None and is_no_signal(p), 10)
        dt = time.monotonic() - t0
        if stick_mount(probe) is not None:
            raise CheckFailed(f"{probe.get('mount_point')} still mounted 10s after unplug")
        if not is_no_signal(probe):
            raise CheckFailed(f"mpv not on the NO SIGNAL card: path={mpv_path(probe)!r}")
        log = self._log_tail(5)
        if "NO SIGNAL" not in log:
            raise CheckFailed(f"app log has no NO SIGNAL line:\n{log}")
        w, h, a = self._screenshot("no-signal")
        if is_blank(a):
            raise CheckFailed("NO SIGNAL screenshot is black")
        return f"unmounted + NO SIGNAL within {dt:.1f}s (no-signal.png {w}x{h})"

    def c_replug(self) -> str:
        r = self._vmctl("usb", "plug", "--monitor", self.args.monitor)
        if r.returncode != 0:
            raise CheckFailed(f"usb plug failed: {r.stderr.strip()}")
        t0 = time.monotonic()
        probe = self._probe_until(lambda p: stick_mount(p) is not None and is_playing_media(p),
                                  30)
        if not is_playing_media(probe):
            raise CheckFailed(f"not playing 30s after re-insert: mount={stick_mount(probe)}, "
                              f"path={mpv_path(probe)!r}; log:\n{self._log_tail()}")
        dt = time.monotonic() - t0
        sched = self.c_schedule()
        return f"remounted + playing within {dt:.1f}s; {sched}"

    def c_crash(self) -> str:
        before = self.target.ssh("pidof mpv").stdout.strip()
        if not before:
            raise CheckFailed("no mpv process to kill")
        self.target.ssh("pkill -9 -x mpv")

        def respawned():
            now = self.target.ssh("pidof mpv").stdout.strip()
            return now and now != before

        if not wait_for(respawned, 30, interval=1):
            raise CheckFailed(f"mpv (was pid {before}) not respawned within 30s; "
                              f"log:\n{self._log_tail()}")
        probe = self._probe_until(is_playing_media, 60)
        if not is_playing_media(probe):
            raise CheckFailed(f"respawned but not playing: path={mpv_path(probe)!r}")
        return f"mpv {before} killed; respawned and playing {os.path.basename(mpv_path(probe) or '')}"

    def c_verify(self) -> str:
        r = self._host(["alpine/verify.sh", "--vm"], log_name="verify.log", timeout=300)
        if r.returncode != 0 or "verify: OK" not in r.stdout:
            raise CheckFailed("verify.sh reports problems after the test actions (verify.log)")
        return "verify: OK"

    def shutdown(self) -> None:
        if self.vm_proc is None:
            return
        if self.args.keep_running:
            print(f"[e2e] --keep-running: VM left up (vmctl pid {self.vm_proc.pid}); "
                  "stop it with `uv run task vm-stop`", flush=True)
            return

        def c_shutdown() -> str:
            assert self.vm_proc is not None
            if self.vm_proc.poll() is None:
                self.vm_proc.send_signal(signal.SIGINT)  # vmctl closes QEMU cleanly
                try:
                    self.vm_proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.vm_proc.kill()  # PDEATHSIG takes QEMU down with it
                    self.vm_proc.wait(timeout=10)
            free = wait_for(lambda: not vmctl.find_disk_holders(self.disk), 15)
            if not free:
                left = vmctl.find_disk_holders(self.disk)
                raise CheckFailed("QEMU still holds the disk after shutdown: "
                                  + "; ".join(h.describe() for h in left))
            return f"vmctl exited {self.vm_proc.returncode}; disk free"

        self.check("shutdown", c_shutdown)
        if self._serial_log is not None:
            self._serial_log.close()

    # -- driver --

    def run(self) -> int:
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        self.run_dir.mkdir(parents=True)
        steps: list[tuple[str, Callable[[], str]]] = [("preflight", self.c_preflight)]
        if self.args.rebuild:
            steps.append(("rebuild", self.c_rebuild))
        steps += [
            ("boot", self.c_boot),
            ("ssh", self.c_ssh),
            ("deploy", self.c_deploy),
            ("service", self.c_service),
            ("mount", self.c_mount),
            ("playing", self.c_playing),
            ("schedule", self.c_schedule),
            ("motion", self.c_motion),
            ("remote-power-off", self.c_remote_power_off),
            ("remote-power-on", self.c_remote_power_on),
            ("remote-zap", self.c_remote_zap),
            ("remote-autofire", self.c_remote_autofire),
            ("remote-volume", self.c_remote_volume),
            ("osd-flash", self.c_osd_flash),
            ("osd-overlay", self.c_osd_overlay),
            ("osd-overlay-zap", self.c_osd_overlay_zap),
            ("osd-overlay-standby", self.c_osd_overlay_standby),
            ("osd-remaining", self.c_osd_remaining),
            ("unplug", self.c_unplug),
            ("replug", self.c_replug),
        ]
        if not self.args.skip_crash:
            steps.append(("crash", self.c_crash))
        steps.append(("verify", self.c_verify))
        # Up to and including "playing", each step depends on the previous
        # one; after that the checks are independent observations.
        gating = {"preflight", "rebuild", "boot", "ssh", "deploy", "service", "mount", "playing"}
        try:
            for i, (name, fn) in enumerate(steps):
                if not self.check(name, fn) and name in gating:
                    self.skip_rest([n for n, _ in steps[i + 1:]], f"'{name}' failed")
                    break
        finally:
            self.shutdown()
        return self.finish()

    def finish(self) -> int:
        report_path = self.run_dir / "report.json"
        report_path.write_text(json.dumps(
            {"ok": self.report.ok, "results": [asdict(r) for r in self.report.results]},
            indent=2))
        print("\n[e2e] ===== summary =====")
        for r in self.report.results:
            print(f"[e2e] {'PASS' if r.ok else 'FAIL'}  {r.name:<10} {r.seconds:>6.1f}s  "
                  f"{r.detail.splitlines()[0] if r.detail else ''}")
        verdict = "ALL CHECKS PASSED" if self.report.ok else "FAILED"
        print(f"[e2e] {verdict}  (report, logs, screenshots: {self.run_dir})", flush=True)
        return 0 if self.report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="e2e", description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--rebuild", action="store_true",
                   help="destroy and rebuild the VM first (vm-rebuild; the reproducibility check)")
    p.add_argument("--usb", default=DEFAULT_USB, help=f"mock stick image (default: {DEFAULT_USB})")
    p.add_argument("--disk", default=vmctl.DEFAULT_DISK, help="VM system disk")
    p.add_argument("--monitor", default=vmctl.DEFAULT_MONITOR_SOCK, help="QMP socket path")
    p.add_argument("--run-dir", default=DEFAULT_RUN_DIR,
                   help=f"where logs, screenshots and report.json go (default: {DEFAULT_RUN_DIR})")
    p.add_argument("--keep-running", action="store_true",
                   help="leave the VM running afterwards (stop it with `uv run task vm-stop`)")
    p.add_argument("--skip-crash", action="store_true", help="skip the mpv crash-recovery check")
    return p


def main(argv: list[str] | None = None) -> int:
    return E2E(build_parser().parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
