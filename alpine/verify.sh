#!/usr/bin/env bash
#
# Rabbit Ears — read-only drift check for a target (VM or box).
#
# Compares the target's installed state against the repo's manifest and
# current source files. Never modifies the target. Exits 0 only if clean.
#
# Usage:
#   alpine/verify.sh --vm
#   alpine/verify.sh --host H [--port P] [--key K] [--insecure]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROBLEMS=0

log()  { printf '\033[1;32m[verify]\033[0m %s\n' "$*"; }
finding() {
  printf '\033[1;31m[verify] %s\033[0m\n' "$*"
  PROBLEMS=$((PROBLEMS + 1))
}

# --- Defaults ---
HOST=""
PORT=22
KEY=""
SSH_EXTRA=()

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
    --insecure)
      SSH_EXTRA+=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
      shift
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 1
      ;;
  esac
done

[ -n "$HOST" ] || { printf 'no target specified (use --vm or --host H)\n' >&2; exit 1; }

# --- SSH helper (BatchMode=yes prevents hanging on prompts) ---
ssh_cmd() {
  local cmd=(ssh -o BatchMode=yes -o ConnectTimeout=10)
  if [ -n "$KEY" ]; then
    cmd+=(-i "$KEY")
  fi
  cmd+=(${SSH_EXTRA[@]+"${SSH_EXTRA[@]}"})
  cmd+=(-p "$PORT" "root@${HOST}")
  cmd+=("$@")
  "${cmd[@]}"
}

# --- 0. Check that the manifest exists ---
MANIFEST="$(ssh_cmd "cat /etc/rabbitears/manifest 2>/dev/null" || true)"
if [ -z "$MANIFEST" ]; then
  finding "no manifest found on target — never deployed? Run: uv run task deploy-vm"
  printf 'verify: %d problem(s)\n' "$PROBLEMS"
  exit 1
fi

# --- 1. Drift on target: every manifest entry's target hash matches ---
log "Checking for target drift ..."
while IFS=$'\t' read -r sha target_path repo_rel; do
  [ -n "$sha" ] || continue
  # Managed blocks are not whole files; they are checked in section 2b.
  case "$target_path" in *::block::*) continue ;; esac
  actual_sha="$(ssh_cmd "sha256sum '$target_path' 2>/dev/null | cut -d' ' -f1" || true)"
  if [ -z "$actual_sha" ]; then
    finding "DRIFT ${target_path}: file missing on the target since the last deploy. Redeploy."
  elif [ "$actual_sha" != "$sha" ]; then
    finding "DRIFT ${target_path}: changed on the target since the last deploy. Fix the repo and redeploy; don't edit the target."
  fi
done <<< "$MANIFEST"

# --- 2. Stale deploy: every recorded hash matches the current repo source ---
log "Checking for stale deploy ..."
while IFS=$'\t' read -r sha target_path repo_rel; do
  [ -n "$sha" ] || continue
  local_src="${REPO_ROOT}/${repo_rel}"
  if [ ! -f "$local_src" ]; then
    finding "STALE ${target_path}: repo source ${repo_rel} no longer exists. Redeploy."
    continue
  fi
  local_sha="$(sha256sum "$local_src" | cut -d' ' -f1)"
  if [ "$local_sha" != "$sha" ]; then
    finding "STALE ${target_path}: the repo changed since the last deploy. Redeploy (uv run task deploy-vm / deploy-box)."
  fi
done <<< "$MANIFEST"

