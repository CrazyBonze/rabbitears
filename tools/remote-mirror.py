#!/usr/bin/env python3
"""Host-side remote mirror (dev tool).

Read the Adomax remote dongle (``0627:697d``) on the *host* — where it is
plugged in directly and the full button map works — show which button is
pressed on a scaled copy of ``docs/remote.jpg``, and relay every event to the
running VM's button FIFO over SSH.

Why this exists: QEMU's ``usb-host`` passthrough of the composite dongle only
delivers one HID interface per boot (keyboard *or* consumer-control, flipping
on each restart), so the full remote can't be used inside the VM directly.
Instead, the host owns the dongle and this tool mirrors it into the VM's normal
input path — the same ``/run/rabbitears/buttons`` FIFO the evdev daemon writes —
so press/release, hold-to-repeat, and the standby gate all behave exactly like
the physical remote on a real 3040.

Run the VM *without* the dongle passthrough (``uv run task vm-run-usb-vnc``),
then ``uv run task remote-mirror``.

Host-only. Depends on ``pygame-ce`` and ``evdev`` (dev dependency group).
Reuses the box-side button map (``rabbitears.evdevd.event_press_release``) so
the wire format and button->name mapping stay in one place.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
import time
from pathlib import Path

import pygame
from evdev import InputDevice, list_devices

from rabbitears.evdevd import VENDOR, event_press_release

DEFAULT_IMAGE = Path("docs/remote.jpg")
DEFAULT_KEY = Path("vm/keys/rabbitears-vm")
FIFO = "/run/rabbitears/buttons"
TARGET_HEIGHT = 760      # scaled remote-photo height (px)
STATUS_STRIP = 24        # px under the photo for the relay status line
FLASH_SECONDS = 0.35     # how long a quick tap stays highlighted

# Button centre points as fractions of the photo (docs/remote.jpg, 1285x2016).
BUTTON_POS: dict[str, tuple[float, float]] = {
    "power": (0.28, 0.17),
    "mute": (0.65, 0.17),
    "sun": (0.47, 0.27),
    "screen-arrow": (0.66, 0.27),
    "channel-up": (0.47, 0.36),
    "channel-down": (0.47, 0.58),
    "arrow-left": (0.30, 0.47),
    "arrow-right": (0.65, 0.47),
    "ok": (0.47, 0.47),
    "home": (0.30, 0.69),
    "back": (0.47, 0.69),
    "menu": (0.65, 0.69),
    "vol-up": (0.66, 0.80),
    "vol-down": (0.28, 0.80),
}

# Click radius (px) around a button centre that counts as "pressed".
CLICK_RADIUS = 30.0


def button_at(pos: tuple[int, int], width: int, height: int) -> str | None:
    """The button whose centre is nearest to ``pos``, or None if none is within
    ``CLICK_RADIUS`` px (so clicking the remote photo sends that button)."""
    x, y = pos
    best: str | None = None
    best_d = CLICK_RADIUS
    for name, (fx, fy) in BUTTON_POS.items():
        cx, cy = fx * width, fy * height
        d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        if d <= best_d:
            best, best_d = name, d
    return best


def build_ssh_argv(key: Path, port: int, host: str) -> list[str]:
    """The persistent relay command: pipe stdin into the VM's button FIFO."""
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-i", str(key),
        "-p", str(port),
        f"root@{host}",
        f"cat > {FIFO}",
    ]


