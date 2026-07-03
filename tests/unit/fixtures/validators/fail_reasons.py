#!/usr/bin/env python
"""Fails with two reason lines on stdout."""
import sys
print("section key 'heroX' not in catalog")
print("")           # blank line — must be dropped
print("missing footer nav")
sys.exit(1)
