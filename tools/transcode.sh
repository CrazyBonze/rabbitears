#!/usr/bin/env bash
#
# Rabbit Ears — transcode media to SCOPE §5 compliance (desktop-only).
#
# Transcode mode:
#   tools/transcode.sh [-j JOBS] SRC_DIR DEST_DIR
#
# Check mode:
#   tools/transcode.sh --check DIR
#
# SCOPE §5: MP4, H.264 High ≤ L4.1, ≤720p, 8-bit yuv420p, AAC stereo.
# Never run on the box — this is a desktop tool.
#
set -euo pipefail

log()  { printf '\033[1;32m[transcode]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[transcode] WARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[transcode] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Video file extensions (case-insensitive).
VIDEO_EXTS="mp4 mkv webm avi mov m4v"

# ---------------------------------------------------------------------------
# check_file FILE — verify one mp4 is SCOPE §5 compliant. Returns 0 if OK.
# ---------------------------------------------------------------------------
check_file() {
  local f="$1"
  local violations=()

  # Get video stream info.
  local vcodec vprofile vlevel width height pixfmt
  vcodec="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null || true)"
  vprofile="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=profile -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null || true)"
  vlevel="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=level -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null || true)"
  width="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=width -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null || true)"
  height="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=height -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null || true)"
  pixfmt="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=pix_fmt -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null || true)"

  # Codec must be h264.
  if [ "$vcodec" != "h264" ]; then
    violations+=("codec=${vcodec} (expected h264)")
  fi

  # Profile must be High or lower (Baseline, Constrained Baseline, Main, High).
  case "$vprofile" in
    Baseline|"Constrained\ Baseline"|Main|High) ;;
    *) violations+=("profile=${vprofile} (expected High or lower)") ;;
  esac

  # Level must be ≤ 4.1 (ffprobe reports as integer: 41 = 4.1).
  if [ -n "$vlevel" ] && [ "$vlevel" -gt 41 ] 2>/dev/null; then
    violations+=("level=${vlevel} (expected ≤ 41)")
  fi

  # Resolution must be ≤ 1280x720.
  if [ -n "$width" ] && [ -n "$height" ]; then
    if [ "$width" -gt 1280 ] || [ "$height" -gt 720 ]; then
      violations+=("resolution=${width}x${height} (expected ≤ 1280x720)")
    fi
  fi

  # Pixel format must be yuv420p (8-bit).
  if [ "$pixfmt" != "yuv420p" ]; then
    violations+=("pix_fmt=${pixfmt} (expected yuv420p)")
  fi

  # Audio: if present, must be AAC.
  local acodec
  acodec="$(ffprobe -v error -select_streams a:0 \
    -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null || true)"
  if [ -n "$acodec" ] && [ "$acodec" != "aac" ]; then
    violations+=("audio=${acodec} (expected aac)")
  fi

  if [ ${#violations[@]} -gt 0 ]; then
    for v in "${violations[@]}"; do
      printf 'VIOLATION %s: %s\n' "$f" "$v"
    done
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# check_mode DIR — check all .mp4 files in DIR.
# ---------------------------------------------------------------------------
check_mode() {
  local dir="$1"
  [ -d "$dir" ] || die "directory not found: $dir"
  local found=0 failed=0
  while IFS= read -r -d '' f; do
    found=$((found + 1))
    if ! check_file "$f"; then
      failed=$((failed + 1))
    fi
  done < <(find "$dir" -maxdepth 1 -name '*.mp4' -print0 | sort -z)
  log "checked $found file(s), $failed violation(s)"
  [ "$failed" -eq 0 ]
}

# ---------------------------------------------------------------------------
# transcode_one SRC DEST — transcode a single file.
# ---------------------------------------------------------------------------
transcode_one() {
  local src="$1"
  local dest="$2"
  local basename
  basename="$(basename "$src")"
  local out="${dest}/$(basename "${basename%.*}").mp4"
  local part="${out}.part"

  # Skip if output exists, is newer, and passes check.
  if [ -f "$out" ] && [ "$out" -nt "$src" ]; then
    if check_file "$out" >/dev/null 2>&1; then
      log "skip (up-to-date): $(basename "$out")"
      return 10  # skip sentinel
    fi
  fi

  log "transcoding: $(basename "$src") -> $(basename "$out")"

  # Build ffmpeg args.
  # -f mp4: the .part extension doesn't let ffmpeg guess the format.
  # -map 0:V:0: skips attached cover-art streams (safer than -map v:0).
  local ffmpeg_args=(
    -i "$src"
    -map 0:V:0
    -map 0:a:0?
    -c:v libx264
    -profile:v high
    -level:v 4.1
    -pix_fmt yuv420p
    # Scale: fit within 1280x720, never upscale, even dimensions.
    -vf "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    -c:a aac
    -ac 2
    -ar 48000
    -movflags +faststart
    -f mp4
    -y
    "$part"
  )

  if ffmpeg "${ffmpeg_args[@]}" </dev/null 2>"${part}.log"; then
    mv "$part" "$out"
    rm -f "${part}.log"
    return 0
  else
    warn "transcode failed: $(basename "$src") (see ${part}.log)"
    rm -f "$part"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# transcode_mode SRC_DIR DEST_DIR JOBS
# ---------------------------------------------------------------------------
transcode_mode() {
  local src_dir="$1"
  local dest_dir="$2"
  local jobs="$3"

  [ -d "$src_dir" ] || die "source directory not found: $src_dir"
  mkdir -p "$dest_dir"

  # Collect video files.
  local files=()
  while IFS= read -r -d '' f; do
    files+=("$f")
  done < <(
    for ext in $VIDEO_EXTS; do
      find "$src_dir" -maxdepth 1 -iname "*.${ext}" -print0 2>/dev/null
    done | sort -z
  )

  [ ${#files[@]} -gt 0 ] || { log "no video files found in $src_dir"; return 0; }

  # Check for name collisions: a.mkv and a.mp4 would both become a.mp4.
  declare -A _out_names=()
  for f in "${files[@]}"; do
    _bn="$(basename "$f")"
    _out="${_bn%.*}.mp4"
    if [ -n "${_out_names[$_out]:-}" ]; then
      die "name collision: ${_out_names[$_out]} and $f both produce $_out. Rename one before transcoding."
    fi
    _out_names[$_out]="$f"
  done

  log "found ${#files[@]} file(s), $jobs job(s)"

  local transcoded=0 skipped=0 failed=0

  # Run transcodes in parallel.
  # NOTE: `wait` returns the job's exit code, which would kill the script
  # under `set -e`. Use `rc=0; wait ... || rc=$?` to capture it safely.
  local pids=()
  for f in "${files[@]}"; do
    transcode_one "$f" "$dest_dir" &
    pids+=($!)
    # Throttle to $jobs concurrent.
    while [ ${#pids[@]} -ge "$jobs" ]; do
      local new_pids=()
      for pid in "${pids[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
          local rc=0
          wait "$pid" 2>/dev/null || rc=$?
          if [ $rc -eq 10 ]; then
            skipped=$((skipped + 1))
          elif [ $rc -ne 0 ]; then
            failed=$((failed + 1))
          else
            transcoded=$((transcoded + 1))
          fi
        else
          new_pids+=("$pid")
        fi
      done
      pids=("${new_pids[@]+"${new_pids[@]}"}")
      [ ${#pids[@]} -ge "$jobs" ] && sleep 0.1
    done
  done

  # Wait for remaining.
  for pid in "${pids[@]+"${pids[@]}"}"; do
    local rc=0
    wait "$pid" 2>/dev/null || rc=$?
    if [ $rc -eq 10 ]; then
      skipped=$((skipped + 1))
    elif [ $rc -ne 0 ]; then
      failed=$((failed + 1))
    else
      transcoded=$((transcoded + 1))
    fi
  done

  log "done: $transcoded transcoded, $skipped skipped, $failed failed"
  [ "$failed" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if [ $# -eq 0 ]; then
  die "usage: $0 [-j JOBS] SRC_DIR DEST_DIR | $0 --check DIR"
fi

if [ "$1" = "--check" ]; then
  [ $# -eq 2 ] || die "--check takes exactly one argument: DIR"
  check_mode "$2"
  exit $?
fi

# Transcode mode.
JOBS=$(( $(nproc) / 2 ))
[ "$JOBS" -lt 1 ] && JOBS=1

if [ "$1" = "-j" ]; then
  JOBS="${2:?-j requires a value}"
  case "$JOBS" in
    ''|*[!0-9]*) die "-j must be a positive integer, got: $JOBS" ;;
  esac
  [ "$JOBS" -ge 1 ] || die "-j must be >= 1, got: $JOBS"
  shift 2
fi

[ $# -eq 2 ] || die "usage: $0 [-j JOBS] SRC_DIR DEST_DIR"
transcode_mode "$1" "$2" "$JOBS"