class Relay:
    """A persistent SSH pipe into the VM's button FIFO (respawning on failure)."""

    def __init__(self, argv: list[str]) -> None:
        self._argv = argv
        self._proc: subprocess.Popen | None = None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def write(self, line: str) -> bool:
        if not self.alive():
            try:
                self._proc = subprocess.Popen(
                    self._argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return False
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        try:
            proc.stdin.write((line + "\n").encode())
            proc.stdin.flush()
            return True
        except OSError:
            self._proc = None
            return False

    def close(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(OSError):
                self._proc.stdin.close()
                self._proc.terminate()
            self._proc = None


def _open_dongle(grab: bool = True) -> list[InputDevice]:
    """Find every input interface of the remote dongle; grab them unless told
    not to (grabbing all four can starve the HID report dispatch, so ``grab``
    is disabled to test that)."""
    devices: list[InputDevice] = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if dev.info.vendor == VENDOR:
            if grab:
                with contextlib.suppress(OSError):
                    dev.grab()
            devices.append(dev)
        else:
            with contextlib.suppress(OSError):
                dev.close()
    return devices


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", default=str(DEFAULT_IMAGE), help="remote photo (default docs/remote.jpg)")
    ap.add_argument("--key", default=str(DEFAULT_KEY), help="VM SSH private key")
    ap.add_argument("--host", default="127.0.0.1", help="VM SSH host")
    ap.add_argument("--port", type=int, default=2222, help="VM SSH port")
    ap.add_argument("--no-relay", action="store_true", help="only show buttons; do not relay to the VM")
    ap.add_argument("--no-grab", action="store_true", help="do not grab() the dongle (read shared with the host)")
    args = ap.parse_args(argv)

    devices = _open_dongle(grab=not args.no_grab)
    if not devices:
        print(
            f"remote-mirror: no dongle found (vendor 0x{VENDOR:04X}). "
            "Is it plugged in, and is the VM running *without* --remote-usb?",
            file=sys.stderr,
        )
        return 1

    relay = None if args.no_relay else Relay(
        build_ssh_argv(Path(args.key), args.port, args.host)
    )

    pygame.init()
    try:
        raw = pygame.image.load(args.image)
    except pygame.error as e:
        print(f"remote-mirror: cannot load {args.image}: {e}", file=sys.stderr)
        return 1
    scale = TARGET_HEIGHT / raw.get_height()
    width, height = round(raw.get_width() * scale), TARGET_HEIGHT
    screen = pygame.display.set_mode((width, height + STATUS_STRIP))
    # convert() needs a display surface, so it must follow set_mode().
    photo = pygame.transform.smoothscale(raw, (width, height)).convert()
    pygame.display.set_caption("rabbitears remote mirror (Esc to quit)")
    font = pygame.font.SysFont(None, 22)

    held: set[str] = set()
    pressed_at: dict[str, float] = {}
    click_held: str | None = None  # button currently held by a mouse click

    try:
        clock = pygame.time.Clock()
        while True:
            now = time.monotonic()
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return 0
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                    return 0
                if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    name = button_at(ev.pos, width, height)
                    if name is not None:
                        click_held = name
                        held.add(name)
                        pressed_at[name] = now
                        if relay is not None:
                            relay.write(name)
                elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                    if click_held is not None:
                        held.discard(click_held)
                        if relay is not None:
                            relay.write(click_held + "~")
                        click_held = None

            for dev in devices:
                # evdev >= 2.0 raises BlockingIOError when a non-blocking fd
                # has no pending events (1.x returned an empty list).
                try:
                    for e in dev.read():
                        pr = event_press_release(e.type, e.code, e.value)
                        if pr is None:
                            continue
                        name, is_press = pr
                        if is_press:
                            held.add(name)
                            pressed_at[name] = now
                            if relay is not None:
                                relay.write(name)
                        else:
                            held.discard(name)
                            if relay is not None:
                                relay.write(name + "~")
                except BlockingIOError:
                    pass

            screen.fill((22, 22, 26))
            screen.blit(photo, (0, 0))
            for name, (fx, fy) in BUTTON_POS.items():
                cx, cy = int(fx * width), int(fy * height)
                if name in held:
                    pygame.draw.circle(screen, (255, 90, 90), (cx, cy), 26)
                    pygame.draw.circle(screen, (255, 255, 255), (cx, cy), 26, 3)
                elif name in pressed_at and now - pressed_at[name] < FLASH_SECONDS:
                    pygame.draw.circle(screen, (255, 200, 70), (cx, cy), 24, 3)

            if relay is not None:
                status = "relay: connected" if relay.alive() else "relay: DISCONNECTED"
            else:
                status = "relay: off (--no-relay)"
            screen.blit(font.render(status, True, (200, 200, 200)), (8, height + 4))
            hint = font.render("click a button to send  ·  Esc quits", True, (140, 140, 140))
            screen.blit(hint, (width - hint.get_width() - 8, height + 4))

            pygame.display.flip()
            clock.tick(60)
    finally:
        for dev in devices:
            with contextlib.suppress(OSError):
                dev.close()
        if relay is not None:
            relay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
