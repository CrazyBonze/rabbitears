# system/fonts — bundled OSD fonts

## VCR OSD Mono

- **File:** `VCR_OSD_Mono.ttf`
- **Author:** Riciery Leal
- **Source:** <https://www.dafont.com/vcr-osd-mono.font>
- **License:** freeware — listed "100% Free" on dafont and "free for personal
  and commercial use" by the font mirrors. No OFL/OSI license text ships inside
  the font archive; redistribution as freeware is the author's stated intent.

A monospace, VCR/CRT-style typeface that reproduces the on-screen lettering of
vintage VCR displays — the right look for the "cable box" OSD (SCOPE §4.5).
It's a TrueType font with scalable outlines, so it renders at the OSD's large
sizes (unlike `font-terminus`, a bitmap font with 12–32 px faces only).

Installed by `alpine/setup.sh` to `/usr/share/fonts/rabbitears/` (so the lbu
include stays scoped, not the whole font dir) and referenced by
`src/rabbitears/osd.py` via the ASS `\fnVCR OSD Mono` tag. `font-dejavu`
(`alpine/packages.txt`) stays as the fontconfig fallback for glyphs VCR OSD
Mono lacks (e.g. accented characters in filenames).

## Pixelarticons (icon font)

- **File:** `pixelart-icons-font.ttf`
- **Author:** Gerrit Halfmann
- **Source:** <https://pixelarticons.com> (npm package `pixelarticons`, `fonts/pixelart-icons-font.ttf`)
- **License:** MIT (`LICENSE` in the package; SCOPE §9).

The on-screen icons (volume, mute) are glyphs from Pixelarticons' prebuilt
webfont, drawn on a strict 24×24 pixel grid. Icons live in the private-use area
(`U+EA01–U+EE0C`); the two the app uses are `U+EDEE` ("volume") and `U+EDED`
("volume-x"), referenced by `src/rabbitears/osd.py` (`ICON_VOLUME`,
`ICON_MUTED`) via the ASS `\fnpixelart-icons-font` tag. Rendered through the
same `show-text`/libass path as the text face — no second overlay layer
(SCOPE §4.5 decision log).
