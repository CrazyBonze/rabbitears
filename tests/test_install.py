"""Unit tests for vm/install.py (the zero-touch VM base install driver).

No QEMU, libvirt or serial port: a scripted fake guest stands in for the live
Alpine system on the other end of the serial console, and a fake clock makes
the watchdog timeouts instant. vm/install.py is a standalone script (not a
package), so it is loaded by file path.

These tests pin down the failures from docs/vm-e2e-blockers.md:
  * an answerfile that leaves an option unset makes setup-alpine ask a
    question nobody answers -> the install must fail fast, naming it;
  * the echoed command line / a stale prompt must never count as "done";
  * one Enter per line ("\\r"), never "\\r\\n".
"""

import base64
import contextlib
import hashlib
import importlib.util
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_INSTALL_PATH = _REPO / "vm" / "install.py"
_spec = importlib.util.spec_from_file_location("rabbitears_vm_install", _INSTALL_PATH)
assert _spec is not None and _spec.loader is not None
install = importlib.util.module_from_spec(_spec)
sys.modules["rabbitears_vm_install"] = install
_spec.loader.exec_module(install)

ANSWERFILE = _REPO / "alpine" / "answerfile"
PROVISION = _REPO / "vm" / "provision.sh"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKeyOnlyForUnitTests rabbitears-vm"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.t += d


class FakeGuest:
    """The far end of the serial console: echoes typed lines, runs a responder.

    Output is scheduled at fake-clock times, so "nothing printed for 20s" is
    just the clock moving past the last scheduled byte.
    """

    def __init__(self, clock: FakeClock, responder) -> None:
        self.clock = clock
        self.responder = responder
        self.queue: list[tuple[float, int, bytes]] = []
        self._seq = 0
        self._line = ""
        self.lines: list[str] = []
        self.writes: list[bytes] = []

    def emit(self, text: str, delay: float = 0.0) -> None:
        self._seq += 1
        self.queue.append((self.clock() + delay, self._seq, text.encode()))
        self.queue.sort()

    def _ready(self) -> list[tuple[float, int, bytes]]:
        return [item for item in self.queue if item[0] <= self.clock()]

    @property
    def in_waiting(self) -> int:
        return sum(len(b) for _, _, b in self._ready())

    def read(self, n: int) -> bytes:
        out = b""
        for item in self._ready():
            if len(out) >= n:
                break
            out += item[2]
            self.queue.remove(item)
        return out

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        for ch in data.decode():
            if ch == "\r":
                line, self._line = self._line, ""
                self.lines.append(line)
                self.emit(line + "\r\n")  # the terminal echoes what was typed
                self.responder(self, line)
            else:
                self._line += ch

    def flush(self) -> None:
        pass


_SENTINEL = re.compile(r'^(.*); echo "RB:\$\?:(t\d+)"$')
HEADINGS_BEFORE_USER = ["Keymap", "Hostname", "Interface", "Timezone", "Proxy",
                        "APK Mirror", "User"]


def heading(name: str) -> str:
    # setup-alpine prints headings in colour on a tty.
    return f"\r\n\x1b[97m {name}\x1b[0m\r\n\x1b[97m{'-' * (len(name) + 2)}\x1b[0m\r\n"


