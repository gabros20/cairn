#!/usr/bin/env python
"""Fails (exit 1) echoing argv[3] — proves the rendered artifact_path is passed through."""
import sys
print(f"argv3={sys.argv[3]}")
sys.exit(1)
