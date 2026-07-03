"""The stable surface for executor authors.

Third-party executors implement the :class:`Executor` protocol and register under the
``cairn.executors`` entry point. Everything an author needs is re-exported here so they
import from one place and never reach into kernel internals.
"""

from __future__ import annotations

from cairn.kernel.types import (
    EFFORTS,
    TIERS,
    Capabilities,
    Executor,
    Finding,
    Invocation,
    Result,
)

__all__ = [
    "Executor",
    "Capabilities",
    "Invocation",
    "Result",
    "Finding",
    "TIERS",
    "EFFORTS",
]
