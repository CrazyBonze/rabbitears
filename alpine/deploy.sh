#!/usr/bin/env bash
#
# Rabbit Ears — deploy the repo to a target (VM or box) over SSH.
#
# The one way code reaches a target. Streams the working tree via tar over
# ssh (no rsync dependency), runs setup.sh on the target, optionally
# commits/reboots, and finishes with a read-only verify.sh drift check.
#
# Usage:
#   alpine/deploy.sh --vm [--reboot | --restart] [--commit] [--no-verify]
#   alpine/deploy.sh --host H [--port P] [--key K] [--reboot | --restart] [--commit] [--no-verify]
#
# --vm is shorthand for --host 127.0.0.1 --port 2222 --key vm/keys/rabbitears-vm
#      with relaxed host-key checking (the VM's host key changes on every rebuild).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

log()  { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy] WARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Defaults ---
HOST=""
PORT=22
KEY=""
SSH_EXTRA=()
DO_COMMIT=0
DO_RESTART=0
DO_REBOOT=0
DO_VERIFY=1

# --- Parse args ---
while [ $# -gt 0 ]; do
  case "$1" in
    --vm)
      HOST="127.0.0.1"
      PORT=2222
      KEY="${REPO_ROOT}/vm/keys/rabbitears-vm"
      SSH_EXTRA=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
      shift
      ;;
    --host)
      HOST="${2:?--host requires a value}"
      shift 2
      ;;
    --port)
      PORT="${2:?--port requires a value}"
      shift 2
      ;;
    --key)
      KEY="${2:?--key requires a value}"
      shift 2
      ;;
    --commit)
      DO_COMMIT=1
      shift
      ;;
    --restart)
      DO_RESTART=1
      shift
      ;;
    --reboot)
      DO_REBOOT=1
      shift
      ;;
    --no-verify)
      DO_VERIFY=0
      shift
      ;;
    *)
      die "unknown argument: $1 (use --vm or --host H [--port P] [--key K])"
      ;;
  esac
done

[ -n "$HOST" ] || die "no target specified (use --vm or --host H)"
[ "$DO_RESTART" -eq 0 ] || [ "$DO_REBOOT" -eq 0 ] \
  || die "--restart and --reboot are mutually exclusive"

# --- Build the ssh command (BatchMode=yes prevents hanging on prompts) ---
ssh_cmd() {
  local cmd=(ssh -o BatchMode=yes -o ConnectTimeout=5)
  if [ -n "$KEY" ]; then
    cmd+=(-i "$KEY")
  fi
  cmd+=(${SSH_EXTRA[@]+"${SSH_EXTRA[@]}"})
  cmd+=(-p "$PORT" "root@${HOST}")
  cmd+=("$@")
  "${cmd[@]}"
}

