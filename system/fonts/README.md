# system/fonts — bundled OSD font

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
