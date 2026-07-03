#!/usr/bin/env python3
"""A guard check that hangs well past any test timeout, exercising the timeout path
(an ERROR outcome resolved via ``on_error``)."""
import time

time.sleep(60)
