#!/usr/bin/env python3
"""A guard check that denies every command (exit 2). Emits several stderr lines;
the engine reports only the LAST one as the reason."""
import sys

print("guard note: evaluating command", file=sys.stderr)
print("blocked: screenshot may not become media (F18)", file=sys.stderr)
sys.exit(2)
