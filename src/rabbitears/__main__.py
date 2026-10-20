"""Entry point: ``python -m rabbitears`` (thin; the logic lives in app.py)."""

import sys

from . import app

if __name__ == "__main__":
    sys.exit(app.main())
