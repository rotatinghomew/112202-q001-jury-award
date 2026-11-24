#!/usr/bin/env python3
"""Root wrapper so isolated Python can find the project package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from voucher_reserve.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
