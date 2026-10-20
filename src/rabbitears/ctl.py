"""Tiny client for the local control socket: ``python3 -m rabbitears.ctl <request>``.

Prints the reply and exits 0 on ``ok``/``status``, 1 on error or no socket.
Intended for deliberate test actions over SSH (AGENTS.md), e.g.

    PYTHONPATH=/opt python3 -m rabbitears.ctl status
"""

from __future__ import annotations

import socket
import sys

from .config import load_config


def main(argv: list[str] | None = None, socket_path: str | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: ctl <command|status>", file=sys.stderr)
        return 2
    request = args[0]
    if socket_path is None:
        socket_path = str(load_config().control_socket)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(socket_path)
            s.sendall((request + "\n").encode("utf-8"))
            reply = s.recv(4096).decode("utf-8", "replace").strip()
    except OSError as e:
        print(f"ctl: {e}", file=sys.stderr)
        return 1
    print(reply)
    # "ok" and a status JSON line are success; anything else (error: …) fails.
    return 0 if (reply == "ok" or reply.startswith("{")) else 1


if __name__ == "__main__":
    sys.exit(main())
