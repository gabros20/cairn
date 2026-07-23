"""Filesystem safety probes for factory watch/ledger dirs (D2 hard refusals).

Queue state is files + atomic rename/hard-link. Watch/ledger dirs under known
cloud-sync roots, or failing a hard-link probe, are a HARD refusal at run/sync
unless ``--unsafe-synced-fs`` is set (logged override). Conflict-copy patterns
are always surfaced as errors.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from cairn.kernel.types import Finding

# Relative-to-home path segments that mark a known cloud-sync root (D2).
# Matched as path components so ``…/Dropbox/…`` and ``…/Library/Mobile Documents/…``
# both hit. Injected ``home`` lets tests avoid depending on the real FS layout.
_CLOUD_SYNC_HOME_SEGMENTS: tuple[tuple[str, ...], ...] = (
    ("Library", "Mobile Documents"),  # iCloud Drive
    ("Dropbox",),
    ("OneDrive",),
    ("Google Drive",),
)

# Absolute path prefixes that are cloud-sync regardless of home (Linux clients etc.).
_CLOUD_SYNC_ABS_PREFIXES: tuple[str, ...] = (
    "/Library/Mobile Documents",
)

# Conflict-copy / placeholder basenames inside a ledger/watch dir.
_CONFLICT_BASENAME_RE = re.compile(
    r"(?:^|.*)"
    r"(?:"
    r".* 2\.json$"  # Finder "foo 2.json"
    r"|.*\.icloud$"  # iCloud placeholder
    r"|.*\.conflict.*$"  # generic conflict marker
    r")",
    re.IGNORECASE,
)


def is_under_cloud_sync(
    path: Path,
    *,
    home: Path | None = None,
    extra_roots: list[Path] | None = None,
) -> str | None:
    """Return a short reason if ``path`` sits under a known cloud-sync root, else None.

    ``home`` defaults to ``Path.home()``; tests inject a fake home. ``extra_roots``
    lets callers extend the match set without depending on real FS layout.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    parts = resolved.parts

    if extra_roots:
        for root in extra_roots:
            try:
                r = Path(root).resolve()
            except OSError:
                r = Path(root)
            if resolved == r or r in resolved.parents:
                return f"under cloud-sync root {r}"

    home_path = Path(home) if home is not None else Path.home()
    try:
        home_resolved = home_path.resolve()
    except OSError:
        home_resolved = home_path

    for segs in _CLOUD_SYNC_HOME_SEGMENTS:
        candidate = home_resolved.joinpath(*segs)
        try:
            cand_r = candidate.resolve()
        except OSError:
            cand_r = candidate
        # Also match by trailing path-component sequence (injectable / non-existent).
        if _parts_contain(parts, segs):
            return f"under cloud-sync root ~/{'/'.join(segs)}"
        if resolved == cand_r or cand_r in resolved.parents:
            return f"under cloud-sync root {cand_r}"

    resolved_s = str(resolved)
    for prefix in _CLOUD_SYNC_ABS_PREFIXES:
        if resolved_s == prefix or resolved_s.startswith(prefix + os.sep):
            return f"under cloud-sync root {prefix}"
    return None


def _parts_contain(parts: tuple[str, ...], segs: tuple[str, ...]) -> bool:
    """True when ``segs`` appears as consecutive components in ``parts``."""
    n = len(segs)
    if n == 0 or n > len(parts):
        return False
    for i in range(len(parts) - n + 1):
        if parts[i : i + n] == segs:
            return True
    return False


def hardlink_probe(directory: Path) -> bool:
    """Return True when hard-links work inside ``directory`` (create/link/unlink temps).

    False on any OSError (EXDEV, EPERM, read-only, etc.). Never leaves residue on success;
    best-effort cleanup on failure.
    """
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    src = dst = None
    try:
        fd, src_s = tempfile.mkstemp(prefix=".cairn-hlprobe-", dir=str(directory))
        os.close(fd)
        src = Path(src_s)
        dst = src.with_name(src.name + ".link")
        os.link(src, dst)
        return True
    except OSError:
        return False
    finally:
        for p in (dst, src):
            if p is not None:
                try:
                    p.unlink()
                except OSError:
                    pass


def find_conflict_copies(watch_abs: Path) -> list[str]:
    """Basenames matching conflict-copy / iCloud placeholder patterns under ledger dirs."""
    watch_abs = Path(watch_abs)
    hits: list[str] = []
    if not watch_abs.is_dir():
        return hits
    # Scan watch root + known ledger subdirs (not a full recursive walk of inbox noise).
    scan_dirs = [watch_abs]
    for name in (
        ".claim",
        ".waiting",
        ".failed",
        ".done",
        ".deferred",
        ".rejected",
        ".ids",
    ):
        d = watch_abs / name
        if d.is_dir():
            scan_dirs.append(d)
        runs = watch_abs / name / ".runs" if name.startswith(".") else None
        if runs is not None and runs.is_dir():
            scan_dirs.append(runs)
    seen: set[str] = set()
    for d in scan_dirs:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.is_file():
                continue
            if _CONFLICT_BASENAME_RE.match(p.name) or _is_conflict_name(p.name):
                rel = str(p.relative_to(watch_abs)) if p.is_relative_to(watch_abs) else p.name
                if rel not in seen:
                    seen.add(rel)
                    hits.append(rel)
    return sorted(hits)


def _is_conflict_name(name: str) -> bool:
    lower = name.lower()
    if lower.endswith(".icloud"):
        return True
    if ".conflict" in lower:
        return True
    # Finder duplicate: "foo 2.json" / "foo 2"
    stem = Path(name).stem
    if re.search(r" \d+$", stem):
        return True
    return False


def check_watch_fs_safety(
    watch_abs: Path,
    *,
    home: Path | None = None,
    extra_roots: list[Path] | None = None,
    skip_hardlink: bool = False,
) -> list[Finding]:
    """Return Findings for cloud-sync / hardlink / conflict-copy problems on ``watch_abs``.

    Cloud-sync and hardlink failures are ``error`` level (hard refusal at run/sync).
    Conflict copies are also ``error`` (surfaced; operator cleans them).
    """
    findings: list[Finding] = []
    reason = is_under_cloud_sync(watch_abs, home=home, extra_roots=extra_roots)
    if reason:
        findings.append(
            Finding(
                "error",
                f"watch dir {watch_abs} is {reason} — queue state requires a real local "
                "filesystem (D2)",
                fix="move the watch/ledger dir off cloud-sync, or pass --unsafe-synced-fs "
                "(logged override; data loss risk)",
            )
        )
    if not skip_hardlink and not hardlink_probe(watch_abs):
        findings.append(
            Finding(
                "error",
                f"hard-link probe failed in {watch_abs} — claim/ledger moves need hard links "
                "(D2)",
                fix="use a real local filesystem (not cloud-sync / network FS), or pass "
                "--unsafe-synced-fs (logged override; data loss risk)",
            )
        )
    for hit in find_conflict_copies(watch_abs):
        findings.append(
            Finding(
                "error",
                f"conflict-copy / cloud placeholder in ledger: {hit}",
                fix="remove the conflict copy; re-sync cloud clients away from the watch dir",
            )
        )
    return findings
