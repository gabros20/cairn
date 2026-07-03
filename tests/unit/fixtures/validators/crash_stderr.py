#!/usr/bin/env python
"""Exits 3 with diagnostic lines on stderr — proves the last stderr line surfaces."""
import sys
print("first diagnostic", file=sys.stderr)
print("boom-last-line", file=sys.stderr)
sys.exit(3)
