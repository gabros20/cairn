#!/usr/bin/env python3
"""A guard check that inspects its OWN PROCESS environment (os.environ, not the stdin
payload) and DENIES if a secret-shaped key reached it. The engine must spawn the check
with a filtered process env, so this must ALLOW in practice; a denial proves the check
subprocess inherited the parent's secrets."""
import os
import sys

leaked = sorted(
    k for k in os.environ if k.startswith("BREASE") or k == "SECRET_CANARY"
)
if leaked:
    print(f"secret leaked into check process env: {leaked}", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
