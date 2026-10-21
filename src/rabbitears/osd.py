"""On-screen display text (SCOPE §4.5).

Pure module (like ``schedule.py``): no I/O, no clock reads, and no imports
from the app, the player, or mpv. It turns plain inputs (a channel number, a
title, a duration, an offset) into the display strings the app shows through
``player.show_osd_text``.

The strings are mpv ``osd-overlay`` payloads (``ass-events`` format). Two mpv
0.41 facts shape them (both verified against mpv 0.41 in the VM):

* ``osd-overlay`` splits its ``data`` on newlines into one ASS *event per
  line*, and libass applies the first ``\\an``/``\\pos`` of each event to that
  event alone. Each element (flash top-right, volume/mute bottom-left, overlay
  centre) is therefore its own line, so a ``show-text`` cannot position them
  independently but ``osd-overlay`` can. The overlay block's own three lines
  use a hard ``\\N`` break so they stay one centred event.
* ``osd-overlay`` does *not* escape ASS tags. User-controlled text (channel
  name, title) is escaped here with :func:`ass_escape`, so a ``{`` in a
  filename can't start a tag.

The overlay is owned by the IPC connection that created it, so the player keeps
a persistent socket for it (see ``Player._osd_command``).

Font sizes are relative: 288 = full screen height. Every OSD element (the
channel flash, the info overlay, and the volume/mute indicator) uses the same
``osd_font_size`` (36 ≈ an eighth of the screen), so a zap and a volume press
produce the same-sized text.
"""

from __future__ import annotations

from pathlib import Path

# ASS stores colour as &HBBGGRR& (BGR, not RGB). The default is the config
# default green, #33CC33 (palindromic, so identical in RGB and BGR).
_DEFAULT_COLOR = "&H33CC33&"
_DEFAULT_FONT_SIZE = 36
_BORDER = 3  # thick border: readability over composite on a CRT (SCOPE §4.5)
# The bundled OSD face (system/fonts/VCR_OSD_Mono.ttf, installed to
# /usr/share/fonts/rabbitears/ by alpine/setup.sh). A VCR/CRT-style monospace
# that fits the "cable box" look (SCOPE §4.5); referenced by the ASS \fn tag.
_FONT = "VCR OSD Mono"

# osd-overlay passes its data to libass verbatim, so user-controlled text (the
# channel name, the title) must be ASS-escaped: a "{" in a filename would
# otherwise start an override tag.
def ass_escape(text: str) -> str:
    """Escape ``text`` for literal display in an ASS event (never crash)."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

# The bundled icon face (system/fonts/pixelart-icons-font.ttf). Pixelarticons
# (MIT, SCOPE §9) ships a prebuilt TTF whose glyphs sit in the private-use
# area; the app renders them inline via ``\fn``, the same single-renderer path
# as the text face (SCOPE §4.5 decision log).
_ICON_FONT = "pixelart-icons-font"
# Icon glyphs (PUA codepoints from pixelart-icons-font.ttf, from the font's CSS):
ICON_VOLUME = "\uedee"   # "volume" — a speaker
ICON_MUTED = "\ueded"    # "volume-x" — a speaker with an X


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


def compose_osd(*lines: str) -> str:
    """Join anchored ASS event fragments into one ``osd-overlay`` payload.

    Each fragment is a complete ASS event (its own ``\\an`` anchor and style);
    ``osd-overlay`` splits the data on newlines into one event per line, so
    each keeps its own anchor.
    """
    return "\n".join(lines)


def flash_line(number: int, color: object, font_size: object) -> str:
    """The channel-flash line (top-right ``\\an9``) for :func:`compose_osd`."""
    return f"{{\\an9}}{_style(color, font_size)}{channel_flash_text(number)}"


def volume_line(level: int, muted: bool, color: object, font_size: object) -> str:
    """The volume/mute indicator line (bottom-left ``\\an1``) for ``compose_osd``.

    The icon glyph uses the bundled Pixelarticons face, the numeric level the
    OSD face — two inline ``\\fn`` spans on one line. The level is always shown
    (SCOPE §4.4: volume and mute are independent, so a muted press still
    reveals the pre-set level); only the icon changes with mute state.
    """
    size = _font_size(font_size)
    icon = ICON_MUTED if muted else ICON_VOLUME
    body = f"{{\\fn{_ICON_FONT}}}{icon}{{\\fn{_FONT}}} {level}"
    return f"{{\\an1}}{{\\fs{size}}}{{\\c{ass_color(color)}}}{{\\bord{_BORDER}}}{body}"


def info_block(
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
    # Hard \N breaks keep the three overlay lines in ONE event (so the single
    # \an8 centres the whole block), unlike the \n that splits osd-overlay
    # events apart.
    return (
        f"{{\\an8}}{_style(color, font_size)}"
        f"CH {number:02d} · {ass_escape(name)}\\N"
        f"{ass_escape(title_line)}\\N"
        f"{remaining_line}"
    )
