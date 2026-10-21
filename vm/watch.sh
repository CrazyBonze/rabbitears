#!/usr/bin/env bash
#
# Open the Remmina viewer on the running Rabbit Ears VM's VNC display.
#
# The VM is booted with `uv run task vm-run-usb-vnc-remote` (VNC on
# 127.0.0.1:5901). This launches the repo's committed Remmina profile against
# it and returns immediately (Remmina is single-instance: if it's already
# running, this just tells it to open the connection).
#
# Host-side only; no target changes.
set -euo pipefail

PROFILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/remmina/rabbitears-vm.remmina"

if ! command -v remmina >/dev/null 2>&1; then
  echo "error: remmina not installed (apt: remmina). Alternative:" >&2
  echo "  vinagre vnc://127.0.0.1:5901" >&2
  exit 1
fi

if command -v ss >/dev/null 2>&1 && ! ss -ltn 2>/dev/null | grep -q ':5901'; then
  echo "note: nothing listening on 127.0.0.1:5901 — boot the VM first:" >&2
  echo "  uv run task vm-run-usb-vnc-remote" >&2
fi

nohup remmina -c "$PROFILE" >/dev/null 2>&1 &
echo "Remmina → Rabbit Ears VM (VNC 127.0.0.1:5901)."
echo "Drive it with the physical remote; stop the VM with Ctrl+C in its terminal (or 'uv run task vm-stop')."