class FakeLiveSystem:
    """A scripted Alpine live system: login, a root shell, setup-alpine."""

    def __init__(self, setup_alpine=None) -> None:
        self.files: dict[str, str] = {}
        self.setup_alpine = setup_alpine or self.setup_ok
        self.logged_in = False

    def __call__(self, guest: FakeGuest, line: str) -> None:
        if not self.logged_in:
            if line == "root":
                self.logged_in = True
                # Real busybox ash follows its prompt with a cursor-position
                # query (ESC[6n); seen on the VM.
                guest.emit("Welcome to Alpine Linux 3.24\r\nlocalhost:~# \x1b[6n", 0.2)
            return
        m = _SENTINEL.match(line)
        if not m:
            out = self.plain(line)
            if out is not None:
                guest.emit(out + "localhost:~# ", 0.1)
            return
        cmd, token = m.groups()
        if cmd.startswith("setup-alpine"):
            self.setup_alpine(guest, token)
            return
        out, rc = self.command(cmd)
        guest.emit(f"{out}RB:{rc}:{token}\r\nlocalhost:~# ", 0.1)

    def plain(self, line: str) -> str | None:
        m = re.match(r"sha256sum (\S+)$", line)
        if m:
            data = self.files.get(m.group(1), "").encode()
            return f"{hashlib.sha256(data).hexdigest()}  {m.group(1)}\r\n"
        if line == "poweroff":
            return ""
        return None

    def command(self, cmd: str) -> tuple[str, int]:
        m = re.match(r"rm -f (\S+)$", cmd)
        if m:
            self.files.pop(m.group(1), None)
            return "", 0
        m = re.match(r"echo '([^']*)' >> (\S+)$", cmd)
        if m:
            self.files[m.group(2)] = self.files.get(m.group(2), "") + m.group(1) + "\n"
            return "", 0
        m = re.match(r"base64 -d (\S+) > (\S+) && rm -f \S+$", cmd)
        if m:
            raw = base64.b64decode(self.files.pop(m.group(1)))
            self.files[m.group(2)] = raw.decode()
            return "", 0
        if cmd in ("dmesg -n 1", "sync"):
            return "", 0
        return f"sh: {cmd.split()[0]}: not found\r\n", 127

    @staticmethod
    def setup_ok(guest: FakeGuest, token: str) -> None:
        t = 1.0
        for name in HEADINGS_BEFORE_USER + ["Disk & Install"]:
            guest.emit(heading(name), t)
            t += 2.0
        guest.emit("Installing system on /dev/sda3:\r\n(1/40) Installing musl\r\n", t)
        guest.emit("Installation is complete. Please reboot.\r\n", t + 5)
        guest.emit(f"RB:0:{token}\r\nrabbitears-vm:~# ", t + 5.1)


def make_console(system) -> tuple[FakeClock, FakeGuest, "install.Console"]:
    clock = FakeClock()
    guest = FakeGuest(clock, system)
    con = install.Console(guest, transcript=io.BytesIO(), clock=clock, sleep=clock.sleep)
    return clock, guest, con


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


class InstallFlowTest(unittest.TestCase):
    """login -> answerfile -> setup-alpine, against the fake live system."""

    def _boot(self, system):
        clock, guest, con = make_console(system)
        guest.emit("\r\nWelcome to Alpine Linux 3.24\r\nlocalhost login: ", 5.0)
        return clock, guest, con

    def test_happy_path(self):
        system = FakeLiveSystem()
        clock, guest, con = self._boot(system)
        answers = install.render_answerfile(ANSWERFILE.read_text(), PUBKEY)
        size = {"bytes": 200_000}

        def disk_size():
            if any("Disk & Install" in x for x in [con.buf]):
                size["bytes"] += 4096
            return size["bytes"]

        with quiet() as out:
            install.login_live(con)
            install.send_answerfile(con, answers)
            install.run_setup_alpine(con, disk_size)
        # The guest received exactly the rendered answerfile.
        self.assertEqual(system.files["/root/af"], answers)
        self.assertIn('ROOTSSHKEY="' + PUBKEY + '"', system.files["/root/af"])
        # setup-alpine ran unattended with an empty root password.
        self.assertIn('setup-alpine -e -f /root/af; echo "RB:$?:', "\n".join(guest.lines))
        # Phases were reported in order.
        logged = re.findall(r"setup-alpine: ([A-Za-z &]+) \(", out.getvalue())
        self.assertEqual(logged, HEADINGS_BEFORE_USER + ["Disk & Install"])
        # Everything read was also written to the transcript.
        self.assertIn(b"Installation is complete", con.transcript.getvalue())

    def test_one_enter_per_line(self):
        """'\\r\\n' is two Enters: the spare one answers the next question."""
        system = FakeLiveSystem()
        _, guest, con = self._boot(system)
        with quiet():
            install.login_live(con)
            install.send_answerfile(con, "A=1\n")
        self.assertTrue(guest.writes)
        for data in guest.writes:
            self.assertTrue(data.endswith(b"\r"), data)
            self.assertNotIn(b"\n", data, data)

    def test_hang_at_interactive_question_fails_fast_and_names_it(self):
        """The real blocker: USEROPTS unset -> 'Setup a user?' -> silent hang."""
        def hangs_at_user(guest, token):
            t = 1.0
            for name in HEADINGS_BEFORE_USER:
                guest.emit(heading(name), t)
                t += 1.0
            guest.emit("Setup a user? (enter a lower-case loginname, or 'no') [no] ", t)

        system = FakeLiveSystem(setup_alpine=hangs_at_user)
        clock, guest, con = self._boot(system)
        with quiet():
            install.login_live(con)
            install.send_answerfile(con, "A=1\n")
            start = clock()
            with self.assertRaises(install.InstallError) as ctx:
                install.run_setup_alpine(con, lambda: 200_000)
        msg = str(ctx.exception)
        self.assertIn("Setup a user?", msg)
        self.assertIn("alpine/answerfile", msg)
        self.assertLess(clock() - start, 60, "must not wait out the 2400s timeout")

    def test_nonzero_exit_is_an_error(self):
        def fails(guest, token):
            guest.emit(heading("Keymap"), 1)
            guest.emit(f"ERROR: something broke\r\nRB:1:{token}\r\nlocalhost:~# ", 2)

        _, _, con = self._boot(FakeLiveSystem(setup_alpine=fails))
        with quiet():
            install.login_live(con)
            with self.assertRaises(install.InstallError) as ctx:
                install.run_setup_alpine(con, lambda: 200_000)
        self.assertIn("status 1", str(ctx.exception))

    def test_success_without_install_complete_is_an_error(self):
        def no_disk(guest, token):
            guest.emit(heading("Keymap"), 1)
            guest.emit(f"RB:0:{token}\r\nlocalhost:~# ", 2)

        _, _, con = self._boot(FakeLiveSystem(setup_alpine=no_disk))
        with quiet():
            install.login_live(con)
            with self.assertRaises(install.InstallError) as ctx:
                install.run_setup_alpine(con, lambda: 200_000)
        self.assertIn("Installation is complete", str(ctx.exception))

    def test_corrupted_answerfile_transfer_is_detected(self):
        class Corrupting(FakeLiveSystem):
            def command(self, cmd):
                out, rc = super().command(cmd)
                if cmd.startswith("base64 -d"):
                    self.files["/root/af"] += "garbage"
                return out, rc

        _, _, con = self._boot(Corrupting())
        with quiet():
            install.login_live(con)
            with self.assertRaises(install.InstallError) as ctx:
                install.send_answerfile(con, "A=1\n")
        self.assertIn("sha256", str(ctx.exception))


