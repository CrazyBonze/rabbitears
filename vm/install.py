#!/usr/bin/env python3
"""Rabbit Ears — zero-touch VM install driver.

Drives the Alpine base install over the VM's serial console (pyserial), then
hands off to paramiko (SSH) once the installed system is up. No human in the
loop.

Prereqs (set up by vm/provision.sh before this runs):
  * the VM is DEFINED (not started) with direct kernel boot (console=ttyS0),
    a TCP serial on 127.0.0.1:$RB_SERIAL_PORT, the Alpine ISO as cdrom, and
    an 8GB IDE disk at /dev/sda;
  * pyserial + paramiko are importable.

Flow:
  1. connect to the TCP serial (before the VM boots, so we catch the prompt);
  2. start the VM;
  3. wait for the live system's `login:`, log in as `root`;
  4. write the answerfile to /root/af (base64 over the wire) and run
     `setup-alpine -f /root/af`;
  5. reboot into the installed system;
  6. poll for the VM's IP, then verify SSH as root (key auth, no password).
"""
import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import NoReturn

VM_NAME = os.environ.get("RB_VM_NAME", "rabbitears-vm")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWERFILE = os.path.join(REPO_ROOT, "alpine", "answerfile")
SSH_KEY = os.path.join(REPO_ROOT, "vm", "keys", "rabbitears-vm")

PROMPT = re.compile(r"~# ")  # root shell prompt in /root (e.g. "localhost:~# ")


def log(msg: str) -> None:
    print(f"[install] {msg}", flush=True)


