"""Load the JSON Schemas bundled with the kernel.

The schemas live under ``cairn/resources/schemas/`` and are read via
``importlib.resources`` so they resolve whether cairn is run from a source
checkout or an installed wheel.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

_PACKAGE = "cairn.resources.schemas"


def get_schema(name: str) -> dict[str, Any]:
    """Return the bundled schema registered under ``name`` (e.g. ``"step-return"``, ``"run"``).

    Raises FileNotFoundError for an unknown name.
    """
    resource = resources.files(_PACKAGE).joinpath(f"{name}.schema.json")
    if not resource.is_file():
        raise FileNotFoundError(f"no bundled schema named {name!r}")
    return json.loads(resource.read_text(encoding="utf-8"))