class ConsoleTest(unittest.TestCase):
    def test_echoed_command_never_satisfies_the_sentinel(self):
        # A guest that only echoes (the command never finishes).
        clock, guest, con = make_console(lambda g, line: None)
        with self.assertRaises(install.InstallError):
            con.run("sleep 999", timeout=10)
        self.assertIn('RB:$?:t1', con.buf)  # the echo was seen, and ignored

    def test_stale_prompt_is_drained_before_sending(self):
        clock, guest, con = make_console(lambda g, line: None)
        guest.emit("localhost:~# ")  # left over from an earlier command
        con.pump()
        con.send("true")
        with self.assertRaises(install.InstallError):
            con.expect(install.SHELL_PROMPT, 5, "a fresh prompt")

    def test_run_returns_the_exit_status(self):
        def responder(g, line):
            m = _SENTINEL.match(line)
            g.emit(f"oops\r\nRB:3:{m.group(2)}\r\n# ", 0.5)

        _, _, con = make_console(responder)
        self.assertEqual(con.run("false"), 3)
        self.assertIn("oops", con.last_output)

    def test_idle_console_fails(self):
        clock, guest, con = make_console(lambda g, line: None)
        with self.assertRaises(install.InstallError) as ctx:
            con.expect(re.compile("never"), 1000, "x", (install.IdleCheck(60),))
        self.assertIn("no console output", str(ctx.exception))
        self.assertLess(clock.t - 1000.0, 120)


class WatchdogTest(unittest.TestCase):
    def _printing(self, first: str, every: float = 10.0, count: int = 200):
        clock, guest, con = make_console(lambda g, line: None)
        con.mark = 0
        guest.emit(first)
        for i in range(count):
            guest.emit(f"progress {i}\r\n", every * (i + 1))
        return clock, guest, con

    def test_phase_overrunning_its_budget_fails(self):
        clock, guest, con = self._printing(heading("APK Mirror"))
        phases = install.PhaseTracker(report=lambda m: None)
        with self.assertRaises(install.InstallError) as ctx:
            con.expect(re.compile("never"), 5000, "x", (phases,))
        self.assertIn("APK Mirror", str(ctx.exception))
        self.assertEqual(phases.seen, ["APK Mirror"])
        self.assertLess(clock.t - 1000.0, install.PHASE_BUDGETS["APK Mirror"] + 30)

    def test_disk_not_growing_fails(self):
        clock, guest, con = self._printing(heading("Disk & Install"))
        phases = install.PhaseTracker(report=lambda m: None)
        disk = install.DiskGrowthCheck(phases, lambda: 200_000, timeout=120)
        with self.assertRaises(install.InstallError) as ctx:
            con.expect(re.compile("never"), 5000, "x", (phases, disk))
        self.assertIn("has not grown", str(ctx.exception))
        self.assertLess(clock.t - 1000.0, 200)

    def test_disk_growing_passes(self):
        clock, guest, con = self._printing(heading("Disk & Install"), count=20)
        guest.emit("done\r\n", 150)
        phases = install.PhaseTracker(report=lambda m: None)
        disk = install.DiskGrowthCheck(phases, lambda: int(clock.t) * 1000, timeout=120)
        con.expect(re.compile("done"), 5000, "done", (phases, disk))
        self.assertTrue(disk.grown)