# --- 2b. Managed blocks: extract from the target and check missing/dup/edited.
#      (STALE for a block is already covered by section 2, which compares the
#       recorded snippet hash against the current repo snippet.) ---
log "Checking managed blocks ..."
while IFS=$'\t' read -r sha target_path repo_rel; do
  [ -n "$sha" ] || continue
  case "$target_path" in
    *::block::*) ;;
    *) continue ;;
  esac
  file="${target_path%%::block::*}"
  name="${target_path##*::block::}"
  # Count BEGIN-line occurrences (detect missing / duplicated).
  count="$(ssh_cmd "grep -c '^# BEGIN ${name}' '$file' 2>/dev/null" || true)"
  if [ -z "$count" ] || [ "$count" = "0" ]; then
    finding "DRIFT ${file} block '${name}': missing on the target. Redeploy."
    continue
  fi
  if [ "$count" -gt 1 ]; then
    finding "DRIFT ${file} block '${name}': appears ${count} times (should be 1). Redeploy."
  fi
  # Extract the block (BEGIN..END inclusive) and compare to the repo snippet.
  block="$(ssh_cmd "awk -v b='# BEGIN ${name}' -v e='# END ${name}' 'index(\$0,b)==1{s=1} s{print} index(\$0,e)==1{s=0}' '$file' 2>/dev/null" || true)"
  snippet="${REPO_ROOT}/${repo_rel}"
  if [ ! -f "$snippet" ]; then
    finding "STALE ${file} block '${name}': repo snippet ${repo_rel} no longer exists. Redeploy."
    continue
  fi
  if [ "$block" != "$(cat "$snippet")" ]; then
    finding "DRIFT ${file} block '${name}': edited on the target since the last deploy. Fix the repo and redeploy."
  fi
done <<< "$MANIFEST"

# --- 3. Unmanaged files in /opt/rabbitears and /usr/share/rabbitears ---
log "Checking for unmanaged files ..."
for dir in /opt/rabbitears /usr/share/rabbitears; do
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    # Check if this file is in the manifest.
    if ! grep -qP "^\S+\t${f}\t" <<< "$MANIFEST" 2>/dev/null; then
      finding "UNMANAGED ${f}: present on the target but not in the manifest."
    fi
  done < <(ssh_cmd "find '$dir' -type f 2>/dev/null" || true)
done

# --- 4. Packages: every package in packages.txt is installed ---
log "Checking packages ..."
PKGS_FILE="${REPO_ROOT}/alpine/packages.txt"
if [ -f "$PKGS_FILE" ]; then
  PKGS="$(grep -vE '^[[:space:]]*(#|$)' "$PKGS_FILE" \
    | sed 's/[[:space:]]*#.*$//' \
    | tr -s '[:space:]' '\n' \
    | grep -vE '^[[:space:]]*$' || true)"
  while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    if ! ssh_cmd "apk info -e '$pkg' >/dev/null 2>&1"; then
      finding "MISSING PACKAGE ${pkg}: not installed on the target."
    fi
  done <<< "$PKGS"
fi

# --- 5. Services: sshd, chronyd, rabbitears, rabbitears-remote, acpid in the
#    default runlevel; rabbitears/rabbitears-remote/acpid started ---
log "Checking services ..."
for svc in sshd chronyd rabbitears rabbitears-remote acpid; do
  if ! ssh_cmd "rc-status default 2>/dev/null | grep -q ' ${svc} '"; then
    finding "SERVICE ${svc}: not in the default runlevel."
  fi
done
for svc in rabbitears rabbitears-remote acpid; do
  if ! ssh_cmd "rc-service ${svc} status 2>&1" | grep -q "started"; then
    finding "SERVICE ${svc}: not started."
  fi
done

# --- 6. lbu: each include path appears exactly once ---
# Normalise: lbu include -l may strip the leading /, so compare
# with the leading / removed on both sides.
log "Checking lbu includes ..."
LBU_LIST="$(ssh_cmd "lbu include -l 2>/dev/null" | sed 's|^/||' || true)"
for path in /opt/rabbitears /usr/share/rabbitears; do
  path_norm="${path#/}"
  count=0
  while IFS= read -r line; do
    line_norm="${line#/}"
    if [ "$line_norm" = "$path_norm" ]; then
      count=$((count + 1))
    fi
  done <<< "$LBU_LIST"
  if [ "$count" -eq 0 ]; then
    finding "LBU ${path}: not in lbu include list."
  elif [ "$count" -gt 1 ]; then
    finding "LBU ${path}: appears ${count} times in lbu include list (should be 1)."
  fi
done

# --- Result ---
if [ "$PROBLEMS" -eq 0 ]; then
  log "verify: OK"
  exit 0
else
  printf 'verify: %d problem(s)\n' "$PROBLEMS"
  exit 1
fi
