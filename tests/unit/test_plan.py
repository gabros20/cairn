"""The planner — load → resolve → expand → verify → emit (ARCHITECTURE §2).

Behaviour tests against ``cairn.kernel.plan.plan`` through its public surface: the real
scaffold ``hello`` pipeline plans green with zero executor config; the whole brease-rebuild
pipeline plans green in all three modes with the right nodes dropped/kept/escalated; and a
battery of seeded mistakes each fails fast with a ConfigError naming the file and node.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.plan import (
    GateNode,
    LoopNode,
    ParallelNode,
    StepNode,
    plan,
    render_run_id,
)

NOW = datetime(2026, 7, 3, 11, 4)
TEMPLATE = Path(__file__).parents[2] / "templates" / "workspace"
BREASE_WS = Path(__file__).parent / "fixtures" / "brease-ws"


def _ids(nodes) -> list[str]:
    return [n.id if isinstance(n, StepNode) else n.name for n in nodes]


def _node(plan_obj, ident):
    for n in plan_obj.nodes:
        if (n.id if isinstance(n, StepNode) else n.name) == ident:
            return n
    raise KeyError(ident)


@pytest.fixture
def hello_ws(tmp_path: Path) -> Path:
    """A copy of the real scaffold workspace with the name placeholder substituted."""
    ws = tmp_path / "ws"
    shutil.copytree(TEMPLATE, ws)
    text = (ws / "cairn.toml").read_text(encoding="utf-8").replace("{{WORKSPACE_NAME}}", "test-ws")
    (ws / "cairn.toml").write_text(text, encoding="utf-8")
    return ws


# --------------------------------------------------------------------------- #
# The tracer bullet: the day-0 pipeline plans with zero executor config.
# --------------------------------------------------------------------------- #


def test_hello_plans_green_with_zero_executor_config(hello_ws):
    p = plan(hello_ws, "hello", {}, now=NOW)
    assert _ids(p.nodes) == ["greet", "tone", "compose"]
    assert [type(n).__name__ for n in p.nodes] == ["StepNode", "GateNode", "StepNode"]
    # no agent steps → no executor resolution demanded, no models resolved
    assert p.resolved_models == {}
    assert not [w for w in p.warnings if w.level == "error"]


def test_hello_run_id_renders_from_the_injected_clock(hello_ws):
    p = plan(hello_ws, "hello", {"name": "Ada"}, now=NOW)
    assert render_run_id(p, NOW) == "hello-Ada-20260703"


def test_hello_gate_and_run_shapes(hello_ws):
    p = plan(hello_ws, "hello", {}, now=NOW)
    greet, tone, compose = p.nodes
    assert greet.kind == "run" and "greeting" in greet.produces
    assert isinstance(tone, GateNode) and tone.default == "friendly"
    assert set(compose.needs) == {"greeting", "tone"}


# --------------------------------------------------------------------------- #
# The real pipeline: all three modes plan green.
# --------------------------------------------------------------------------- #


def _brease(mode, **params):
    return plan(BREASE_WS, "brease-rebuild", {"url": "https://acme.com", "mode": mode, **params}, now=NOW)


def test_rebuild_drops_strategy_and_art_review():
    p = _brease("rebuild")
    ids = _ids(p.nodes)
    assert "strategy" not in ids
    assert "art-review" not in ids
    skipped = {s.node for s in p.skipped}
    assert {"strategy", "art-review"} <= skipped
    # rebuild's design=reproduce → no builder escalation
    assert p.resolved_models["build"] == ("claude", "sonnet", "medium")


def test_reimagine_keeps_strategy_and_art_review():
    p = _brease("reimagine")
    ids = _ids(p.nodes)
    assert "strategy" in ids
    assert "art-review" in ids
    loop = _node(p, "art-review")
    assert isinstance(loop, LoopNode)
    assert loop.max_interactive == 3 and loop.max_headless == 2
    assert loop.until is not None  # the until: predicate is a runtime Expr


def test_redesign_escalates_the_builder_tier_to_reasoning():
    p = _brease("redesign")
    # frontend-builder escalate: dims.design != 'reproduce' fires → reasoning tier
    assert p.resolved_models["build"] == ("claude", "opus", "high")
    assert "art-review" in _ids(p.nodes)
    assert "strategy" not in _ids(p.nodes)  # content=keep → strategy still dropped


def test_brease_off_default_plans_green_end_to_end():
    p = _brease("rebuild")
    # the credential branch is entirely dropped when brease=off
    assert "model-cms" not in _ids(p.nodes)
    assert "populate" not in _ids(p.nodes)
    assert not [w for w in p.warnings if w.level == "error"]


def test_brease_on_keeps_the_cms_branch():
    p = _brease("reimagine", brease="on")
    ids = _ids(p.nodes)
    assert {"brease-auth", "model-cms", "populate-approval", "populate"} <= set(ids)
    # populate references a gate root but short-circuit keeps it as a runtime node
    populate = _node(p, "populate")
    assert populate.when_runtime is not None
    assert populate.retry == (0, False)  # CMS mutation: never blind-retry


def test_parallel_blueprint_is_a_concurrent_pair():
    p = _brease("rebuild")
    par = _node(p, "blueprint")
    assert isinstance(par, ParallelNode)
    assert _ids(par.steps) == ["architect", "design-author"]


def test_run_id_bakes_mode_and_slug():
    assert render_run_id(_brease("redesign"), NOW) == "acme-redesign-20260703"
    assert render_run_id(_brease("redesign", variant="v2"), NOW) == "acme-redesign-20260703-v2"


# --------------------------------------------------------------------------- #
# Executor routing.
# --------------------------------------------------------------------------- #


def test_executor_override_and_per_step_pin():
    p = plan(
        BREASE_WS,
        "brease-rebuild",
        {"url": "https://acme.com", "mode": "redesign"},
        executor="codex",
        now=NOW,
    )
    # global override moves agents to codex, but the review step's hard pin stays claude
    assert p.resolved_models["build"] == ("codex", "gpt-5.5", "high")
    assert p.resolved_models["review"] == ("claude", "opus", "high")
    assert _node(p, "build").executor == "codex"


def test_step_executor_map_overrides_default_but_not_pin():
    p = plan(
        BREASE_WS,
        "brease-rebuild",
        {"url": "https://acme.com", "mode": "redesign"},
        step_executors={"build": "grok"},
        now=NOW,
    )
    # grok's reasoning tier omits effort → the agent's effort passes through (ARCHITECTURE
    # §2 / prompt rule: "agent effort passes through unless the tier spec fixes it"). The grok
    # executor later nulls it at invoke time (Invocation.effort None when baked into the alias).
    assert p.resolved_models["build"] == ("grok", "grok-4.3-high", "medium")


# --------------------------------------------------------------------------- #
# --from / --to slicing.
# --------------------------------------------------------------------------- #


def test_to_node_slices_and_scopes_executor_resolution():
    p = plan(BREASE_WS, "brease-rebuild", {"url": "https://acme.com"}, to_node="blueprint", now=NOW)
    assert _ids(p.nodes) == ["discover", "scope", "select-urls", "capture", "audit", "blueprint"]
    # only in-range agent steps demanded executor config
    assert set(p.resolved_models) == {"discover", "capture", "audit", "architect", "design-author"}


def test_from_node_slices_the_tail():
    p = plan(BREASE_WS, "brease-rebuild", {"url": "https://acme.com"}, from_node="build", now=NOW)
    assert _ids(p.nodes)[0] == "build"
    assert "discover" not in _ids(p.nodes)


def test_unknown_to_node_is_a_config_error():
    with pytest.raises(ConfigError) as exc:
        plan(BREASE_WS, "brease-rebuild", {"url": "https://acme.com"}, to_node="nope", now=NOW)
    assert "nope" in str(exc.value)


def test_needs_optional_on_a_plan_dropped_producer_survives():
    # build.needs_optional includes content-map, produced only by the (dropped) populate step.
    p = _brease("rebuild")
    build = _node(p, "build")
    assert "content-map" in build.needs_optional
    assert "content-map" not in _ids(p.nodes)  # its producer is gone, but the plan is valid


# --------------------------------------------------------------------------- #
# The error battery — a purpose-built minimal workspace per failure.
# --------------------------------------------------------------------------- #


def _write_ws(tmp_path: Path, pipeline_yaml: str, *, agents=None, extra=None) -> Path:
    ws = tmp_path / "ws"
    (ws / "pipelines").mkdir(parents=True)
    (ws / "schemas").mkdir()
    (ws / "validators").mkdir()
    (ws / "agents").mkdir()
    (ws / "cairn.toml").write_text(
        '[workspace]\nname = "t"\ndefault_executor = "claude"\n'
        "[executors.claude]\nenabled = true\n[executors.claude.tiers]\n"
        'reasoning = { model = "opus", effort = "high" }\n'
        'balanced = { model = "sonnet", effort = "medium" }\n'
        'cheap = { model = "haiku", effort = "low" }\n'
        "[secrets]\nOK_TOKEN = {}\n",
        encoding="utf-8",
    )
    (ws / "schemas" / "s.json").write_text('{"type":"object"}', encoding="utf-8")
    (ws / "validators" / "v.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (ws / "pipelines" / "p.yaml").write_text(pipeline_yaml, encoding="utf-8")
    for name, body in (agents or {}).items():
        (ws / "agents" / f"{name}.yaml").write_text(body, encoding="utf-8")
    for rel, body in (extra or {}).items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return ws


_HEADER = """pipeline: p
version: 1
params:
  x: { type: string, default: hi }
