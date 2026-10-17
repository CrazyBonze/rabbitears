#!/usr/bin/env bash
#
# Rabbit Ears — export a read-only copy of the installed VM disk.
#
# Single-purpose helper for the QMP screendump validation flow: copies the
# libvirt-managed qcow2 (root:libvirt-qemu, mode 660) to a user-writable
# path so an unprivileged `vmctl.py run` can boot a *copy* without touching
# the original. The source disk is never modified, renamed, or re-permissioned.
#
# Intended to be run via a narrow sudoers rule, e.g.:
#
#   michael ALL=(ALL) NOPASSWD: /home/michael/rabbitears/vm/export-disk.sh
#
# Usage:
#   sudo vm/export-disk.sh [DEST]
#   (default DEST: /tmp/rabbitears-vm-test.qcow2)

set -euo pipefail

SRC="/var/lib/libvirt/images/rabbitears-vm.qcow2"
DEST="${1:-/tmp/rabbitears-vm-test.qcow2}"

[ -f "$SRC" ] || { echo "source disk not found: $SRC" >&2; exit 1; }
[ -e "$DEST" ] && { echo "destination exists, refusing to overwrite: $DEST" >&2; exit 1; }

cp "$SRC" "$DEST"

# Hand the copy to the invoking (unprivileged) user.
REAL_USER="${SUDO_USER:-$(id -un)}"
chown "$REAL_USER" "$DEST"
chmod 0600 "$DEST"

echo "Copied $SRC -> $DEST (owner: $REAL_USER)"
