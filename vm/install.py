#!/usr/bin/env python3
"""Rabbit Ears — zero-touch VM install driver.

Drives the Alpine base install over the VM's serial console (pyserial), then
hands off to paramiko (SSH) once the installed system is up. No human in the
loop. Run by vm/provision.sh (as root, with the host's system python3), never
directly.

Prereqs (set up by vm/provision.sh before this runs):
  * the VM is DEFINED (not started) with direct kernel boot (console=ttyS0),
    a pty serial, the Alpine ISO as cdrom, and an 8GB IDE disk at /dev/sda;
  * pyserial + paramiko are importable (apt: python3-serial, python3-paramiko).

Flow:
  1. start the VM and attach to its serial pty;
  2. wait for the live system's `login:`, log in as `root`;
  3. send the answerfile (with this clone's SSH key injected), check its
     sha256 on the guest, and run `setup-alpine -e -f /root/af`;
  4. `poweroff` the live system once setup-alpine reports success;
  5. redefine the domain to boot from disk and start it;
  6. poll for the VM's IP, verify over SSH that the *installed* system
     answers (hostname, disk-backed root), then power it off so the disk is
     left at rest for vmctl.py.

Reliability rules (see docs/vm-e2e-blockers.md for the failures they fix):
  * Every byte read from the console is appended to a transcript
    (RB_INSTALL_LOG, default /var/lib/rabbitears-vm/install-serial.log).
  * Commands are confirmed by a sentinel the echoed command line can't match
    (`echo "RB:$?:<token>"` prints `RB:0:<token>`), after draining stale
    output, so an old prompt or the echo never counts as "done".
  * Lines end with "\\r" only (one Enter). "\\r\\n" is two Enters: the spare
    newline sits in the tty buffer and silently answers whatever question
    comes next, which made installs pass or hang by luck.
  * Watchdogs fail fast instead of waiting out one long timeout: an
    interactive question on the console (a missing answerfile option), a
    setup-alpine phase that overruns its budget, a console that goes silent,
    and a system disk that doesn't grow once "Disk & Install" starts.
  * On failure the live VM is destroyed (unless RB_KEEP_VM=1), so no orphaned
    QEMU is left holding the disk.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Callable, NoReturn

VM_NAME = os.environ.get("RB_VM_NAME", "rabbitears-vm")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWERFILE = os.path.join(REPO_ROOT, "alpine", "answerfile")
SSH_KEY = os.path.join(REPO_ROOT, "vm", "keys", "rabbitears-vm")
SSH_PUBKEY = SSH_KEY + ".pub"
DISK_PATH = os.environ.get("RB_DISK_PATH", f"/var/lib/libvirt/images/{VM_NAME}.qcow2")
SERIAL_LOG = os.environ.get("RB_INSTALL_LOG", "/var/lib/rabbitears-vm/install-serial.log")
KEEP_ON_FAILURE = os.environ.get("RB_KEEP_VM") == "1"
EXPECTED_HOSTNAME = "rabbitears-vm"  # HOSTNAMEOPTS in alpine/answerfile

LOGIN_PROMPT = re.compile(r"login: *$", re.M)
# The live root shell prompt, e.g. "localhost:~# ". busybox ash's line editor
# follows it with a cursor-position query (ESC [ 6 n), so allow trailing
# escape sequences.
SHELL_PROMPT = re.compile(r"# *(?:\x1b\[[0-9;?]*[A-Za-z])*$")
INSTALL_DONE = "Installation is complete"  # printed by setup-disk on success
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]")

# setup-alpine's section headings, in order, with how long each section may
# take before we call it stuck. "User" also covers setup-sshd (apk add
# openssh); "Disk & Install" covers partitioning + installing the base system.
PHASE_BUDGETS: dict[str, float] = {
    "Keymap": 120,
    "Hostname": 180,   # restarts networking (DHCP)
    "Interface": 180,
    "Root Password": 60,
    "Timezone": 180,
    "Proxy": 60,
    "Network Time Protocol": 180,
    "APK Mirror": 300,
    "User": 300,
    "Xen": 60,
    "Disk & Install": 1500,
}
_HEADING_RE = re.compile(
    r"^ (" + "|".join(re.escape(h) for h in PHASE_BUDGETS) + r") *\r?$", re.M
)

# An alpine-conf `ask` prints the question, then "[default] ", and blocks.
_QUESTION_RES = [
    re.compile(r"\[[^\]\n]*\] *$"),          # "Setup a user? (...) [no] "
    re.compile(r"\? *$"),                    # "HTTP/FTP proxy URL? ..."
    re.compile(r"\(y/n\)", re.I),            # "Erase the above disk(s)...? (y/n)"
    re.compile(r"password: *$", re.I),       # "New password:"
    re.compile(r"\(or '[^']*'\) *$"),        # "... (or 'none')"
]
_OPENRC_STATUS = re.compile(r"\[ *(ok|!!|\?\?) *\] *$")

Clock = Callable[[], float]


class InstallError(Exception):
    """The install can't continue; the message says why (and what to fix)."""


