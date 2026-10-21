"""On-screen display text (SCOPE §4.5).

Pure module (like ``schedule.py``): no I/O, no clock reads, and no imports
from the app, the player, or mpv. It turns plain inputs (a channel number, a
title, a duration, an offset) into the display strings the app shows through
``player.show_osd_text``.

The strings are mpv ``show-text`` payloads. Two mpv 0.41 quirks shape them
(both verified against mpv 0.41 in the VM):

* ``show-text`` *escapes* ASS tags by default. The payloads therefore start
  with the unescape marker (U+0092 ``a0`` — what the ``osd-ass-cc/0``
  property expands to), and user-controlled text (channel name, title) is
  re-escaped with the escape marker (U+0092 ``a1``) so a ``{`` in a filename
  can't start a tag.
* ASS anchor tags position *lines*, not spans: the flash and the overlay must
  be on separate lines. mpv's OSD message layer is a single surface, so when
  both are up they are composed into one payload (``flash_over_info_ass``);
  the app re-sends the overlay-only payload when the flash expires, so a
  flash never wipes the overlay.

Font sizes are relative: 288 = full screen height, so the flash uses
``osd_font_size`` (72 ≈ a quarter of the screen) and the overlay uses half.
"""

from __future__ import annotations

from pathlib import Path

# ASS stores colour as &HBBGGRR& (BGR, not RGB). The default is the config
# default green, #33CC33 (palindromic, so identical in RGB and BGR).
_DEFAULT_COLOR = "&H33CC33&"
_DEFAULT_FONT_SIZE = 72
_BORDER = 3  # thick border: readability over composite on a CRT (SCOPE §4.5)
# The bundled OSD face (system/fonts/VCR_OSD_Mono.ttf, installed to
# /usr/share/fonts/rabbitears/ by alpine/setup.sh). A VCR/CRT-style monospace
# that fits the "cable box" look (SCOPE §4.5); referenced by the ASS \fn tag.
_FONT = "VCR OSD Mono"

# mpv's show-text escapes ASS tags unless the unescape marker (U+0092 "a0")
# precedes them; the escape marker (U+0092 "a1") re-enables escaping for the
# user-controlled text (channel name, title) so a "{" in a filename is safe.
_ASS_UNESCAPE = "\u0092a0"
_ASS_ESCAPE = "\u0092a1"


def channel_flash_text(number: int) -> str:
    """The whole channel flash: ``CH 03`` for number 3. No name, nothing else."""
    return f"CH {number:02d}"


def show_title(path: Path) -> str:
    """The on-screen title for a file: the filename without its extension.

    ``S01E01 - Opening.mp4`` -> ``S01E01 - Opening``; a name with no extension
    is returned unchanged. No ``S01E03`` parsing, no prettifying (SCOPE §4.5:
    the title is "from filename").
    """
    return path.stem


def remaining_seconds(duration: float, offset: float, elapsed: float) -> float:
    """Seconds left in the current file: ``duration - (offset + elapsed)``.

    Clamped at 0 for a file that has overrun (a late step() near EOF). Pure
    math: the caller supplies the clock value.
    """
    return max(0.0, duration - (offset + elapsed))


def format_remaining(seconds: float) -> str:
    """Human text for the overlay's remaining line.

    Under an hour: ``Mm SSs`` (``12m 30s``, ``0m 05s``). An hour or more:
    ``Hh MMm SSs`` (``1h 05m 09s``). Truncated to whole seconds.
    """
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def info_text(number: int, name: str, title: str, remaining: str) -> str:
    """The overlay body: channel line, title, remaining line — in that order."""
    return "\n".join(_info_lines(number, name, title, remaining))


def _info_lines(
    number: int, name: str, title: str, remaining: str
) -> tuple[str, str, str]:
    """The three overlay lines (channel line, title, remaining line)."""
    return f"CH {number:02d} · {name}", title, f"{remaining} remaining"


# --- ASS wrappers (pure; the player decides how the string reaches mpv) ----

def ass_color(hex_color: object) -> str:
    """``#RRGGBB`` -> ASS ``&HBBGGRR&`` (ASS stores colour as BGR).

    Anything that is not a six-digit hex colour falls back to the default
    green, so a bad config value can never break the OSD.
    """
    if isinstance(hex_color, str):
        h = hex_color.strip()
        if h.startswith("#"):
            h = h[1:]
        if len(h) == 6:
            try:
                int(h, 16)
            except ValueError:
                return _DEFAULT_COLOR
            r, g, b = h[0:2], h[2:4], h[4:6]
            return f"&H{b}{g}{r}&"
    return _DEFAULT_COLOR


def _font_size(font_size: object) -> int:
    """A sane ASS font size, or the default (never crash on a bad config)."""
    if (
        isinstance(font_size, int)
        and not isinstance(font_size, bool)
        and 8 <= font_size <= 999
    ):
        return font_size
    return _DEFAULT_FONT_SIZE


def _style(color: object, font_size: object) -> str:
    return (
        f"{{\\fn{_FONT}}}"
        f"{{\\fs{_font_size(font_size)}}}"
        f"{{\\c{ass_color(color)}}}"
        f"{{\\bord{_BORDER}}}"
    )


def flash_ass(flash_text: str, color: object, font_size: object) -> str:
    """The channel flash payload: top-right (``\\an9``), large, bordered.

    ``flash_text`` is app-generated (``CH NN``), so no re-escaping is needed.
    """
    return f"{_ASS_UNESCAPE}{{\\an9}}{_style(color, font_size)}{flash_text}"


def _info_payload(
    number: int, name: str, title: str, remaining: str,
    color: object, font_size: object,
) -> str:
    """The overlay lines as an ASS payload (centre, ``\\an8``).

    The channel name and the title come from the drive (user-controlled), so
    they are re-escaped: a ``{`` in a filename can't start an ASS tag.
    """
    _channel_line, title_line, remaining_line = _info_lines(
        number, name, title, remaining
    )
    return (
        f"{{\\an8}}{_style(color, font_size)}"
        f"CH {number:02d} · {_ASS_ESCAPE}{name}\n"
        f"{_ASS_ESCAPE}{title_line}\n"
        f"{remaining_line}"
    )


def info_ass(
    number: int, name: str, title: str, remaining: str,
    color: object, font_size: object,
) -> str:
    """The info overlay payload: centre screen (``\\an8``), bordered."""
    return _ASS_UNESCAPE + _info_payload(
        number, name, title, remaining, color, font_size
    )


def flash_over_info_ass(
    number: int, name: str, title: str, remaining: str,
    color: object, flash_font_size: object, info_font_size: object,
) -> str:
    """Flash + overlay in one payload: flash top-right, overlay centre.

    ASS anchor tags position *lines*, so the flash is its own line above the
    overlay block. Used when the overlay is up and a tune fires the flash;
    the app re-sends the overlay-only payload (``info_ass``) when the flash
    expires, so the flash never wipes the overlay.
    """
    return (
        f"{_ASS_UNESCAPE}{{\\an9}}{_style(color, flash_font_size)}"
        f"{channel_flash_text(number)}\n"
        + _info_payload(number, name, title, remaining, color, info_font_size)
    )
