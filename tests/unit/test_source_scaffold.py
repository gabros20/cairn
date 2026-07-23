"""W4-T3 — ``cairn new source`` scaffolds + GitHub reference adapter.

No live network: provider API calls are seamed/mocked. Covers generator file
set, plan validity, triggers ship-gate defaults, pure pull/notify logic, and
unknown/existing-file refusals.
"""

from __future__ import annotations

import importlib.util
import json
import py_compile
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from cairn.cli import main
from cairn.kernel import newkit, sourcekit
from cairn.kernel.plan import plan as build_plan
from cairn.kernel.queue_ledger import parse_item_name
from cairn.kernel.trigger_host import load_triggers
from cairn.kernel.types import ExitCode
from cairn.kernel.work_item import safe_item_id, work_item_rev

REPO = Path(__file__).parents[2]
NOW = datetime(2026, 7, 3, tzinfo=timezone.utc)
TEMPLATE_SCHEMAS = REPO / "templates" / "workspace" / "schemas"
TEMPLATE_VALIDATORS = REPO / "templates" / "workspace" / "validators"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_module(path: Path, name: str):
    """Import a generated script by path (hyphenated filenames OK)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return newkit.new_workspace("sourcews", tmp_path)


@pytest.fixture
def github_ws(ws: Path) -> Path:
    sourcekit.new_source("github-issues", ws)
    return ws


# --------------------------------------------------------------------------- #
# Generator — files, refusals, CLI
# --------------------------------------------------------------------------- #


def test_new_source_github_issues_creates_expected_files(ws: Path):
    result = sourcekit.new_source("github-issues", ws)
    expected = {
        "pipelines/pull-github-issues.yaml",
        "pipelines/fix-github-issues.yaml",
        "scripts/pull_github-issues.py",
        "scripts/refresh_github-issues.py",
        "scripts/notify_github-issues.py",
    }
    assert set(result.files) == expected
    for rel in expected:
        assert (ws / rel).is_file(), rel
    assert (ws / "work" / "inbox").is_dir()
    assert (ws / "state").is_dir()
    # Triggers/schedules are NOT auto-written (print-only, self-improve posture).
    assert not (ws / "triggers.yaml").exists()
    assert not (ws / "schedules.yaml").exists()
    assert "identity: strict" in result.triggers_snippet
    assert "lease: 60m" in result.triggers_snippet
    assert "pull-github-issues" in result.schedules_snippet


def test_new_source_seam_providers_have_todo_marker(ws: Path, tmp_path: Path):
    for provider in ("linear", "jira", "notion"):
        w = newkit.new_workspace(provider.replace("-", ""), tmp_path / provider)
        result = sourcekit.new_source(provider, w)
        pull = (w / f"scripts/pull_{provider}.py").read_text(encoding="utf-8")
        notify = (w / f"scripts/notify_{provider}.py").read_text(encoding="utf-8")
        assert f"TODO: your {provider} API call here" in pull
        assert "safe_item_id" in pull  # seam docs + import — id sanitization is kernel-side
        assert "uncertain" in notify  # fail-closed default until seam filled
        assert any("scaffold seam" in n for n in result.notes)


@pytest.mark.parametrize("provider", list(sourcekit.KNOWN_PROVIDERS))
def test_all_providers_compile_and_plan(tmp_path: Path, provider: str):
    """I1: every provider's generated .py compiles and every pipeline plans.

    Catches embedded-template syntax rot for linear/jira/notion (75% of surface)
    without requiring a templates/<kind>/ tree migration.
    """
    w = newkit.new_workspace(f"ws-{provider}", tmp_path / provider)
    sourcekit.new_source(provider, w)
    scripts = list((w / "scripts").glob(f"*_{provider}.py"))
    assert len(scripts) == 3, scripts
    for script in scripts:
        py_compile.compile(str(script), doraise=True)
    build_plan(w, f"pull-{provider}", {}, now=NOW)
    build_plan(w, f"fix-{provider}", {"event": "/tmp/item.json"}, now=NOW)


def test_unknown_provider_refused(ws: Path):
    with pytest.raises(ValueError, match="unknown source provider"):
        sourcekit.new_source("bitbucket", ws)


def test_existing_file_refused(github_ws: Path):
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        sourcekit.new_source("github-issues", github_ws)


def test_cli_new_source_prints_snippets(ws: Path, monkeypatch, capsys):
    monkeypatch.chdir(ws)
    assert main(["new", "source", "github-issues"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "pipelines/pull-github-issues.yaml" in out
    assert "identity: strict" in out
    assert "lease: 60m" in out
    assert "schedules.yaml" in out
    assert "SG6" in out or "quarantine" in out.lower()
    assert "worktree" in out.lower()


def test_cli_unknown_provider_config_exit(ws: Path, monkeypatch, capsys):
    monkeypatch.chdir(ws)
    assert main(["new", "source", "bitbucket"]) == int(ExitCode.CONFIG)
    err = capsys.readouterr().err
    assert "unknown source provider" in err


# --------------------------------------------------------------------------- #
# Plan validity + triggers ship-gates
# --------------------------------------------------------------------------- #


def test_generated_pipelines_plan_valid(github_ws: Path):
    pull = build_plan(github_ws, "pull-github-issues", {}, now=NOW)
    assert pull.pipeline == "pull-github-issues"
    # cursor: on the poll step
    poll = next(n for n in pull.nodes if getattr(n, "id", None) == "poll" or getattr(n, "name", None) == "poll")
    # StepNode uses .id and .cursor
    assert getattr(poll, "cursor", None) == "state/github-issues.cursor" or any(
        getattr(n, "cursor", None) for n in pull.nodes
    )

    fix = build_plan(
        github_ws, "fix-github-issues", {"event": "/tmp/item.json"}, now=NOW
    )
    assert fix.pipeline == "fix-github-issues"
    art_names = set(fix.artifacts)
    assert {"work-item", "source-status", "delivery"} <= art_names


def test_cli_plan_pull_and_fix(github_ws: Path, monkeypatch, capsys):
    monkeypatch.chdir(github_ws)
    assert main(["plan", "pull-github-issues"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "pull-github-issues" in out
    assert "poll" in out

    assert main(["plan", "fix-github-issues", "--param", "event=/tmp/x.json"]) == int(
        ExitCode.OK
    )
    out = capsys.readouterr().out
    assert "fix-github-issues" in out
    assert "source-status" in out


def test_triggers_snippet_parses_with_sg1_sg4(github_ws: Path, ws: Path):
    result = sourcekit.SourceScaffoldResult(
        provider="github-issues",
        files=(),
        triggers_snippet=sourcekit._triggers_snippet(
            sourcekit.provider_spec("github-issues")
        ),
        schedules_snippet="",
        notes=(),
    )
    # Strip comment-only fencing lines; keep the YAML mapping.
    lines = [
        ln
        for ln in result.triggers_snippet.splitlines()
        if not ln.strip().startswith("#")
    ]
    (github_ws / "triggers.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    triggers = load_triggers(github_ws)
    assert "fix-github-issues" in triggers
    t = triggers["fix-github-issues"]
    assert t.identity == "strict"  # SG1
    assert t.lease_ttl_s == 3600  # SG4 explicit 60m
    assert t.inbox_max == 50
    assert t.pipeline == "fix-github-issues"
    assert t.watch == "work/inbox/"
    assert t.order == "aged"


# --------------------------------------------------------------------------- #
# Poll-report / work-item / source-status wiring
# --------------------------------------------------------------------------- #


def test_pull_pipeline_wires_poll_report_validator(github_ws: Path):
    doc = yaml.safe_load(
        (github_ws / "pipelines/pull-github-issues.yaml").read_text(encoding="utf-8")
    )
    pr = doc["artifacts"]["poll-report"]
    assert pr["schema"] == "schemas/poll-report.json"
    assert pr["validator"] == "validators/poll-report-complete.py"
    poll = next(s for s in doc["steps"] if s.get("step") == "poll")
    assert poll["cursor"] == "state/github-issues.cursor"
    assert poll["produces"] == ["poll-report"]


def test_fix_pipeline_wires_source_status(github_ws: Path):
    doc = yaml.safe_load(
        (github_ws / "pipelines/fix-github-issues.yaml").read_text(encoding="utf-8")
    )
    ss = doc["artifacts"]["source-status"]
    assert ss["schema"] == "schemas/source-status.json"
    assert ss["validator"] == "validators/source-status.py"
    # closed routes to cancel-closed; work/deliver skip
    work = next(s for s in doc["steps"] if s.get("step") == "work")
    deliver = next(s for s in doc["steps"] if s.get("step") == "deliver")
    cancel = next(s for s in doc["steps"] if s.get("step") == "cancel-closed")
    assert "closed" in work["when"]
    assert "closed" in deliver["when"]
    assert "closed" in cancel["when"]


def test_paused_poll_report_validates_complete_true(github_ws: Path, tmp_path: Path):
    """Backpressure pause must pass the completeness validator (exit 0, no advance)."""
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_gh"
    )
    report = pull.paused_poll_report("github", {"updated_at": "2024-01-01T00:00:00Z", "id": "1"})
    assert report["complete"] is True
    assert report["paused"] is True
    assert report["emitted"] == 0
    # Validator must accept complete:true
    path = tmp_path / "poll-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    res = subprocess.run(
        [
            sys.executable,
            str(TEMPLATE_VALIDATORS / "poll-report-complete.py"),
            str(tmp_path),
            "poll-report",
            "poll-report.json",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------- #
# Pure pull logic (seamed fetch — no network)
# --------------------------------------------------------------------------- #


def test_work_item_filename_and_rev_identity_strict(github_ws: Path):
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_fn"
    )
    name, body = pull.build_work_item(
        item_id="42",
        title="Fix me",
        url="https://example.com/42",
        created="2024-01-15T12:00:00Z",
        updated_at="2024-01-15T12:00:00Z",
        payload={"n": 42},
        prio=3,
        source="github",
        rev_fn=work_item_rev,
    )
    item = parse_item_name(name)
    assert item is not None
    assert item.prio == 3
    assert item.source == "github"
    assert item.id == "42"
    assert body["rev"] == item.rev
    assert body["prio"] == 3
    assert body["source"] == "github"
    assert name == item.filename


def test_backpressure_skips_poll_without_fetch(github_ws: Path, tmp_path: Path):
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_bp"
    )
    workspace = tmp_path / "ws"
    inbox = workspace / "work" / "inbox"
    inbox.mkdir(parents=True)
    # Fill inbox to cap
    for i in range(3):
        (inbox / f"p5-github-item{i}-r0001705320000000000.json").write_text("{}", encoding="utf-8")

    called = {"n": 0}

    def boom(**kwargs):
        called["n"] += 1
        raise AssertionError("fetch must not be called under backpressure")

    report_path = tmp_path / "report.json"
    cursor_next = tmp_path / "cursor.next"
    rc = pull.run_pull(
        workspace=workspace,
        cursor_value='{"updated_at":"2024-01-01T00:00:00Z","id":"1"}',
        cursor_next=cursor_next,
        poll_report_path=report_path,
        inbox_max=3,
        fetch=boom,
        environ={"GH_TOKEN": "test-token-not-real"},
    )
    assert rc == 0
    assert called["n"] == 0
    assert not cursor_next.exists()  # watermark untouched
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["paused"] is True
    assert report["complete"] is True
    assert report["emitted"] == 0


def test_run_pull_emits_work_items_via_seamed_fetch(github_ws: Path, tmp_path: Path):
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_emit"
    )
    workspace = tmp_path / "ws"
    (workspace / "work" / "inbox").mkdir(parents=True)

    def fake_fetch(*, since, cursor, environ=None, runner=None):
        return [
            {
                "id": "7",
                "title": "Hello",
                "url": "https://example.com/7",
                "created": "2024-06-01T00:00:00Z",
                "updated_at": "2024-06-01T08:00:00Z",
                "prio": 2,
                "payload": {"state": "OPEN"},
            }
        ]

    report_path = tmp_path / "report.json"
    cursor_next = tmp_path / "cursor.next"
    rc = pull.run_pull(
        workspace=workspace,
        cursor_value="",
        cursor_next=cursor_next,
        poll_report_path=report_path,
        inbox_max=50,
        fetch=fake_fetch,
        rev_fn=work_item_rev,
        environ={"GH_TOKEN": "test-token-not-real"},
    )
    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["emitted"] == 1
    assert report["source"] == "github"
    files = list((workspace / "work" / "inbox").glob("*.json"))
    assert len(files) == 1
    item = parse_item_name(files[0].name)
    assert item is not None and item.id == "7"
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["title"] == "Hello"
    assert cursor_next.is_file()
    cur = json.loads(cursor_next.read_text(encoding="utf-8"))
    assert cur["id"] == "7"


def test_hostile_upstream_id_sanitized_not_traversal_or_wedge(
    github_ws: Path, tmp_path: Path
):
    """C1 repro: seamed fetch with ../../state/pwned must not traverse or crash."""
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_hostile"
    )
    workspace = tmp_path / "ws"
    inbox = workspace / "work" / "inbox"
    inbox.mkdir(parents=True)
    state = workspace / "state"
    state.mkdir(parents=True)
    # Sentinel: if traversal writes here, the test fails.
    (state / "pwned").write_text("untouched\n", encoding="utf-8")

    def hostile_fetch(*, since, cursor, environ=None, runner=None):
        return [
            {
                "id": "../../state/pwned",
                "title": "evil",
                "url": "x",
                "created": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "prio": 5,
                "payload": {},
            },
            {
                "id": "good-1",
                "title": "ok",
                "url": "y",
                "created": "2024-01-02T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
                "prio": 3,
                "payload": {},
            },
            {
                "id": "///",  # unsalvageable → skip, not wedge
                "title": "poison",
                "url": "z",
                "created": "2024-01-03T00:00:00Z",
                "updated_at": "2024-01-03T00:00:00Z",
                "prio": 1,
                "payload": {},
            },
        ]

    report_path = tmp_path / "report.json"
    cursor_next = tmp_path / "cursor.next"
    rc = pull.run_pull(
        workspace=workspace,
        cursor_value="",
        cursor_next=cursor_next,
        poll_report_path=report_path,
        fetch=hostile_fetch,
        rev_fn=work_item_rev,
        environ={"GH_TOKEN": "t"},
    )
    assert rc == 0
    # No path traversal into state/
    assert (state / "pwned").read_text(encoding="utf-8") == "untouched\n"
    # Files only under inbox, no slashes in basenames
    written = list(inbox.glob("*.json"))
    assert all("/" not in p.name for p in written)
    names = {p.name for p in written}
    # Hostile id salvaged to state-pwned; good-1 emitted; /// skipped
    assert any("state-pwned" in n for n in names)
    assert any("good-1" in n for n in names)
    for p in written:
        assert parse_item_name(p.name) is not None
        assert p.resolve().is_relative_to(inbox.resolve())
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["emitted"] >= 2
    assert report.get("skipped", 0) >= 1  # /// unsalvageable
    assert cursor_next.is_file()  # pull not wedged — cursor advances past good items


def test_id_collision_increments_skipped_with_reason(
    github_ws: Path, tmp_path: Path
):
    """Two raw upstream ids that sanitize to the same token must not be silent."""
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_coll"
    )
    workspace = tmp_path / "ws"
    (workspace / "work" / "inbox").mkdir(parents=True)

    # Foo-1 and foo--1 both sanitize to foo-1 (same dest when rev/prio match).
    assert safe_item_id("Foo-1") == safe_item_id("foo--1") == "foo-1"
    ts = "2024-06-01T00:00:00Z"

    def collide_fetch(*, since, cursor, environ=None, runner=None):
        return [
            {
                "id": "Foo-1",
                "title": "first",
                "url": "u1",
                "created": ts,
                "updated_at": ts,
                "prio": 5,
                "payload": {},
            },
            {
                "id": "foo--1",
                "title": "second-collides",
                "url": "u2",
                "created": ts,
                "updated_at": ts,
                "prio": 5,
                "payload": {},
            },
            {
                "id": "other-9",
                "title": "unrelated",
                "url": "u3",
                "created": ts,
                "updated_at": ts,
                "prio": 3,
                "payload": {},
            },
        ]

    report_path = tmp_path / "report.json"
    cursor_next = tmp_path / "cursor.next"
    rc = pull.run_pull(
        workspace=workspace,
        cursor_value="",
        cursor_next=cursor_next,
        poll_report_path=report_path,
        fetch=collide_fetch,
        rev_fn=work_item_rev,
        environ={"GH_TOKEN": "t"},
    )
    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["emitted"] == 2  # Foo-1 + other-9
    assert report.get("skipped", 0) == 1
    reasons = report.get("skip_reasons") or []
    assert len(reasons) == 1
    assert "id-collision" in reasons[0]
    assert "foo--1" in reasons[0]
    assert "Foo-1" in reasons[0]
    # Safety unchanged: first writer wins; no overwrite of first body.
    inbox = workspace / "work" / "inbox"
    collided = list(inbox.glob("*foo-1*.json"))
    assert len(collided) == 1
    body = json.loads(collided[0].read_text(encoding="utf-8"))
    assert body["title"] == "first"


def test_overlap_since_rewinds_watermark(github_ws: Path):
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_ov"
    )
    since = pull.overlap_since(
        {"updated_at": "2024-01-15T12:00:00Z", "id": "9"}, overlap_seconds=60
    )
    assert since is not None
    assert since < "2024-01-15T12:00:00Z" or since.endswith("11:59:00Z")
    assert pull.overlap_since(None) is None


def test_fetch_failure_emits_incomplete_report(github_ws: Path, tmp_path: Path):
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_fail"
    )
    workspace = tmp_path / "ws"
    (workspace / "work" / "inbox").mkdir(parents=True)

    def fail_fetch(**kwargs):
        raise RuntimeError("rate limited")

    report_path = tmp_path / "report.json"
    cursor_next = tmp_path / "cursor.next"
    rc = pull.run_pull(
        workspace=workspace,
        cursor_value='{"updated_at":"2024-01-01T00:00:00Z","id":"1"}',
        cursor_next=cursor_next,
        poll_report_path=report_path,
        fetch=fail_fetch,
        environ={"GH_TOKEN": "t"},
    )
    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"] is False
    assert not cursor_next.exists()


# --------------------------------------------------------------------------- #
# Notify pure logic — fail-closed markers (SG5)
# --------------------------------------------------------------------------- #


def test_idempotency_marker_stable(github_ws: Path):
    notify = _load_module(
        github_ws / "scripts/notify_github-issues.py", "cairn_test_notify_m"
    )
    m1 = notify.idempotency_marker("github", "42", "0001705320000000000")
    m2 = notify.idempotency_marker("github", "42", "r0001705320000000000")
    assert m1 == m2
    assert m1.startswith("cairn-delivery-github-42-r")
    assert notify.identity_key("GitHub", "42") == "github-42"


def test_decide_write_back_fail_closed(github_ws: Path):
    notify = _load_module(
        github_ws / "scripts/notify_github-issues.py", "cairn_test_notify_d"
    )
    assert notify.decide_write_back("absent") == "create"
    assert notify.decide_write_back("present") == "reuse"
    assert notify.decide_write_back("uncertain") == "block"


def test_notify_uncertain_exits_blocked_never_creates(github_ws: Path, tmp_path: Path):
    notify = _load_module(
        github_ws / "scripts/notify_github-issues.py", "cairn_test_notify_u"
    )
    wi = tmp_path / "wi.json"
    rev = work_item_rev("2024-01-15T12:00:00Z")[1:]
    wi.write_text(
        json.dumps(
            {
                "id": "42",
                "source": "github",
                "title": "t",
                "url": "",
                "prio": 5,
                "created": "2024-01-15T12:00:00Z",
                "updated_at": "2024-01-15T12:00:00Z",
                "rev": rev,
                "payload": {},
            }
        ),
        encoding="utf-8",
    )
    delivery = tmp_path / "delivery.json"
    created = {"n": 0}

    def find_uncertain(**kwargs):
        return "uncertain"

    def create_boom(**kwargs):
        created["n"] += 1
        raise AssertionError("must not create on uncertainty")

    rc = notify.run_notify(
        wi,
        delivery,
        find=find_uncertain,
        create=create_boom,
        environ={"GH_TOKEN": "t"},
    )
    assert rc == 9  # BLOCKED
    assert created["n"] == 0
    assert not delivery.exists()


def test_notify_absent_creates_once(github_ws: Path, tmp_path: Path):
    notify = _load_module(
        github_ws / "scripts/notify_github-issues.py", "cairn_test_notify_c"
    )
    wi = tmp_path / "wi.json"
    rev = work_item_rev("2024-01-15T12:00:00Z")[1:]
    wi.write_text(
        json.dumps(
            {
                "id": "9",
                "source": "github",
                "title": "Ship it",
                "url": "",
                "prio": 5,
                "created": "2024-01-15T12:00:00Z",
                "updated_at": "2024-01-15T12:00:00Z",
                "rev": rev,
                "payload": {},
            }
        ),
        encoding="utf-8",
    )
    delivery = tmp_path / "delivery.json"

    def find_absent(**kwargs):
        return "absent"

    def create_ok(*, marker, work_item, environ=None):
        return {"effect": "issue_comment", "url": "https://example.com/c/1"}

    rc = notify.run_notify(
        wi, delivery, find=find_absent, create=create_ok, environ={"GH_TOKEN": "t"}
    )
    assert rc == 0
    receipt = json.loads(delivery.read_text(encoding="utf-8"))
    assert receipt["status"] == "created"
    assert "cairn-delivery-github-9-r" in receipt["marker"]


def test_notify_present_reuses(github_ws: Path, tmp_path: Path):
    notify = _load_module(
        github_ws / "scripts/notify_github-issues.py", "cairn_test_notify_r"
    )
    wi = tmp_path / "wi.json"
    rev = work_item_rev("2024-01-15T12:00:00Z")[1:]
    wi.write_text(
        json.dumps(
            {
                "id": "9",
                "source": "github",
                "title": "Ship it",
                "url": "",
                "prio": 5,
                "created": "2024-01-15T12:00:00Z",
                "updated_at": "2024-01-15T12:00:00Z",
                "rev": rev,
                "payload": {},
            }
        ),
        encoding="utf-8",
    )
    delivery = tmp_path / "delivery.json"

    rc = notify.run_notify(
        wi,
        delivery,
        find=lambda **k: "present",
        create=lambda **k: (_ for _ in ()).throw(AssertionError("no create")),
        environ={"GH_TOKEN": "t"},
    )
    assert rc == 0
    assert json.loads(delivery.read_text())["status"] == "reused"


# --------------------------------------------------------------------------- #
# Refresh T6b classifier
# --------------------------------------------------------------------------- #


def test_refresh_classify_status(github_ws: Path):
    refresh = _load_module(
        github_ws / "scripts/refresh_github-issues.py", "cairn_test_refresh"
    )
    assert refresh.classify_status("aaa", upstream_rev=None, closed=True)["status"] == "closed"
    assert refresh.classify_status("aaa", upstream_rev="aaa", closed=False)["status"] == "current"
    changed = refresh.classify_status("aaa", upstream_rev="bbb", closed=False)
    assert changed["status"] == "changed"
    assert changed["upstream_rev"] == "bbb"


def test_refresh_run_with_seamed_lookup(github_ws: Path, tmp_path: Path):
    refresh = _load_module(
        github_ws / "scripts/refresh_github-issues.py", "cairn_test_refresh_run"
    )
    rev = work_item_rev("2024-01-15T12:00:00Z")[1:]
    wi = tmp_path / "wi.json"
    wi.write_text(
        json.dumps({"id": "1", "rev": rev, "source": "github"}),
        encoding="utf-8",
    )
    out = tmp_path / "status.json"
    rc = refresh.run_refresh(
        wi,
        out,
        lookup=lambda **k: {"rev": rev, "closed": False},
        environ={"GH_TOKEN": "t"},
    )
    assert rc == 0
    assert json.loads(out.read_text())["status"] == "current"


def test_github_scripts_never_hardcode_token(github_ws: Path):
    for rel in (
        "scripts/pull_github-issues.py",
        "scripts/notify_github-issues.py",
        "scripts/refresh_github-issues.py",
    ):
        text = (github_ws / rel).read_text(encoding="utf-8")
        assert "ghp_" not in text
        assert "sk-" not in text
        assert "GH_TOKEN" in text
        # Values come from os.environ / environ mapping only.
        assert "os.environ" in text or "environ" in text


def test_github_scripts_use_gh_timeout(github_ws: Path):
    """Minor: hung gh → timeout; find path treats it as uncertain → BLOCKED."""
    pull = (github_ws / "scripts/pull_github-issues.py").read_text(encoding="utf-8")
    notify = (github_ws / "scripts/notify_github-issues.py").read_text(encoding="utf-8")
    assert "timeout=" in pull and "GH_TIMEOUT" in pull
    assert "timeout=" in notify
    assert "TimeoutExpired" in pull or "timeout" in pull.lower()
    # PR-lookup path removed — create_effect only issues comments.
    assert "gh pr list" not in notify
    assert "issue" in notify and "comment" in notify


def test_generated_pull_imports_kernel_safe_item_id(github_ws: Path):
    text = (github_ws / "scripts/pull_github-issues.py").read_text(encoding="utf-8")
    assert "from cairn.kernel.work_item import" in text
    assert "safe_item_id" in text
    assert "work_item_filename" in text
    # No hand-rolled .lower()-only filename builder left as the authority.
    pull = _load_module(
        github_ws / "scripts/pull_github-issues.py", "cairn_test_pull_import"
    )
    assert pull.safe_item_id is safe_item_id or pull.safe_item_id("../../x") == safe_item_id(
        "../../x"
    )
