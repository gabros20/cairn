#!/usr/bin/env python3
"""A guard check that neither allows (0) nor denies (2): it crashes with exit 1.
Any outcome that is not a clean 0/2 is an ERROR outcome the engine resolves via
the guard's ``on_error`` policy."""
import sys

print("traceback: something went wrong", file=sys.stderr)
sys.exit(1)
