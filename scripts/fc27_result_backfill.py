#!/usr/bin/env python3
"""Thin executable wrapper for the FC27 Result Backfill CLI."""

from __future__ import annotations

import sys
from src.fc27_result_backfill import main

if __name__ == "__main__":
    sys.exit(main())