run_id: "p-{date}"
artifacts:
  a: { path: a.json, schema: schemas/s.json }
  b: { path: b.json, schema: schemas/s.json }
steps:
"""


def _plan_p(tmp_path, steps, **kw):
    ws = _write_ws(tmp_path, _HEADER + steps, **kw)
    return plan(ws, "p", {}, now=NOW)


def test_typod_needs_names_the_step_and_artifact(tmp_path):
    steps = "  - { id: one, run: 'echo', produces: [a] }\n  - { id: two, run: 'echo', needs: [aa], produces: [b] }\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "two" in str(exc.value) and "aa" in str(exc.value)


def test_duplicate_produce_is_an_error(tmp_path):
    steps = "  - { id: one, run: 'echo', produces: [a] }\n  - { id: two, run: 'echo', produces: [a] }\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "produced more than once" in str(exc.value)


def test_unknown_agent_file_is_an_error(tmp_path):
    steps = "  - { id: one, agent: ghost, produces: [a] }\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "ghost" in str(exc.value)


def test_missing_schema_file_is_an_error(tmp_path):
    yaml_text = (
        "pipeline: p\nversion: 1\nrun_id: \"p-{date}\"\n"
        "artifacts:\n  a: { path: a.json, schema: schemas/missing.json }\n"
        "steps:\n  - { id: one, run: 'echo', produces: [a] }\n"
    )
    ws = _write_ws(tmp_path, yaml_text)
    with pytest.raises(ConfigError) as exc:
        plan(ws, "p", {}, now=NOW)
    assert "missing.json" in str(exc.value)


def test_bad_enum_param_is_an_error(tmp_path):
    yaml_text = (
        "pipeline: p\nversion: 1\n"
        "params:\n  mode: { type: enum, values: [a, b], default: a }\n"
        "run_id: \"p-{date}\"\n"
        "artifacts:\n  a: { path: a.json, schema: schemas/s.json }\n"
        "steps:\n  - { id: one, run: 'echo', produces: [a] }\n"
    )
    ws = _write_ws(tmp_path, yaml_text)
    with pytest.raises(ConfigError) as exc:
        plan(ws, "p", {"mode": "zzz"}, now=NOW)
    assert "mode" in str(exc.value) and "zzz" in str(exc.value)


def test_unparseable_when_is_an_error(tmp_path):
    steps = "  - { id: one, run: 'echo', produces: [a], when: 'params.x ==' }\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "one" in str(exc.value)


def test_env_name_not_in_secrets_is_an_error(tmp_path):
    steps = "  - { id: one, agent: worker, produces: [a] }\n"
    agent = "tier: balanced\ntools: { allow: [read] }\nenv: [MISSING_TOKEN]\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps, agents={"worker": agent})
    assert "MISSING_TOKEN" in str(exc.value)


def test_missing_allowlist_fragment_is_an_error(tmp_path):
    steps = "  - { id: one, agent: worker, produces: [a] }\n"
    agent = "tier: balanced\ntools: { allow: [bash], bash: allowlist.yaml#nope }\n"
    extra = {"allowlist.yaml": "real:\n  - 'echo *'\n"}
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps, agents={"worker": agent}, extra=extra)
    assert "nope" in str(exc.value)


def test_artifact_ref_inside_an_artifact_path_is_an_error(tmp_path):
    yaml_text = (
        "pipeline: p\nversion: 1\nrun_id: \"p-{date}\"\n"
        "artifacts:\n"
        "  a: { path: a.json, schema: schemas/s.json }\n"
        "  b: { path: \"b-{artifact:a}.json\", schema: schemas/s.json }\n"
        "steps:\n"
        "  - { id: one, run: 'echo', produces: [a] }\n"
        "  - { id: two, run: 'echo', produces: [b] }\n"
    )
    ws = _write_ws(tmp_path, yaml_text)
    with pytest.raises(ConfigError) as exc:
        plan(ws, "p", {}, now=NOW)
    assert "artifact" in str(exc.value).lower() and "path" in str(exc.value).lower()


def test_cycle_outside_a_loop_is_an_error(tmp_path):
    steps = "  - { id: one, run: 'echo {cycle}', produces: [a] }\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "cycle" in str(exc.value).lower()


def test_parallel_children_must_produce_disjoint_artifacts(tmp_path):
    steps = (
        "  - parallel: pair\n"
        "    steps:\n"
        "      - { id: one, run: 'echo', produces: [a] }\n"
        "      - { id: two, run: 'echo', produces: [a] }\n"
    )
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "disjoint" in str(exc.value)


# --------------------------------------------------------------------------- #
# Warnings (non-fatal).
# --------------------------------------------------------------------------- #


def test_unused_artifact_and_missing_skill_are_warnings(tmp_path):
    yaml_text = (
        "pipeline: p\nversion: 1\nrun_id: \"p-{date}\"\n"
        "artifacts:\n"
        "  a: { path: a.json, schema: schemas/s.json }\n"
        "  orphan: { path: orphan.json, schema: schemas/s.json }\n"
        "steps:\n"
        "  - { id: one, agent: worker, produces: [a] }\n"
    )
    agent = "tier: balanced\nskills: [ghost-skill]\ntools: { allow: [read] }\n"
    ws = _write_ws(tmp_path, yaml_text, agents={"worker": agent})
    p = plan(ws, "p", {}, now=NOW)
    messages = " ".join(w.message for w in p.warnings)
    assert "orphan" in messages  # declared but never produced
    assert "ghost-skill" in messages  # skill dir missing
    # warnings never block: the plan still emits
    assert _ids(p.nodes) == ["one"]
