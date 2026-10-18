#!/usr/bin/env bash
#
# TEMPORARY setup helper — safe to delete once the QMP screendump validation
# is done. Installs a narrow sudoers rule allowing ONLY
# /home/michael/rabbitears/vm/export-disk.sh to run passwordless, so the
# unprivileged QMP-screendump flow can boot a *copy* of the VM disk.
#
# Usage:
#   sudo vm/setup-export-sudoers.sh
#
# Idempotent: re-running just rewrites the same rule file.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/vm/export-disk.sh"
RULE_FILE="/etc/sudoers.d/rabbitears-export-disk"
USER_NAME="${SUDO_USER:-$(id -un)}"

[ "$(id -u)" -eq 0 ] || { echo "run as: sudo $0" >&2; exit 1; }
[ -f "$SCRIPT" ] || { echo "missing $SCRIPT" >&2; exit 1; }

# Make the export script executable.
chmod 0755 "$SCRIPT"

# Write the rule to a temp file and validate it BEFORE touching /etc/sudoers.d,
# so a syntax error can never break sudo.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
printf '%s ALL=(ALL) NOPASSWD: %s\n' "$USER_NAME" "$SCRIPT" > "$tmp"
visudo -cf "$tmp"
install -m 0440 -o root -g root "$tmp" "$RULE_FILE"

echo "Installed rule in $RULE_FILE:"
cat "$RULE_FILE"
echo
echo "Test it with:  sudo ${SCRIPT} /tmp/rabbitears-vm-test.qcow2"
