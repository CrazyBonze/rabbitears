#!/usr/bin/env python3
"""e2e remote test double: writes button lines to the app's button FIFO.

The VM has no physical remote, so the e2e drives the input path with this
script (deployed to the VM and run over SSH). It writes the same wire format the
evdev daemon does (SCOPE §4.4): one line per button event — a press is the button
name, a release is the name with a trailing "~".

    e2e-remote <button>            write a tap: a press, then a release 50 ms
                                   later (like a real remote; the app's
                                   KeyRepeater repeats a held channel key until
                                   its release arrives, so a bare press would
                                   repeat forever and interfere with later checks)
    e2e-remote hold <button> <ms>  write a press, wait <ms>, then a release
                                   (exercises the app's autofire)

The FIFO is opened O_WRONLY|O_NONBLOCK, so if the app (the reader) is dead the
open fails immediately and this exits non-zero — the e2e can then tell the app
isn't there. Stdlib only (runs on the box/VM's system python3).
"""

import os
import sys
import time

FIFO = "/run/rabbitears/buttons"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: e2e-remote <button> | hold <button> <ms>", file=sys.stderr)
        return 2
    if argv[0] == "hold":
        if len(argv) < 3:
            print("usage: e2e-remote hold <button> <ms>", file=sys.stderr)
            return 2
        button, ms = argv[1], int(argv[2])
    else:
        # A tap: press + release 50 ms apart (see the usage above).
        button, ms = argv[0], 50

    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        print(f"e2e-remote: cannot open {FIFO}: {e}", file=sys.stderr)
        return 1
    try:
        os.write(fd, (button + "\n").encode())
        if ms > 0:
            time.sleep(ms / 1000.0)
            os.write(fd, (button + "~\n").encode())
    except OSError as e:
        print(f"e2e-remote: write to {FIFO} failed: {e}", file=sys.stderr)
        return 1
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