class WaitingForInputTest(unittest.TestCase):
    QUESTIONS = [
        "Setup a user? (enter a lower-case loginname, or 'no') [no] ",
        "HTTP/FTP proxy URL? (e.g. 'http://proxy:8080', or 'none') [none] ",
        "WARNING: Erase the above disk(s) and continue? (y/n) [n] ",
        "New password: ",
        "Which timezone are you in? (or '?' or 'none') [UTC] ",
        "Enter ssh key or URL for root (or 'none') [none] ",
        "Which NTP client to run? ('busybox', 'openntpd', 'chrony' or 'none') [busybox] ",
        "Enter mirror number or URL: [1] ",
        "\x1b[97mSelect keyboard layout: [none] \x1b[0m",
    ]
    NOT_QUESTIONS = [
        "localhost:~# ",
        "rabbitears-vm:~# ",
        "Installing system on /dev/sda3:",
        "(12/40) Installing musl (1.2.5-r9)",
        "rabbitears-vm login: ",
        "",
        " * Starting networking ... [ ok ]",
    ]

    def test_questions(self):
        for q in self.QUESTIONS:
            with self.subTest(q=q):
                self.assertIsNotNone(install.waiting_for_input("earlier output\r\n" + q))

    def test_not_questions(self):
        for s in self.NOT_QUESTIONS:
            with self.subTest(s=s):
                self.assertIsNone(install.waiting_for_input("earlier output\r\n" + s))

    def test_finished_line_is_not_waiting(self):
        # A question only blocks if the cursor is still on its line.
        self.assertIsNone(install.waiting_for_input("Setup a user? [no] \r\nok\r\n"))
        self.assertIsNone(install.waiting_for_input("Which one? [x]\r\n"))


class RenderAnswerfileTest(unittest.TestCase):
    def test_replaces_placeholder_only(self):
        text = 'A=1\nROOTSSHKEY="injected-by-vm/install.py"\nB=2\n'
        out = install.render_answerfile(text, PUBKEY + "\n")
        self.assertEqual(out, f'A=1\nROOTSSHKEY="{PUBKEY}"\nB=2\n')

    def test_appends_when_missing(self):
        out = install.render_answerfile("A=1\n", PUBKEY)
        self.assertTrue(out.endswith(f'ROOTSSHKEY="{PUBKEY}"\n'))

    def test_rejects_non_keys(self):
        for bad in ["", "not a key", PUBKEY + "\n" + PUBKEY, 'ssh-ed25519 AAA" ; rm -rf /']:
            with self.subTest(bad=bad), self.assertRaises(install.InstallError):
                install.render_answerfile("A=1\n", bad)

    def test_repo_answerfile_has_no_real_key(self):
        # The key is per clone (vm/keys, gitignored); the repo carries a placeholder.
        self.assertIn('ROOTSSHKEY="injected-by-vm/install.py"', ANSWERFILE.read_text())


def source_answerfile(path: Path) -> tuple[dict[str, str], set[str]]:
    """Source the answerfile with /bin/sh exactly as setup-alpine does.

    Returns (all variables, the subset that is exported).
    """
    script = ('. "$1"; set | sed -n "s/^\\([A-Za-z_][A-Za-z_0-9]*\\)=.*/\\1/p" > "$2"; '
              'env -0 > "$3"')
    with tempfile.TemporaryDirectory() as tmp:
        names_f, env_f = Path(tmp) / "names", Path(tmp) / "env"
        r = subprocess.run(["/bin/sh", "-c", script, "sh", str(path), str(names_f), str(env_f)],
                           env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
                           timeout=10)
        assert r.returncode == 0, r.stderr
        exported = {e.split("=", 1)[0] for e in env_f.read_text().split("\0") if "=" in e}
        names = set(names_f.read_text().split())
        # Re-source to read each value verbatim (multi-line values included).
        values = {}
        for name in names:
            v = subprocess.run(["/bin/sh", "-c", f'. "$1"; printf %s "${name}"', "sh", str(path)],
                               env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
                               timeout=10)
            values[name] = v.stdout
    return values, exported


