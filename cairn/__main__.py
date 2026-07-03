"""Enable `python -m cairn`."""

from __future__ import annotations

import sys

from cairn.cli import main

if __name__ == "__main__":
    sys.exit(main())
