"""W0.5 three-way split: facade re-exports, layering, and object identity."""

from __future__ import annotations

import ast
from pathlib import Path

import cairn.kernel.queue_drain as queue_drain
import cairn.kernel.queue_ledger as queue_ledger
import cairn.kernel.trigger_host as trigger_host
import cairn.kernel.triggerkit as triggerkit

# Every name the facade must keep working for existing importers (task-T5 brief).
_FACADE_PUBLIC = (
    "Trigger",
    "load_triggers",
    "watch_dir",
    "scan_candidates",
    "claim",
    "consume",
    "stuck_claims",
    "sync_triggers",
    "remove_trigger",
    "list_installed_triggers",
    "run_trigger",
    "trigger_launchd_label",
    "trigger_systemd_unit_names",
    "render_trigger_launchd",
    "render_trigger_systemd",
    "TriggerStatus",
)

# name → home module for identity checks (public functions/classes only).
_HOME = {
    "Trigger": trigger_host,
    "load_triggers": trigger_host,
    "watch_dir": trigger_host,
    "scan_candidates": queue_ledger,
    "claim": queue_ledger,
    "consume": queue_ledger,
    "stuck_claims": queue_ledger,
    "sync_triggers": trigger_host,
    "remove_trigger": trigger_host,
    "list_installed_triggers": trigger_host,
    "run_trigger": queue_drain,
    "trigger_launchd_label": trigger_host,
    "trigger_systemd_unit_names": trigger_host,
    "render_trigger_launchd": trigger_host,
    "render_trigger_systemd": trigger_host,
    "TriggerStatus": trigger_host,
}


def test_facade_exports_every_public_name():
    for name in _FACADE_PUBLIC:
        assert hasattr(triggerkit, name), f"facade missing {name}"
        assert getattr(triggerkit, name) is not None


def _imported_modules(source: str) -> set[str]:
    """Module names referenced by import / import-from statements in ``source``."""
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            # bare relative ``from . import name`` — record the bound name
            if node.level and not node.module:
                for alias in node.names:
                    imported.add(alias.name)
    return imported


def test_queue_ledger_imports_neither_sibling():
    """queue_ledger is the bottom layer: pure paths in/out, no host/drain deps.

    Inspect source (AST) rather than reloading into sys.modules — a reload would
    replace module objects and break other tests that already hold function
    references from the original modules.
    """
    src = Path(queue_ledger.__file__).read_text(encoding="utf-8")
    imported = _imported_modules(src)

    # Absolute and relative forms both forbidden.
    forbidden_substrings = ("trigger_host", "queue_drain")
    offenders = {
        name
        for name in imported
        if any(part in name.split(".") for part in forbidden_substrings)
        or name in forbidden_substrings
    }
    assert not offenders, f"queue_ledger imports siblings: {offenders}"

    # Belt: the source text itself must not mention either sibling as a dependency.
    # (Docstrings may say "must not import X" — only care about import lines.)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            chunk = ast.get_source_segment(src, node) or ""
            assert "trigger_host" not in chunk, chunk
            assert "queue_drain" not in chunk, chunk

    # Runtime __dict__: no live binding to either sibling module object.
    for key, value in vars(queue_ledger).items():
        if key.startswith("__"):
            continue
        modname = getattr(value, "__name__", "") or ""
        assert modname not in {
            "cairn.kernel.trigger_host",
            "cairn.kernel.queue_drain",
        }, f"queue_ledger.__dict__[{key!r}] is sibling module"


def test_trigger_host_does_not_import_queue_drain():
    """trigger_host must not import queue_drain (would cycle: host → drain → host).

    Mirror of test_queue_ledger_imports_neither_sibling for the middle layer's
    one-way constraint (trigger_host.py module docstring; review-T5-quality-r1).
    """
    src = Path(trigger_host.__file__).read_text(encoding="utf-8")
    imported = _imported_modules(src)

    forbidden = "queue_drain"
    offenders = {
        name
        for name in imported
        if forbidden in name.split(".") or name == forbidden
    }
    assert not offenders, f"trigger_host imports queue_drain: {offenders}"

    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            chunk = ast.get_source_segment(src, node) or ""
            assert "queue_drain" not in chunk, chunk

    for key, value in vars(trigger_host).items():
        if key.startswith("__"):
            continue
        modname = getattr(value, "__name__", "") or ""
        assert modname != "cairn.kernel.queue_drain", (
            f"trigger_host.__dict__[{key!r}] is queue_drain module"
        )


def test_facade_and_home_module_share_object_identity():
    for name, home in _HOME.items():
        facade_obj = getattr(triggerkit, name)
        home_obj = getattr(home, name)
        assert facade_obj is home_obj, f"{name}: facade is not home module object"
