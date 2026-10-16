"""Clock-based "live" schedule math (SCOPE.md §4.2).

A channel's files play in a fixed deterministic order, forming one long
loop whose length is the sum of all file durations.  Instead of counting
frames or advancing a counter (which drifts), the playback position is
derived from the wall clock:

    position = (now - EPOCH) mod loop_length

EPOCH is a fixed constant shared by both boxes, so they always air the
same schedule.  Given the position, we walk the playlist to find which
file contains it and the seek offset within that file.  On end-of-file
the position is simply recomputed from the clock, so drift never
accumulates.

Both functions here are pure: no I/O, no global state.
"""

from __future__ import annotations


def compute_position(epoch: float, loop_length: float, now: float) -> float:
    """Return the playback position in [0, loop_length) for time `now`.

    `epoch` and `now` are Unix timestamps (floats); `loop_length` is the
    total duration of the loop in seconds.

    Python's modulo always returns a result with the sign of the divisor,
    so `now < epoch` (clock set before the epoch) wraps around to near
    the *end* of the loop rather than crashing — a dead CMOS battery
    shifts the schedule but must not break anything (SCOPE §4.2).
    """
    if loop_length <= 0:
        raise ValueError("loop_length must be positive")
    return (now - epoch) % loop_length


def find_file(files: list[tuple[str, float]], position: float) -> tuple[str, float]:
    """Return (path, offset) for the file containing `position`.

    `files` is a list of (path, duration) tuples in playlist order.
    `position` must be within the total length of the playlist; a
    position at the very end clamps to the last file at offset 0.
    Raises ValueError if `files` is empty.
    """
    if not files:
        raise ValueError("files is empty")
    if position < 0:
        raise ValueError("position must be non-negative")

    total = sum(duration for _, duration in files)
    if position >= total:
        # Only reachable at the exact loop boundary (or bad input);
        # treat as the start of the loop.
        position = 0.0

    for path, duration in files:
        if position < duration:
            return (path, position)
        position -= duration

    # Unreachable if durations are non-negative, but keep a safe fallback.
    last = files[-1]
    return (last[0], 0.0)