def log(msg: str) -> None:
    print(f"[install] {msg}", flush=True)


def die(msg: str) -> NoReturn:
    print(f"[install] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _lines(text: str) -> list[str]:
    return [ln for ln in strip_ansi(text).replace("\r", "\n").split("\n") if ln.strip()]


def waiting_for_input(text: str) -> str | None:
    """Return the last console line if it looks like an interactive question.

    A question leaves the cursor on its line (no trailing newline). The shell
    prompt, the getty login prompt and OpenRC's "[ ok ]" are not questions.
    """
    clean = strip_ansi(text)
    if clean.rstrip(" \t").endswith(("\n", "\r")):
        return None  # the last line is finished; nothing is waiting on it
    lines = _lines(clean)
    if not lines:
        return None
    last = lines[-1].rstrip()
    if last.endswith("#") or LOGIN_PROMPT.search(last) or _OPENRC_STATUS.search(last):
        return None
    for rx in _QUESTION_RES:
        if rx.search(last):
            return last.strip()
    return None


def render_answerfile(text: str, pubkey: str) -> str:
    """Return the answerfile with ROOTSSHKEY set to ``pubkey``.

    The repo's alpine/answerfile carries a placeholder; the clone's own key
    (vm/keys/rabbitears-vm.pub, gitignored) is injected here so provisioning
    never rewrites a tracked file.
    """
    key = pubkey.strip()
    if "\n" in key or not re.match(r"^(ssh-|ecdsa-|sk-)", key) or '"' in key:
        raise InstallError(f"not a single-line OpenSSH public key: {key[:40]!r}")
    line = f'ROOTSSHKEY="{key}"'
    if re.search(r"^ROOTSSHKEY=.*$", text, re.M):
        return re.sub(r"^ROOTSSHKEY=.*$", lambda _m: line, text, count=1, flags=re.M)
    return text.rstrip("\n") + "\n" + line + "\n"


# --- The serial console -----------------------------------------------------

class Console:
    """A serial console with a transcript, stale-output draining and sentinels.

    ``ser`` needs ``in_waiting``, ``read(n)``, ``write(b)`` and ``flush()``
    (a pyserial ``Serial``, or a fake in tests). ``clock``/``sleep`` are
    injectable so the watchdogs can be tested without waiting.
    """

    def __init__(self, ser, transcript=None, clock: Clock = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.ser = ser
        self.transcript = transcript
        self.clock = clock
        self.sleep = sleep
        self.buf = ""
        self.mark = 0  # expect() only looks at output after this offset
        self.last_rx = clock()
        self.last_output = ""  # output of the last run() command
        self._token = 0

    def pump(self) -> bool:
        """Read whatever is pending. Returns True if anything arrived."""
        n = self.ser.in_waiting
        if not n:
            return False
        data = self.ser.read(n)
        if not data:
            return False
        if self.transcript is not None:
            self.transcript.write(data)
            self.transcript.flush()
        self.buf += data.decode("utf-8", "replace")
        self.last_rx = self.clock()
        return True

    def drain(self, quiet: float = 0.3, limit: float = 3.0) -> None:
        """Consume output until the console is quiet, then ignore all of it."""
        start = quiet_since = self.clock()
        while self.clock() - start < limit:
            if self.pump():
                quiet_since = self.clock()
            elif self.clock() - quiet_since >= quiet:
                break
            else:
                self.sleep(0.05)
        self.mark = len(self.buf)

    def send(self, line: str) -> None:
        """Drain stale output, then type ``line`` and press Enter once."""
        self.drain()
        self.ser.write((line + "\r").encode())
        self.ser.flush()

    def expect(self, pattern: re.Pattern, timeout: float, label: str,
               checks: tuple = ()) -> re.Match:
        """Wait for ``pattern`` in output newer than ``mark``.

        Each check is called as ``check(console)`` on every poll and returns
        an error string to abort, or None.
        """
        deadline = self.clock() + timeout
        while True:
            got = self.pump()
            m = pattern.search(self.buf, self.mark)
            if m:
                self.mark = m.end()
                return m
            for check in checks:
                err = check(self)
                if err:
                    raise InstallError(err)
            if self.clock() >= deadline:
                raise InstallError(f"timed out after {timeout:.0f}s waiting for {label}")
            if not got:
                self.sleep(0.05)

    def run(self, cmd: str, timeout: float = 30.0, label: str | None = None,
            checks: tuple = ()) -> int:
        """Run a shell command on the console; return its exit status.

        The sentinel ``RB:<status>:<token>`` only appears in the command's
        *output*: the echoed command line shows ``RB:$?:<token>``.
        """
        self._token += 1
        token = f"t{self._token}"
        self.send(f'{cmd}; echo "RB:$?:{token}"')
        start = self.mark  # set by send()'s drain
        m = self.expect(re.compile(rf"RB:(\d+):{token}\b"), timeout, label or repr(cmd),
                        checks)
        self.last_output = self.buf[start:m.start()]
        return int(m.group(1))

    def tail(self, lines: int = 30) -> str:
        return "\n".join(_lines(self.buf[-8000:])[-lines:])


# --- Watchdogs (checks for Console.expect) ---------------------------------

class InputPromptCheck:
    """Fail when the guest sits on an interactive question."""

    def __init__(self, idle: float = 20.0) -> None:
        self.idle = idle

    def __call__(self, con: Console) -> str | None:
        if con.clock() - con.last_rx < self.idle:
            return None
        question = waiting_for_input(con.buf[con.mark:])
        if question:
            return (f"the guest is waiting for interactive input: {question!r}. "
                    "An option in alpine/answerfile is missing or invalid "
                    "(setup-alpine asks when an *OPTS variable is unset).")
        return None


class IdleCheck:
    """Fail when the console has printed nothing for ``idle`` seconds."""

    def __init__(self, idle: float = 600.0) -> None:
        self.idle = idle

    def __call__(self, con: Console) -> str | None:
        quiet = con.clock() - con.last_rx
        if quiet >= self.idle:
            return f"no console output for {quiet:.0f}s"
        return None


class PhaseTracker:
    """Log setup-alpine's sections as they start; fail if one overruns.

    Section headings (" Keymap", " Disk & Install", ...) are printed on their
    own line, possibly wrapped in colour codes.
    """

    def __init__(self, budgets: dict[str, float] | None = None,
                 report: Callable[[str], None] = log) -> None:
        self.budgets = budgets or PHASE_BUDGETS
        self.report = report
        self.current: str | None = None
        self.since = 0.0
        self.started = None
        self.seen: list[str] = []
        self._scan = None  # offset in con.buf; set on first call

    def __call__(self, con: Console) -> str | None:
        now = con.clock()
        if self._scan is None:
            self._scan = con.mark
            self.started = now
            self.since = now
        new = con.buf[self._scan:]
        cut = new.rfind("\n")
        if cut >= 0:  # only complete lines
            for m in _HEADING_RE.finditer(strip_ansi(new[:cut + 1])):
                self.current = m.group(1)
                self.since = now
                self.seen.append(self.current)
                self.report(f"setup-alpine: {self.current} (+{now - self.started:.0f}s)")
            self._scan += cut + 1
        if self.current is not None:
            budget = self.budgets.get(self.current, 300)
            if now - self.since > budget:
                return (f"setup-alpine is stuck in its '{self.current}' section "
                        f"({now - self.since:.0f}s, budget {budget:.0f}s)")
        return None


class DiskGrowthCheck:
    """Fail if the system disk doesn't grow once 'Disk & Install' begins.

    ``size`` returns the disk's allocated bytes (st_blocks * 512): a fresh
    sparse qcow2 is ~200K; partitioning + mkfs grow it within seconds.
    """

    def __init__(self, phases: PhaseTracker, size: Callable[[], int],
                 timeout: float = 300.0) -> None:
        self.phases = phases
        self.size = size
        self.timeout = timeout
        self.base: int | None = None
        self.since = 0.0
        self.grown = False

    def __call__(self, con: Console) -> str | None:
        if self.grown or self.phases.current != "Disk & Install":
            return None
        try:
            size = self.size()
        except OSError:
            return None
        if self.base is None:
            self.base, self.since = size, con.clock()
            return None
        if size > self.base:
            self.grown = True
            return None
        if con.clock() - self.since > self.timeout:
            return (f"the system disk has not grown in {self.timeout:.0f}s since "
                    f"'Disk & Install' began (still {size} bytes allocated)")
        return None


def allocated_bytes(path: str) -> int:
    return os.stat(path).st_blocks * 512


# --- The install, step by step ----------------------------------------------

def login_live(con: Console, timeout: float = 180.0) -> None:
    log("waiting for the live system's login prompt...")
    con.expect(LOGIN_PROMPT, timeout, "the live system's login prompt", (IdleCheck(120),))
    con.send("root")
    con.expect(SHELL_PROMPT, 60, "the live root shell prompt")
    log("logged in as root on the live system")
    # Keep kernel messages off the console so they don't interleave with output.
    con.run("dmesg -n 1")


def send_answerfile(con: Console, text: str, dest: str = "/root/af") -> None:
    """Write ``text`` to ``dest`` on the guest and verify its sha256."""
    data = text.encode()
    b64 = base64.b64encode(data).decode()
    digest = hashlib.sha256(data).hexdigest()
    log(f"writing the answerfile to {dest} ({len(data)} bytes, chunked)...")
    # A long line over a serial console is fragile, so send short chunks.
    if con.run(f"rm -f {dest}.b64") != 0:
        raise InstallError("could not prepare the answerfile on the guest")
    for i in range(0, len(b64), 200):
        if con.run(f"echo '{b64[i:i + 200]}' >> {dest}.b64") != 0:
            raise InstallError("writing an answerfile chunk failed")
    if con.run(f"base64 -d {dest}.b64 > {dest} && rm -f {dest}.b64") != 0:
        raise InstallError("decoding the answerfile on the guest failed")
    # The hash appears only in sha256sum's output, never in the echoed command.
    con.send(f"sha256sum {dest}")
    con.expect(re.compile(re.escape(digest)), 15,
               "the answerfile's sha256 (the transfer was corrupted)")
    log("answerfile verified on the guest (sha256 matches)")


def run_setup_alpine(con: Console, disk_size: Callable[[], int],
                     timeout: float = 2400.0) -> None:
    log("running `setup-alpine -e -f /root/af` (progress below; full transcript "
        f"in {SERIAL_LOG})...")
    phases = PhaseTracker()
    checks = (
        InputPromptCheck(),
        phases,
        IdleCheck(600),
        DiskGrowthCheck(phases, disk_size),
    )
    rc = con.run("setup-alpine -e -f /root/af", timeout=timeout,
                 label="setup-alpine to finish", checks=checks)
    if rc != 0:
        raise InstallError(f"setup-alpine exited with status {rc}")
    if INSTALL_DONE not in con.last_output:
        raise InstallError(f"setup-alpine returned 0 but never printed {INSTALL_DONE!r} "
                           "(setup-disk did not install the system)")
    log("base install finished (setup-disk: installation is complete)")


def power_off(con: Console, name: str, timeout: float = 120.0) -> None:
    """Flush and power the live system off; wait for the domain to stop."""
    con.run("sync")
    con.send("poweroff")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if domstate(name) == "shut off":
            log("live system powered off")
            return
        time.sleep(1)
    log("live system did not power off in time; destroying the domain")
    virsh("destroy", name)


# --- libvirt helpers --------------------------------------------------------

def virsh(*args: str) -> str:
    """Run a virsh command (directly as root, else via the libvirt group)."""
    cmd = ["virsh", "-c", "qemu:///system", *args]
    if os.geteuid() != 0:
        cmd = ["sg", "libvirt", "-c", " ".join(shlex.quote(a) for a in cmd)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout + r.stderr


def domstate(name: str) -> str:
    """'running', 'shut off', ... or '' if the domain doesn't exist."""
    out = virsh("domstate", name).strip()
    first = out.splitlines()[0] if out else ""
    return "" if first.startswith("error") else first


def serial_pty_path(name: str, timeout: float = 30.0) -> str:
    """Return the serial pty path (from virsh dumpxml) once the VM has started."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = re.search(r"<serial[^>]*>\s*<source path='([^']+)'", virsh("dumpxml", name))
        if m:
            return m.group(1)
        time.sleep(0.5)
    die("timed out finding the serial pty path in the domain XML")


def connect_serial(name: str, timeout: float = 30.0):
    import serial
    path = serial_pty_path(name, timeout)
    log(f"attaching to serial pty {path}")
    return serial.Serial(path, timeout=0.2)


def disk_boot_xml(xml: str) -> str:
    """Drop the direct kernel boot from a domain XML and boot from disk."""
    xml = re.sub(r"\s*<kernel>[^<]*</kernel>", "", xml)
    xml = re.sub(r"\s*<initrd>[^<]*</initrd>", "", xml)
    xml = re.sub(r"\s*<cmdline>[^<]*</cmdline>", "", xml)
    if "<boot " not in xml:
        xml = re.sub(r"(<type [^>]*>hvm</type>)", r"\1\n    <boot dev='hd'/>", xml, count=1)
    return xml


def switch_to_disk_boot(name: str) -> None:
    """Redefine the (shut off) domain to boot from disk and start it, so the
    *installed* system — not the live ISO — comes up with sshd + our key."""
    if domstate(name) != "shut off":
        virsh("destroy", name)
    xml = disk_boot_xml(virsh("dumpxml", name))
    fd, path = tempfile.mkstemp(prefix=f"{name}-diskboot-", suffix=".xml")
    with os.fdopen(fd, "w") as f:
        f.write(xml)
    virsh("define", path)
    os.unlink(path)
    virsh("start", name)


def domain_ips(name: str) -> list[str]:
    return re.findall(r"(\d+\.\d+\.\d+\.\d+)", virsh("domifaddr", name))


def open_transcript(path: str):
    """Open the serial transcript (overwritten per run, readable by the user)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "wb")  # noqa: SIM115 - closed by main()
        os.chmod(path, 0o644)
        return f
    except OSError as e:
        log(f"WARNING: cannot write the serial transcript to {path}: {e}")
        return None


# --- Post-install verification ---------------------------------------------

VERIFY_CMD = "hostname; awk '$2==\"/\" {print $1, $3}' /proc/mounts; uname -r; apk --version"


def check_installed(output: str) -> str | None:
    """Check VERIFY_CMD's output; return a problem description or None."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if len(lines) < 2:
        return f"unexpected verification output: {output!r}"
    if lines[0] != EXPECTED_HOSTNAME:
        return f"hostname is {lines[0]!r}, expected {EXPECTED_HOSTNAME!r} (not the installed system?)"
    root = lines[1].split()
    if len(root) < 2 or root[1] in ("tmpfs", "rootfs", "squashfs"):
        return f"root filesystem is {lines[1]!r}: this is the live system, not the disk install"
    return None


def _ssh_client():
    """A paramiko client that accepts the host key of the VM we just built."""
    import paramiko

    class _AcceptLocalHostKey(paramiko.MissingHostKeyPolicy):
        """Accept the host key of the local VM we just provisioned (trusted)."""

        def missing_host_key(self, client, hostname, key):
            # Deliberate: a VM we created on localhost moments ago.
            return None

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_AcceptLocalHostKey())
    return client


def verify_ssh(name: str, timeout: float = 300.0) -> str:
    """Wait for the installed system's IP and verify it over SSH. Returns the IP."""
    deadline = time.time() + timeout
    last: object = "no IP from `virsh domifaddr` yet"
    while time.time() < deadline:
        for ip in domain_ips(name):
            client = _ssh_client()
            try:
                client.connect(ip, username="root", key_filename=SSH_KEY, timeout=10,
                               allow_agent=False, look_for_keys=False)
                _, stdout, _ = client.exec_command(VERIFY_CMD, timeout=30)
                out = stdout.read().decode()
            except Exception as e:  # noqa: BLE001 - retry until sshd is ready
                last = f"{ip}: {e}"
                continue
            finally:
                client.close()
            problem = check_installed(out)
            if problem:
                die(f"SSH to {ip} works, but {problem}")
            log("=== installed system reports ===")
            print(out, flush=True)
            return ip
        time.sleep(5)
    die(f"could not verify the installed system over SSH: {last}")


def poweroff_installed(name: str, ip: str, timeout: float = 90.0) -> None:
    """Power the installed system off over SSH so the disk is left at rest.

    (`virsh shutdown` relies on ACPI handling in the guest; this doesn't.)
    """
    client = _ssh_client()
    try:
        client.connect(ip, username="root", key_filename=SSH_KEY, timeout=10,
                       allow_agent=False, look_for_keys=False)
        client.exec_command("sync; poweroff")
    except Exception as e:  # noqa: BLE001 - fall through to the state poll
        log(f"WARNING: poweroff over SSH failed: {e}")
    finally:
        client.close()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if domstate(name) == "shut off":
            log("installed system powered off (disk at rest)")
            return
        time.sleep(1)
    log("installed system did not power off in time; provision.sh will force it")


# --- main ---------------------------------------------------------------------

def report_failure(con: Console, err: Exception) -> None:
    print(f"[install] ERROR: {err}", file=sys.stderr, flush=True)
    print("[install] last console lines:", file=sys.stderr)
    for line in con.tail().splitlines():
        print(f"  | {line}", file=sys.stderr)
    print(f"[install] full serial transcript: {SERIAL_LOG}", file=sys.stderr, flush=True)


def main() -> int:
    for mod in ("serial", "paramiko"):
        try:
            __import__(mod)
        except ImportError:
            die(f"missing python module '{mod}' (apt: python3-serial / python3-paramiko)")
    for path in (ANSWERFILE, SSH_KEY, SSH_PUBKEY):
        if not os.path.exists(path):
            die(f"not found: {path}")
    with open(ANSWERFILE) as f, open(SSH_PUBKEY) as k:
        try:
            answers = render_answerfile(f.read(), k.read())
        except InstallError as e:
            die(str(e))

    # 1. Start the live VM fresh. A running domain here is a leftover from an
    #    aborted run, and its console state is unknown.
    if domstate(VM_NAME) == "running":
        log("domain already running (aborted earlier run?) — restarting it")
        virsh("destroy", VM_NAME)
    log("starting the VM (live ISO, direct kernel boot)...")
    out = virsh("start", VM_NAME)
    if domstate(VM_NAME) != "running":
        die(f"virsh start failed: {out.strip()}")
    ser = connect_serial(VM_NAME)
    transcript = open_transcript(SERIAL_LOG)
    con = Console(ser, transcript)
    try:
        login_live(con)
        send_answerfile(con, answers)
        run_setup_alpine(con, lambda: allocated_bytes(DISK_PATH))
        power_off(con, VM_NAME)
    except InstallError as e:
        report_failure(con, e)
        if KEEP_ON_FAILURE:
            print(f"[install] RB_KEEP_VM=1: leaving the live VM running. Inspect it with:\n"
                  f"  virsh -c qemu:///system console {VM_NAME}", file=sys.stderr)
        else:
            virsh("destroy", VM_NAME)
            print("[install] destroyed the live VM (pass --keep-on-failure to "
                  "provision.sh to leave it running).", file=sys.stderr)
        return 1
    finally:
        ser.close()
        if transcript is not None:
            transcript.close()

    # 2. Boot the installed system and verify it over SSH.
    log("switching the VM to boot from disk (the installed system)...")
    switch_to_disk_boot(VM_NAME)
    ip = verify_ssh(VM_NAME)
    log(f"installed system verified at {ip}")
    poweroff_installed(VM_NAME, ip)
    log("zero-touch install complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