# --- Wait for SSH to answer (up to 120s) ---
wait_for_ssh() {
  log "Waiting for SSH on ${HOST}:${PORT} ..."
  local deadline=$(( $(date +%s) + 120 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ssh_cmd true 2>/dev/null; then
      log "SSH is up."
      return 0
    fi
    sleep 2
  done
  die "timed out waiting for SSH on ${HOST}:${PORT}"
}

# --- Wait for SSH to go down (up to 30s) ---
wait_for_ssh_down() {
  log "Waiting for SSH to go down ..."
  local deadline=$(( $(date +%s) + 30 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! ssh_cmd true 2>/dev/null; then
      log "SSH is down."
      return 0
    fi
    sleep 1
  done
  warn "SSH still up after 30s; proceeding anyway."
}

# --- Stream the repo to the target via tar over ssh ---
ship_repo() {
  log "Shipping repo to ${HOST}:/tmp/rabbitears-repo ..."
  ssh_cmd "rm -rf /tmp/rabbitears-repo && mkdir -p /tmp/rabbitears-repo"
  tar \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    -cf - \
    -C "$REPO_ROOT" \
    alpine/ system/ src/ tools/testpattern.png tools/e2e-remote.py \
    | ssh_cmd "tar -xf - -C /tmp/rabbitears-repo"
  log "Repo shipped."
}

# --- Run setup.sh on the target ---
run_setup() {
  log "Running setup.sh on target ..."
  ssh_cmd "sh /tmp/rabbitears-repo/alpine/setup.sh"
}

# --- Diskless guard (fail closed: if we can't tell, assume diskless) ---
check_diskless() {
  # Returns 0 if the target is diskless (/ is tmpfs).
  # Takes the last match in case there are multiple entries for /.
  local fstype
  fstype="$(ssh_cmd "awk '\$2==\"/\" {fs=\$3} END {print fs}' /proc/mounts" 2>/dev/null)" \
    || { warn "cannot determine filesystem type; assuming diskless (fail closed)"; return 0; }
  [ "$fstype" = "tmpfs" ]
}

# --- LBU commit ---
do_commit() {
  if check_diskless; then
    log "Running lbu commit ..."
    ssh_cmd "lbu commit"
  else
    warn "Target is not diskless (LBUOPTS=none); lbu commit is a no-op."
  fi
}

# --- Restart / Reboot ---
do_restart() {
  log "Restarting rabbitears service ..."
  ssh_cmd "rc-service rabbitears restart"
}

do_reboot() {
  # Diskless guard: refuse before doing anything destructive.
  if check_diskless && [ "$DO_COMMIT" -eq 0 ]; then
    die "refusing --reboot without --commit on a diskless target: / is tmpfs, so an uncommitted reboot would discard everything setup.sh just did. Add --commit."
  fi
  # Capture the boot_id so we can detect the new boot.
  local old_boot_id
  old_boot_id="$(ssh_cmd "cat /proc/sys/kernel/random/boot_id" 2>/dev/null || true)"
  log "Rebooting target ..."
  ssh_cmd "reboot" 2>/dev/null || true
  # Wait for the old system to go down.
  wait_for_ssh_down
  # Wait for the new system to come up.
  wait_for_ssh
  # Verify it's actually a new boot (not the old system still answering).
  if [ -n "$old_boot_id" ]; then
    local new_boot_id
    new_boot_id="$(ssh_cmd "cat /proc/sys/kernel/random/boot_id" 2>/dev/null || true)"
    if [ "$new_boot_id" = "$old_boot_id" ]; then
      warn "boot_id unchanged — the target may not have actually rebooted."
    fi
  fi
  # Poll for the service to report started (up to 60s).
  log "Waiting for rabbitears service ..."
  local deadline=$(( $(date +%s) + 60 ))
  local status=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    status="$(ssh_cmd "rc-service rabbitears status" 2>&1 || true)"
    if printf '%s' "$status" | grep -q "started"; then
      log "Service is started."
      return 0
    fi
    sleep 2
  done
  warn "rabbitears service did not report started within 60s. Last status: ${status}"
}

# --- Verify ---
do_verify() {
  log "Running verify.sh ..."
  local verify_script="${SCRIPT_DIR}/verify.sh"
  if [ ! -f "$verify_script" ]; then
    log "verify.sh not found; skipping."
    return 0
  fi
  local verify_args=()
  if [ -n "$KEY" ]; then
    verify_args+=(--host "$HOST" --port "$PORT" --key "$KEY")
  else
    verify_args+=(--host "$HOST" --port "$PORT")
  fi
  if [ ${#SSH_EXTRA[@]} -gt 0 ]; then
    verify_args+=(--insecure)
  fi
  bash "$verify_script" "${verify_args[@]}"
}

# --- Main ---
# Diskless guard: check BEFORE doing anything, so we refuse early.
if [ "$DO_REBOOT" -eq 1 ] && [ "$DO_COMMIT" -eq 0 ]; then
  if check_diskless; then
    die "refusing --reboot without --commit on a diskless target: / is tmpfs, so an uncommitted reboot would discard everything setup.sh just did. Add --commit."
  fi
fi

wait_for_ssh
ship_repo
run_setup

if [ "$DO_COMMIT" -eq 1 ]; then
  do_commit
fi

if [ "$DO_RESTART" -eq 1 ]; then
  do_restart
fi

if [ "$DO_REBOOT" -eq 1 ]; then
  do_reboot
fi

if [ "$DO_VERIFY" -eq 1 ]; then
  do_verify
else
  log "Skipping verify (--no-verify)."
fi

log "Deploy complete."
