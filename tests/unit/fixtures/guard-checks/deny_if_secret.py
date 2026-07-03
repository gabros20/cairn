#!/usr/bin/env python3
"""A guard check that inspects the ``env`` it was handed on stdin and DENIES if any
non-``CAIRN_*`` key reached it — i.e. it denies iff a secret leaked through. Because
the engine forwards only the ``CAIRN_*`` subset, this check must ALLOW in practice;
a denial proves the safe-subset filter regressed."""
import json
import sys

payload = json.load(sys.stdin)
env = payload.get("env", {})
leaked = sorted(k for k in env if not k.startswith("CAIRN_"))
if leaked:
    print(f"leaked non-cairn env into check: {leaked}", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
