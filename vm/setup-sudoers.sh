#!/usr/bin/env bash
#
# Rabbit Ears — one-time host setup: passwordless sudo for the two VM scripts
# that need root, so they (and agents) can run them without a password prompt:
#
#   vm/provision.sh   libvirt domain + disk under /var/lib (self-elevates)
#   vm/make-usb.sh    loop-mount the mock USB stick image   (self-elevates)
#
# Rules are written for THIS checkout's absolute path and the invoking user,
# so it works for any clone location / username. Run it once per clone, and
# again if you move the repo.
#
# Usage:
#   ./vm/setup-sudoers.sh          # asks for your sudo password once
#   ./vm/setup-sudoers.sh --remove # delete the rules
#
# Idempotent. Validates with `visudo -c` before installing, so a bad rule can
# never break sudo.
#
# Security note: a NOPASSWD rule on a script you can edit is equivalent to
# passwordless root for your user. Only use this on your own dev machine.

set -euo pipefail

RULE_FILE="/etc/sudoers.d/rabbitears"
LEGACY_RULE_FILE="/etc/sudoers.d/rabbitears-export-disk"   # retired disk-copy workaround
SCRIPTS=(vm/provision.sh vm/make-usb.sh)

log()  { printf '\033[1;32m[sudoers]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[sudoers]\033[0m %s\n' "$*" >&2; exit 1; }

# Self-elevate (prompts for the password this one time).
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

USER_NAME="${SUDO_USER:-}"
[ -n "$USER_NAME" ] && [ "$USER_NAME" != "root" ] \
  || die "run this as your normal user (./vm/setup-sudoers.sh), not as root"

# Physical path (no symlinks) — what sudo will see when the script re-execs.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# sudoers needs escaping for spaces and ,:=\ — refuse rather than get it wrong.
case "$REPO_ROOT" in
  *[!A-Za-z0-9._/-]*) die "repo path has characters sudoers would need escaped: ${REPO_ROOT}" ;;
esac

if [ "${1:-}" = "--remove" ]; then
  rm -f "$RULE_FILE"
  log "Removed ${RULE_FILE}."
  exit 0
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
{
  echo "# Managed by ${REPO_ROOT}/vm/setup-sudoers.sh — do not edit by hand."
  for s in "${SCRIPTS[@]}"; do
    printf '%s ALL=(root) NOPASSWD: %s\n' "$USER_NAME" "${REPO_ROOT}/${s}"
  done
} > "$tmp"
visudo -cqf "$tmp" || die "generated rules failed visudo validation (nothing installed)"

if [ -f "$RULE_FILE" ] && cmp -s "$tmp" "$RULE_FILE"; then
  log "${RULE_FILE} already up to date."
else
  install -m 0440 -o root -g root "$tmp" "$RULE_FILE"
  log "Installed ${RULE_FILE}."
fi

if [ -f "$LEGACY_RULE_FILE" ]; then
  rm -f "$LEGACY_RULE_FILE"
  log "Removed obsolete ${LEGACY_RULE_FILE}."
fi

for s in "${SCRIPTS[@]}"; do
  [ -e "${REPO_ROOT}/${s}" ] || log "note: ${s} doesn't exist yet; its rule takes effect once it does."
done

cat "$RULE_FILE"