def die(msg: str) -> NoReturn:
    print(f"[install] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def virsh(*args: str) -> str:
    """Run a virsh command as the libvirt group (no sudo needed)."""
    cmd = " ".join(["virsh", "-c", "qemu:///system", *args])
    r = subprocess.run(["sg", "libvirt", "-c", cmd], capture_output=True, text=True)
    return r.stdout + r.stderr


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


def read_until(ser, pattern: re.Pattern, timeout: float, label: str) -> str:
    """Read the serial until `pattern` matches the accumulated buffer."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = ser.in_waiting
        if n:
            buf += ser.read(n).decode("utf-8", "replace")
            if pattern.search(buf):
                return buf
        else:
            time.sleep(0.05)
    die(f"timeout waiting for {label!r}; last 400 chars: {buf[-400:]!r}")


def try_read_until(ser, pattern: re.Pattern, timeout: float) -> bool:
    """Like read_until but returns False on timeout instead of dying."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = ser.in_waiting
        if n:
            buf += ser.read(n).decode("utf-8", "replace")
            if pattern.search(buf):
                return True
        else:
            time.sleep(0.05)
    return False


def send(ser, line: str) -> None:
    ser.write((line + "\r\n").encode())
    ser.flush()
    time.sleep(0.3)  # let the console absorb the line


def switch_to_disk_boot(name: str) -> None:
    """Stop the VM, redefine it to boot from disk (drop the direct kernel boot),
    and start it. This makes the *installed* system — not the live ISO — boot,
    so sshd + our root key come up."""
    virsh("destroy", name)
    xml = virsh("dumpxml", name)
    xml = re.sub(r"\s*<kernel>[^<]*</kernel>", "", xml)
    xml = re.sub(r"\s*<initrd>[^<]*</initrd>", "", xml)
    xml = re.sub(r"\s*<cmdline>[^<]*</cmdline>", "", xml)
    if "<boot " not in xml:
        xml = xml.replace(
            "<type arch='x86_64' machine='pc'>hvm</type>",
            "<type arch='x86_64' machine='pc'>hvm</type>\n    <boot dev='hd'/>",
            1,
        )
    fd, path = tempfile.mkstemp(prefix=f"{name}-diskboot-", suffix=".xml")
    with os.fdopen(fd, "w") as f:
        f.write(xml)
    virsh("define", path)
    os.unlink(path)
    virsh("start", name)


def main() -> None:
    for mod in ("serial", "paramiko"):
        try:
            __import__(mod)
        except ImportError:
            die(f"missing python module '{mod}' — install it (see vm/README)")

    if not os.path.exists(ANSWERFILE):
        die(f"answerfile not found: {ANSWERFILE}")
    if not os.path.exists(SSH_KEY):
        die(f"ssh key not found: {SSH_KEY}")

    # 1. Start the VM if it isn't already running (idempotent re-runs).
    if VM_NAME not in virsh("list", "--name"):
        log("starting VM...")
        virsh("start", VM_NAME)
    else:
        log("VM already running")

    # 2. Find the serial pty and attach. We connect within a couple seconds of
    #    boot — well before the login prompt appears (~10-30s in) — so we catch it.
    ser = connect_serial(VM_NAME)

    # 3. Log in to the live system.
    log("waiting for the live system's login prompt...")
    read_until(ser, re.compile(r"login:"), timeout=180, label="login prompt")
    send(ser, "root")
    read_until(ser, PROMPT, timeout=60, label="root shell prompt")
    log("logged in as root on the live system")

    # 4. Write the answerfile and run the unattended install.
    #    A single long line over a serial console is fragile (it can split into
    #    `>` continuation prompts), so write the base64 in small chunks.
    with open(ANSWERFILE, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    log("writing answerfile to /root/af (chunked) ...")
    send(ser, "rm -f /root/af.b64")
    read_until(ser, PROMPT, timeout=15, label="prompt before writing answerfile")
    for i in range(0, len(b64), 200):
        send(ser, f"echo '{b64[i:i + 200]}' >> /root/af.b64")
        read_until(ser, PROMPT, timeout=15, label="prompt after answerfile chunk")
    send(ser, "base64 -d /root/af.b64 > /root/af && rm -f /root/af.b64 && echo AF_OK")
    read_until(ser, re.compile(r"AF_OK"), timeout=30, label="answerfile written")

    log("running `setup-alpine -f /root/af` (several minutes; the disk apk install is slow over NAT)...")
    send(ser, "setup-alpine -f /root/af")
    if try_read_until(ser, PROMPT, timeout=1800):
        log("base install finished")
    else:
        log("setup-alpine did not return a prompt (likely rebooted); continuing")
    ser.close()

    # 5. Switch the domain to boot from disk, so the *installed* system comes up
    #    (the direct-kernel-boot config would otherwise re-boot the live ISO).
    log("switching the VM to boot from disk (the installed system)...")
    switch_to_disk_boot(VM_NAME)

    # 6. Wait for the installed system's IP, then verify SSH.
    ip = wait_for_ip(VM_NAME, timeout=300)
    log(f"installed system is up at {ip}")
    verify_ssh(ip)
    log("zero-touch install complete.")


def wait_for_ip(name: str, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", virsh("domifaddr", name))
        if m:
            return m.group(1)
        time.sleep(5)
    die("timed out waiting for the VM's IP (is the answerfile's network config ok?)")


def verify_ssh(ip: str) -> None:
    import paramiko

    class _AcceptLocalHostKey(paramiko.MissingHostKeyPolicy):
        """Accept the host key of the local VM we just provisioned (trusted)."""

        def missing_host_key(self, client, hostname, key):
            # Deliberate: a VM we created on localhost moments ago.
            return None

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_AcceptLocalHostKey())
    deadline = time.time() + 120
    last = None
    while time.time() < deadline:
        try:
            client.connect(ip, username="root", key_filename=SSH_KEY, timeout=10,
                           allow_agent=False, look_for_keys=False)
            break
        except Exception as e:  # noqa: BLE001 - retry until sshd is ready
            last = e
            time.sleep(5)
    else:
        die(f"SSH to {ip} failed: {last}")
    _, stdout, _ = client.exec_command("echo ok; hostname; uname -a; apk version")
    log("=== installed system reports ===")
    print(stdout.read().decode(), flush=True)
    client.close()


if __name__ == "__main__":
    main()
