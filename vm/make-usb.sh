#!/usr/bin/env bash
#
# Rabbit Ears — build the mock USB stick image (host side).
#
# Creates a partitioned exFAT image labelled RABBITEARS that mimics the real
# media drive (SCOPE §6): one MBR partition, exFAT, label RABBITEARS, with
# the transcoded content from the staging dir synced onto it. The VM attaches
# it as a real USB mass-storage device (vmctl.py run --usb), so the guest sees
# sda1 and the box's mdev rule mounts it by label — exactly like the 3040.
#
# Idempotent: a second run with no content changes copies nothing and does not
# reformat. It never silently reformat an existing image.
#
# Usage (self-elevates under the NOPASSWD rule from vm/setup-sudoers.sh):
#   ./vm/make-usb.sh [--src DIR] [--out IMG] [--size SIZE] [--reformat] [--reset-state]
#
#   --src DIR        staging dir holding channels/ (default: /var/lib/rabbitears-vm/media)
#   --out IMG        output image path       (default: /var/lib/rabbitears-vm/usb.img)
#   --size SIZE      image size, e.g. 16G    (default: 16G)
#   --reformat       rebuild the image even if it exists with a different fs/label
#   --reset-state    drop the on-drive /.rabbitears/ (index + state) before syncing
#
set -euo pipefail

log()  { printf '\033[1;32m[make-usb]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[make-usb] WARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[make-usb] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Privilege: run as root (re-exec with sudo if needed), like provision.sh.
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi
REAL_USER="${SUDO_USER:-root}"

# ---------------------------------------------------------------------------
# Defaults + argument parsing.
# ---------------------------------------------------------------------------
SRC="/var/lib/rabbitears-vm/media"
OUT="/var/lib/rabbitears-vm/usb.img"
SIZE="16G"
REFORMAT=0
RESET_STATE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --src)         SRC="${2:?--src requires a value}"; shift 2 ;;
    --out)         OUT="${2:?--out requires a value}"; shift 2 ;;
    --size)        SIZE="${2:?--size requires a value}"; shift 2 ;;
    --reformat)    REFORMAT=1; shift ;;
    --reset-state) RESET_STATE=1; shift ;;
    *) die "unknown argument: $1 (use --src DIR --out IMG --size SIZE --reformat --reset-state)" ;;
  esac
done

[ -d "$SRC/channels" ] || die "staging dir $SRC/channels not found (run the transcode first)"

# ---------------------------------------------------------------------------
# Refuse to run if a running QEMU has the image open (would corrupt it).
# ---------------------------------------------------------------------------
if [ -f "$OUT" ]; then
  if command -v fuser >/dev/null 2>&1 && fuser "$OUT" >/dev/null 2>&1; then
    die "image $OUT is open by a running process (QEMU?). Stop the VM first (uv run task vm-stop)."
  fi
  if pgrep -af 'qemu-system' 2>/dev/null | grep -qF "$OUT"; then
    die "image $OUT is referenced by a running QEMU. Stop the VM first (uv run task vm-stop)."
  fi
fi

