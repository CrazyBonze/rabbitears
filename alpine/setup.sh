#!/bin/sh
#
# Rabbit Ears — idempotent box setup.
#
# Reads alpine/packages.txt, installs the packages, lays down the system/
# config, registers the OpenRC services, and includes /opt/rabbitears in lbu.
# Every step is idempotent, so it's safe to re-run.
#
# POSIX sh (no bash): the box is a minimal Alpine install with only busybox
# ash, so this must not depend on bash.
#
# Run as root on the box (or the VM):
#     sudo ./alpine/setup.sh
#
# NOTE: this deliberately does NOT run `lbu commit` — that's a release action
#       (see alpine/deploy.sh). setup.sh only lays down the state.
#
set -eu

# --- Locate the repo root (this script lives in <repo>/alpine/) ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

log() { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "setup.sh must be run as root (try: sudo ./alpine/setup.sh)" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Install packages (from the authoritative list).
# ---------------------------------------------------------------------------
PKGS_FILE="${REPO_ROOT}/alpine/packages.txt"
[ -f "$PKGS_FILE" ] || { echo "missing ${PKGS_FILE}" >&2; exit 1; }

log "Installing packages from ${PKGS_FILE} ..."
# Strip full-line + inline comments and blank lines; the rest become apk add
# arguments (one package per token).
PKGS="$(grep -vE '^[[:space:]]*(#|$)' "$PKGS_FILE" \
  | sed 's/[[:space:]]*#.*$//' \
  | tr -s '[:space:]' '\n' \
  | grep -vE '^[[:space:]]*$')"
[ -n "$PKGS" ] || { echo "no packages found in ${PKGS_FILE}" >&2; exit 1; }
log "Packages: $(echo "$PKGS" | tr '\n' ' ')"
apk add --no-cache $PKGS

# ---------------------------------------------------------------------------
# 2. App config dir.
# ---------------------------------------------------------------------------
log "Creating /etc/rabbitears/ ..."
install -d -m 0755 /etc/rabbitears
# TODO: drop a default config.toml here (EPOCH, volume cap, OSD settings) once
#       src/rabbitears/config.py defines the schema. Empty for now.

# ---------------------------------------------------------------------------
# 3. Copy system/ files into place.
# ---------------------------------------------------------------------------
log "Installing system/ config ..."
# mpv config -> /etc/mpv/mpv.conf
install -d -m 0755 /etc/mpv
install -m 0644 "${REPO_ROOT}/system/mpv/mpv.conf" /etc/mpv/mpv.conf

# OpenRC init script -> /etc/init.d/rabbitears
install -m 0755 "${REPO_ROOT}/system/openrc/rabbitears" /etc/init.d/rabbitears

# TODO: system/acpid/       — power-button override (KEY_POWER -> standby, not off).
# TODO: system/fstab.snippet — LABEL=RABBITEARS exFAT mount at /media/rabbitears.
# TODO: system/fonts/       — bundled pixel font + license (replaces font-terminus).

# ---------------------------------------------------------------------------
# 4. App code.
# ---------------------------------------------------------------------------
# The app lives at /opt/rabbitears (lbu include). No app code yet, so just
# create the dir; alpine/deploy.sh will rsync src/ here later.
log "Preparing /opt/rabbitears ..."
install -d -m 0755 /opt/rabbitears
# TODO: rsync src/rabbitears -> /opt/rabbitears once the app exists (deploy.sh).

# ---------------------------------------------------------------------------
# 5. Register OpenRC services.
# ---------------------------------------------------------------------------
log "Registering services ..."
rc-update add sshd default        2>/dev/null || true
rc-update add chronyd default     2>/dev/null || true
rc-update add rabbitears default  2>/dev/null || true
# TODO: acpid — enable once the power-button override is in place.

# ---------------------------------------------------------------------------
# 6. lbu: include the app so it persists in the overlay.
# ---------------------------------------------------------------------------
log "lbu include /opt/rabbitears ..."
lbu include /opt/rabbitears

log "setup complete."
