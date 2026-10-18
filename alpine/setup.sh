#!/bin/sh
#
# Rabbit Ears — idempotent box setup.
#
# Reads alpine/packages.txt, installs the packages, lays down the system/
# config, registers the OpenRC services, and includes managed paths in lbu.
# Every step is idempotent, so it's safe to re-run. A second run with no
# repo changes prints "0 changed".
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
warn() { printf '\033[1;33m[setup] WARNING:\033[0m %s\n' "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
  echo "setup.sh must be run as root (try: sudo ./alpine/setup.sh)" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Change-aware install helper.
# Writes DEST only when it is missing or its content/mode differs from SRC.
# Increments CHANGED when a write occurs. Records the file in the manifest.
# ---------------------------------------------------------------------------
CHANGED=0
MANIFEST_TMP="$(mktemp)"
trap 'rm -f "$MANIFEST_TMP"' EXIT

# manifest_entry SHA256 TARGET_PATH REPO_RELATIVE_SOURCE
manifest_entry() {
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$MANIFEST_TMP"
}

# Strip leading zeros from an octal mode string so "0644" == "644".
_norm_mode() {
  printf '%s' "$1" | sed 's/^0*//'
}

install_file() {
  # install_file SRC DEST MODE
  _src="$1" _dest="$2" _mode="$3"
  _repo_rel="${_src#"$REPO_ROOT"/}"
  _mode_norm="$(_norm_mode "$_mode")"

  if [ -f "$_dest" ]; then
    # Compare content and mode (normalise both sides).
    _dest_mode="$(stat -c '%a' "$_dest")"
    if cmp -s "$_src" "$_dest" && [ "$_dest_mode" = "$_mode_norm" ]; then
      # Already identical — still record in manifest.
      _sha="$(sha256sum "$_dest" | cut -d' ' -f1)"
      manifest_entry "$_sha" "$_dest" "$_repo_rel"
      return 0
    fi
  fi

  install -d -m 0755 "$(dirname "$_dest")"
  install -m "$_mode" "$_src" "$_dest"
  CHANGED=$((CHANGED + 1))
  log "installed: $_dest"
  _sha="$(sha256sum "$_dest" | cut -d' ' -f1)"
  manifest_entry "$_sha" "$_dest" "$_repo_rel"
}

# ---------------------------------------------------------------------------
# 1. Install packages (from the authoritative list).
# ---------------------------------------------------------------------------
PKGS_FILE="${REPO_ROOT}/alpine/packages.txt"
[ -f "$PKGS_FILE" ] || { echo "missing ${PKGS_FILE}" >&2; exit 1; }

log "Installing packages from ${PKGS_FILE} ..."
PKGS="$(grep -vE '^[[:space:]]*(#|$)' "$PKGS_FILE" \
  | sed 's/[[:space:]]*#.*$//' \
  | tr -s '[:space:]' '\n' \
  | grep -vE '^[[:space:]]*$')"
[ -n "$PKGS" ] || { echo "no packages found in ${PKGS_FILE}" >&2; exit 1; }
log "Packages: $(echo "$PKGS" | tr '\n' ' ')"
# No --no-cache: on the diskless box the apk cache on eMMC is what allows
# offline package reinstall at boot (SCOPE §3).
apk add $PKGS

# ---------------------------------------------------------------------------
# 2. App config dir.
# ---------------------------------------------------------------------------
log "Creating /etc/rabbitears/ ..."
install -d -m 0755 /etc/rabbitears

# ---------------------------------------------------------------------------
# 3. Copy system/ files into place.
# ---------------------------------------------------------------------------
log "Installing system/ config ..."
install_file "${REPO_ROOT}/system/mpv/mpv.conf" /etc/mpv/mpv.conf 0644
install_file "${REPO_ROOT}/system/openrc/rabbitears" /etc/init.d/rabbitears 0755

# TODO: system/acpid/       — power-button override (KEY_POWER -> standby, not off).
# TODO: system/fstab.snippet — LABEL=RABBITEARS exFAT mount at /media/rabbitears.
# TODO: system/fonts/       — bundled pixel font + license (replaces font-terminus).

# ---------------------------------------------------------------------------
# 4. App code: atomic swap into /opt/rabbitears.
#    Uses find for all files (including subpackages), not just top-level.
# ---------------------------------------------------------------------------
log "Installing app code to /opt/rabbitears ..."
APP_SRC="${REPO_ROOT}/src/rabbitears"
APP_DEST="/opt/rabbitears"

# Check if the tree is already identical (skip the swap).
# Compare every file in SRC against DEST, and check for extra files in DEST.
_tree_identical=1
for _f in $(find "$APP_SRC" -type f | sort); do
  _rel="${_f#"$APP_SRC"/}"
  if [ ! -f "$APP_DEST/$_rel" ] || ! cmp -s "$_f" "$APP_DEST/$_rel"; then
    _tree_identical=0
    break
  fi
done
# Also check for files in DEST that are not in SRC (deleted from repo).
if [ "$_tree_identical" -eq 1 ]; then
  for _f in $(find "$APP_DEST" -type f 2>/dev/null | sort); do
    _rel="${_f#"$APP_DEST"/}"
    if [ ! -f "$APP_SRC/$_rel" ]; then
      _tree_identical=0
      break
    fi
  done
fi

if [ "$_tree_identical" -eq 1 ]; then
  log "app tree already up to date"
  # Record every file in the manifest.
  for _f in $(find "$APP_DEST" -type f | sort); do
    _rel="${_f#"$APP_DEST"/}"
    _sha="$(sha256sum "$_f" | cut -d' ' -f1)"
    manifest_entry "$_sha" "$_f" "src/rabbitears/$_rel"
  done
else
  # Atomic swap: copy to .new, mv old aside, mv new into place, rm old.
  rm -rf "${APP_DEST}.new"
  cp -r "$APP_SRC" "${APP_DEST}.new"
  find "${APP_DEST}.new" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  if [ -d "$APP_DEST" ]; then
    mv "$APP_DEST" "${APP_DEST}.old"
  fi
  mv "${APP_DEST}.new" "$APP_DEST"
  rm -rf "${APP_DEST}.old"
  CHANGED=$((CHANGED + 1))
  log "app tree swapped"
  # Record every file in the manifest.
  for _f in $(find "$APP_DEST" -type f | sort); do
    _rel="${_f#"$APP_DEST"/}"
    _sha="$(sha256sum "$_f" | cut -d' ' -f1)"
    manifest_entry "$_sha" "$_f" "src/rabbitears/$_rel"
  done
fi

# ---------------------------------------------------------------------------
# 5. Test pattern image.
# ---------------------------------------------------------------------------
log "Installing test pattern ..."
install_file "${REPO_ROOT}/tools/testpattern.png" /usr/share/rabbitears/testpattern.png 0644

# ---------------------------------------------------------------------------
# 6. Register OpenRC services.
# ---------------------------------------------------------------------------
log "Registering services ..."
rc-update add sshd default        2>/dev/null || true
rc-update add chronyd default     2>/dev/null || true
rc-update add rabbitears default  2>/dev/null || true
# TODO: acpid — enable once the power-button override is in place.

# ---------------------------------------------------------------------------
# 7. lbu: include managed paths (idempotent).
#    Normalise: lbu include -l may strip the leading /, so compare
#    with the leading / removed on both sides.
# ---------------------------------------------------------------------------
log "lbu include ..."
_lbu_include() {
  _path="$1"
  _path_norm="${_path#/}"
  if ! lbu include -l 2>/dev/null | sed 's|^/||' | grep -qx "$_path_norm"; then
    lbu include "$_path"
    log "lbu include: $_path"
  fi
}
_lbu_include /opt/rabbitears
_lbu_include /usr/share/rabbitears

# ---------------------------------------------------------------------------
# 8. LBU coverage check: every manifest path outside /etc must be under
#    an lbu include path.
# ---------------------------------------------------------------------------
log "Checking lbu coverage ..."
_lbu_paths="$(lbu include -l 2>/dev/null | sed 's|^/||' || true)"
_lbu_ok=1
# Extract unique top-level directories from the manifest that are outside /etc.
_manifest_dirs="$(cut -f2 "$MANIFEST_TMP" | grep -v '^/etc/' | while read -r _p; do
  # Walk up to the top-level managed dir (e.g. /opt/rabbitears, /usr/share/rabbitears).
  _d="$_p"
  while [ "${_d#/}" != "${_d##*/}" ] && [ "$_d" != "/" ] && [ "$_d" != "" ]; do
    _parent="$(dirname "$_d")"
    [ "$_parent" = "$_d" ] && break
    _d="$_parent"
  done
  printf '%s\n' "$_d"
done | sort -u)"
for _dest in $_manifest_dirs; do
  _dest_norm="${_dest#/}"
  _covered=0
  for _lbu_p in $_lbu_paths; do
    # Check if _dest_norm is under _lbu_p (or equal).
    case "$_dest_norm" in
      "$_lbu_p"/*|"$_lbu_p") _covered=1; break ;;
    esac
  done
  if [ "$_covered" -eq 0 ]; then
    warn "path $_dest is NOT under any lbu include — it will vanish on reboot"
    _lbu_ok=0
  else
    log "lbu-covered: $_dest"
  fi
done
[ "$_lbu_ok" -eq 1 ] && log "all managed paths are lbu-covered"

# ---------------------------------------------------------------------------
# 9. Write the install manifest atomically.
#    Create the temp file in /etc/rabbitears (same filesystem) so mv is atomic.
# ---------------------------------------------------------------------------
log "Writing install manifest ..."
install -d -m 0755 /etc/rabbitears
_manifest_tmp="$(mktemp /etc/rabbitears/.manifest.XXXXXX)"
cp "$MANIFEST_TMP" "$_manifest_tmp"
chmod 0644 "$_manifest_tmp"
mv "$_manifest_tmp" /etc/rabbitears/manifest

log "setup complete: ${CHANGED} changed"