class AnswerfileTest(unittest.TestCase):
    """alpine/answerfile must answer every question setup-alpine 3.22 asks.

    Each unset *OPTS variable makes setup-alpine (or a setup-* child) prompt on
    the serial console, and the install hangs before it touches the disk.
    """

    REQUIRED = [
        "KEYMAPOPTS", "HOSTNAMEOPTS", "DEVDOPTS", "INTERFACESOPTS", "TIMEZONEOPTS",
        "PROXYOPTS", "NTPOPTS", "APKREPOSOPTS", "USEROPTS", "SSHDOPTS", "ROOTSSHKEY",
        "DISKOPTS", "LBUOPTS", "APKCACHEOPTS", "empty_root_password", "ERASE_DISKS",
    ]

    @classmethod
    def setUpClass(cls):
        cls.vars, cls.exported = source_answerfile(ANSWERFILE)

    def test_every_option_is_set(self):
        missing = [v for v in self.REQUIRED if not self.vars.get(v)]
        self.assertEqual(missing, [], "unset options make setup-alpine prompt and hang")

    def test_proxy_answer_is_non_interactive(self):
        # setup-proxy prompts unless given 'none' or an http(s) URL.
        self.assertRegex(self.vars["PROXYOPTS"], r"^(none|https?://\S+)$")

    def test_user_answer_is_non_interactive(self):
        # setup-user prompts unless given a username (or 'none').
        words = self.vars["USEROPTS"].split()
        self.assertTrue(words and not words[-1].startswith("-"),
                        "USEROPTS must end with a username or 'none'")

    def test_sshd_answer_is_non_interactive(self):
        self.assertIn(self.vars["SSHDOPTS"].split()[-1], ("openssh", "dropbear", "none"))

    def test_erase_disks_matches_diskopts_and_is_exported(self):
        disks = [w for w in self.vars["DISKOPTS"].split() if w.startswith("/dev/")]
        # setup-disk's confirm_erase compares the strings exactly.
        self.assertEqual(self.vars["ERASE_DISKS"], " ".join(disks))
        self.assertIn("ERASE_DISKS", self.exported, "setup-disk is a child process")

    def test_root_password_empty(self):
        self.assertEqual(self.vars["empty_root_password"], "1")

    def test_interfaces_use_dhcp(self):
        # Without a dhcp interface setup-alpine runs setup-dns, which prompts.
        self.assertRegex(self.vars["INTERFACESOPTS"], r"iface eth0 inet dhcp")

    def test_apk_repos_are_versioned_and_match_the_pinned_iso(self):
        version = re.search(r'^ALPINE_VERSION="(\d+\.\d+)\.\d+"', PROVISION.read_text(), re.M)
        self.assertIsNotNone(version, "ALPINE_VERSION not found in vm/provision.sh")
        repos = self.vars["APKREPOSOPTS"].split()
        self.assertTrue(repos)
        for url in repos:
            with self.subTest(url=url):
                # setup-apkrepos writes each word verbatim; a bare ".../alpine"
                # makes apk fetch ".../alpine/x86_64/APKINDEX.tar.gz" (404).
                self.assertRegex(url, r"^https?://\S+/v\d+\.\d+/(main|community)$")
                self.assertIn(f"/v{version.group(1)}/", url)
        self.assertTrue(any(u.endswith("/community") for u in repos),
                        "alpine/packages.txt needs community (mpv, py3-evdev, exfatprogs)")


class HelpersTest(unittest.TestCase):
    def test_disk_boot_xml(self):
        xml = ("<domain><os>\n    <type arch='x86_64' machine='pc-i440fx-8.2'>hvm</type>\n"
               "    <kernel>/b/vmlinuz</kernel>\n    <initrd>/b/initrd</initrd>\n"
               "    <cmdline>console=ttyS0</cmdline>\n  </os></domain>")
        out = install.disk_boot_xml(xml)
        self.assertNotIn("<kernel>", out)
        self.assertNotIn("<initrd>", out)
        self.assertNotIn("<cmdline>", out)
        self.assertIn("<boot dev='hd'/>", out)

    def test_check_installed(self):
        ok = "rabbitears-vm\n/dev/sda3 ext4\n6.12.1-0-lts\napk-tools 3.0\n"
        self.assertIsNone(install.check_installed(ok))
        live = "localhost\ntmpfs tmpfs\n"
        self.assertIsNotNone(install.check_installed(live))
        live_named = "rabbitears-vm\ntmpfs tmpfs\n"
        self.assertIn("live system", install.check_installed(live_named))
        self.assertIsNotNone(install.check_installed(""))


if __name__ == "__main__":
    unittest.main()