# ---------------------------------------------------------------------------
# Loop / mount state + cleanup (always run, even on failure).
# ---------------------------------------------------------------------------
LOOP=""
MNT=""
cleanup() {
  local rc=$?
  if [ -n "$MNT" ] && grep -q " $MNT " /proc/mounts 2>/dev/null; then
    sync
    umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null || true
  fi
  if [ -n "$LOOP" ]; then
    losetup -d "$LOOP" 2>/dev/null || true
  fi
  return $rc
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Create the image once (sparse file + MBR with one exFAT partition).
# ---------------------------------------------------------------------------
CREATED=0
if [ ! -f "$OUT" ]; then
  log "creating sparse image $OUT ($SIZE) ..."
  mkdir -p "$(dirname "$OUT")"
  truncate -s "$SIZE" "$OUT"
  CREATED=1
else
  log "reusing existing image $OUT"
fi

# (Re)write the MBR partition table when the image is new or --reformat. This
# runs before losetup (which reads the table) and lets --reformat recover a
# corrupt image that has no valid partition.
if [ "$CREATED" -eq 1 ] || [ "$REFORMAT" -eq 1 ]; then
  log "writing MBR partition table (one exFAT partition spanning the disk) ..."
  # MBR (dos): start=2048, size=(empty -> to end), type=0x07 (exFAT). No
  # `unit: sectors` line: this sfdisk rejects the partition line when present.
  printf 'label: dos\n2048,,0x07\n' | sfdisk "$OUT" >/dev/null
fi

# ---------------------------------------------------------------------------
# 2. Attach the loop device with partition scanning.
# ---------------------------------------------------------------------------
LOOP="$(losetup -P --find --show "$OUT")"
PART="${LOOP}p1"
log "attached $OUT -> $LOOP (partition $PART)"

# ---------------------------------------------------------------------------
# 3. Format (new / --reformat) or verify (existing) the exFAT label.
# ---------------------------------------------------------------------------
if [ "$CREATED" -eq 1 ] || [ "$REFORMAT" -eq 1 ]; then
  log "formatting $PART as exFAT, label RABBITEARS ..."
  mkfs.exfat -L RABBITEARS "$PART" >/dev/null
else
  fs_type="$(blkid -s TYPE -o value "$PART" 2>/dev/null || true)"
  fs_label="$(blkid -s LABEL -o value "$PART" 2>/dev/null || true)"
  if [ "$fs_type" != "exfat" ] || [ "$fs_label" != "RABBITEARS" ]; then
    die "image $OUT partition 1 is not exFAT labelled RABBITEARS (type='${fs_type}' label='${fs_label}'). Re-run with --reformat to rebuild it."
  fi
  log "existing image verified: exFAT, label RABBITEARS"
fi

# ---------------------------------------------------------------------------
# 4. Mount the partition and sync content.
# ---------------------------------------------------------------------------
MNT="$(mktemp -d /tmp/rabbitears-usb.XXXXXX)"
mount -t exfat -o rw "$PART" "$MNT"
log "mounted $PART at $MNT"

# exFAT has no owners/permissions, so rsync -rt (no -o/-g). --modify-window=2
# matches exFAT's 2-second timestamp granularity so unchanged files are not
# re-copied. --delete removes files that left the staging dir.
rsync -rt --delete --modify-window=2 "$SRC/channels/" "$MNT/channels/"

# Preserve the on-drive /.rabbitears/ (index + state written by the box) unless
# --reset-state is given. The rsync above only touches channels/, so this is
# the only thing that can remove it.
if [ "$RESET_STATE" -eq 1 ]; then
  log "resetting on-drive state (/.rabbitears/) ..."
  rm -rf "$MNT/.rabbitears"
fi

# ---------------------------------------------------------------------------
# 5. Ownership so the user can open the image through vmctl.py (kvm group).
# ---------------------------------------------------------------------------
chown root:kvm "$OUT" 2>/dev/null || chown root:kvm "$OUT"
chmod 0660 "$OUT"

# ---------------------------------------------------------------------------
# 6. Summary.
# ---------------------------------------------------------------------------
echo
log "=== mock USB stick: $OUT ==="
log "label: RABBITEARS   fs: exfat   size: $SIZE"
for d in "$MNT"/channels/*/; do
  [ -d "$d" ] || continue
  n="$(find "$d" -maxdepth 1 -name '*.mp4' | wc -l | tr -d ' ')"
  log "  channel $(basename "$d"): $n file(s)"
done
if [ -d "$MNT/.rabbitears" ]; then
  log "  .rabbitears/: present (index/state preserved)"
fi
log "usage: $(df -h "$MNT" | awk 'NR==2 {print $3" used / "$2" total ("$5" full)"}')"
log "done."
