"""Rabbit Ears — kid-safe "cable box" (mpv + clock schedule + remote).

The console-script entry point (``pyproject.toml``: ``rabbitears =
"rabbitears:main"``) and ``python -m rabbitears`` both funnel into
:func:`rabbitears.app.main`.
"""

from . import app


def main() -> None:
    raise SystemExit(app.main())
