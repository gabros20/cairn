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
    # frontend-builder escalate: dims.design != 'reproduce' fires → reasoning tier (opus).
    # Escalation swaps the tier (sonnet → opus) but the agent's pinned effort: medium wins
    # over the reasoning tier's effort=high (agent effort beats tier effort).
    assert p.resolved_models["build"] == ("claude", "opus", "medium")
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
    # global override moves agents to codex, but the review step's hard pin stays claude.
    # frontend-builder pins effort: medium in its frontmatter, which now wins over codex's
    # reasoning-tier effort=high (agent effort beats tier effort — specific over general).
    assert p.resolved_models["build"] == ("codex", "gpt-5.5", "medium")
    # design-director pins effort: high, matching claude's reasoning tier — high either way.
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
    # grok's reasoning tier omits effort → the agent's effort applies (medium). The grok
    # executor passes it natively via --effort (grok 0.2.82).
    assert p.resolved_models["build"] == ("grok", "grok-4.3-high", "medium")


# --------------------------------------------------------------------------- #
# Effort precedence: agent frontmatter wins, tier spec is the fallback, executor
# default last (specific-over-general — a per-agent `effort:` is never silently
# overridden by a tier-baked effort).
# --------------------------------------------------------------------------- #


def _effort_ws(tmp_path: Path, *, agent_effort: str | None, tier_effort: str | None) -> Path:
    ws = tmp_path / "ws"
    (ws / "pipelines").mkdir(parents=True)
    (ws / "schemas").mkdir()
    (ws / "agents").mkdir()
    tier = (
        f'balanced = {{ model = "sonnet", effort = "{tier_effort}" }}'
        if tier_effort
        else 'balanced = { model = "sonnet" }'
    )
    (ws / "cairn.toml").write_text(
        '[workspace]\nname = "t"\ndefault_executor = "claude"\n'
        "[executors.claude]\nenabled = true\n[executors.claude.tiers]\n" + tier + "\n",
        encoding="utf-8",
    )
    (ws / "schemas" / "s.json").write_text('{"type":"object"}', encoding="utf-8")
    agent_line = f"effort: {agent_effort}\n" if agent_effort else ""
    (ws / "agents" / "worker.yaml").write_text(
        "tier: balanced\n" + agent_line + "tools: { allow: [read] }\n", encoding="utf-8"
    )
    (ws / "pipelines" / "p.yaml").write_text(
        'pipeline: p\nversion: 1\nrun_id: "p-{date}"\n'
        "artifacts:\n  a: { path: a.json, schema: schemas/s.json }\n"
        "steps:\n  - { id: one, agent: worker, produces: [a] }\n",
        encoding="utf-8",
    )
    return ws


def test_agent_effort_wins_over_tier_effort(tmp_path):
    ws = _effort_ws(tmp_path, agent_effort="high", tier_effort="medium")
    p = plan(ws, "p", {}, now=NOW)
    assert p.resolved_models["one"] == ("claude", "sonnet", "high")


def test_tier_effort_applies_when_agent_omits_it(tmp_path):
    ws = _effort_ws(tmp_path, agent_effort=None, tier_effort="medium")
    p = plan(ws, "p", {}, now=NOW)
    assert p.resolved_models["one"] == ("claude", "sonnet", "medium")


def test_no_effort_anywhere_resolves_to_none(tmp_path):
    # Neither the agent nor the tier fixes effort → None (the executor's own default applies).
    ws = _effort_ws(tmp_path, agent_effort=None, tier_effort=None)
    p = plan(ws, "p", {}, now=NOW)
    assert p.resolved_models["one"] == ("claude", "sonnet", None)


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


def _write_ws(tmp_path: Path, pipeline_yaml: str, *, agents=None, extra=None, tools="") -> Path:
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
        "[secrets]\nOK_TOKEN = {}\n" + tools,
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


def test_typod_produce_that_is_consumed_downstream_is_an_error(tmp_path):
    # 'aa' is a typo of the declared 'a'; it is produced AND consumed, so dataflow alone
    # is satisfied — only an undeclared-name check catches it (else it fails at runtime with
    # a misleading "unanswered gate 'aa'").
    steps = (
        "  - { id: one, run: 'echo', produces: [aa] }\n"
        "  - { id: two, run: 'echo', needs: [aa], produces: [b] }\n"
    )
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "aa" in str(exc.value) and "one" in str(exc.value)


def test_typod_pure_needs_is_an_error(tmp_path):
    steps = (
        "  - { id: one, run: 'echo', produces: [a] }\n"
        "  - { id: two, run: 'echo', needs: [aa], produces: [b] }\n"
    )
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "aa" in str(exc.value) and "two" in str(exc.value)


def test_gate_name_is_a_valid_needs_target(hello_ws):
    # hello's compose needs [greeting, tone] where tone is a GATE — gate names remain
    # consumable, the new declared-name check must not reject them.
    p = plan(hello_ws, "hello", {}, now=NOW)
    assert set(_node(p, "compose").needs) == {"greeting", "tone"}


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


# --------------------------------------------------------------------------- #
# (A) Static params/dims leaf check — a typo on the lazy side of &&/|| must not
#     hide behind short-circuit evaluation (defeats "misspellings disable steps").
# --------------------------------------------------------------------------- #


def test_params_typo_on_lazy_branch_is_caught(tmp_path):
    # gates.g is a runtime root, so `&&` short-circuits before params.NOPE is ever
    # evaluated — the static path check must catch it anyway.
    steps = "  - { id: one, run: 'echo', produces: [a], when: \"gates.g.choice == 'yes' && params.NOPE == 'z'\" }\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "NOPE" in str(exc.value) and "one" in str(exc.value)


def test_params_typo_stays_caught_with_flipped_operands(tmp_path):
    steps = "  - { id: one, run: 'echo', produces: [a], when: \"params.NOPE == 'z' && gates.g.choice == 'yes'\" }\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "NOPE" in str(exc.value)


def test_dims_typo_in_unless_is_caught(tmp_path):
    steps = "  - { id: one, run: 'echo', produces: [a], unless: \"dims.NOPE == 'x'\" }\n"
    with pytest.raises(ConfigError) as exc:
        _plan_p(tmp_path, steps)
    assert "NOPE" in str(exc.value)


def test_valid_lazy_branch_params_path_still_plans_green(tmp_path):
    # params.x exists; the lazy gates.g branch keeps the node runtime-conditional.
    steps = "  - { id: one, run: 'echo', produces: [a], when: \"params.x == 'hi' && gates.g.choice == 'yes'\" }\n"
    p = _plan_p(tmp_path, steps)
    assert _node(p, "one").when_runtime is not None


def test_unless_runtime_predicate_is_negated_into_when_runtime(tmp_path):
    # A runtime-only unless: survives as when_runtime = !(unless) — active iff the
    # predicate is false.
    steps = "  - { id: one, run: 'echo', produces: [a], unless: \"artifacts.a.ok == true\" }\n"
    p = _plan_p(tmp_path, steps)
    predicate = _node(p, "one").when_runtime
    assert predicate is not None

    def resolver(root, parts):
        assert (root, parts) == ("artifacts", ("a", "ok"))
        return True  # unless-condition holds → node inactive

    assert bool(predicate.evaluate(resolver)) is False
    assert bool(predicate.evaluate(lambda r, p: False)) is True  # unless false → node runs


# --------------------------------------------------------------------------- #
# (C) Loop re-production exemption is scoped to before-loop artifacts only.
# --------------------------------------------------------------------------- #

_LOOP_HEAD = """pipeline: p
version: 1
run_id: "p-{date}"
artifacts:
  seed: { path: seed.json, schema: schemas/s.json }
  rev:  { path: "rev-r{cycle}.json", schema: schemas/s.json }
steps:
  - { id: seed, run: 'echo', produces: [seed] }
  - loop: lp
    min: 1
    max: { interactive: 2, headless: 1 }
    until: "artifacts.rev.ok == true"
    body:
"""


def test_loop_body_reproducing_a_new_name_twice_is_an_error(tmp_path):
    body = (
        "      - { id: one, run: 'echo', needs: [seed], produces: [rev] }\n"
        "      - { id: two, run: 'echo', needs: [seed], produces: [rev] }\n"
    )
    ws = _write_ws(tmp_path, _LOOP_HEAD + body)
    with pytest.raises(ConfigError) as exc:
        plan(ws, "p", {}, now=NOW)
    assert "rev" in str(exc.value) and "lp" in str(exc.value)


def test_loop_body_may_reproduce_an_artifact_produced_before_the_loop(tmp_path):
    # `seed` is produced before the loop; a body step re-producing it is the sanctioned
    # re-production (§2.6) and must plan green.
    body = (
        "      - { id: check, run: 'echo', needs: [seed], produces: [rev] }\n"
        "      - { id: fix, run: 'echo', needs: [rev], produces: [seed] }\n"
    )
    ws = _write_ws(tmp_path, _LOOP_HEAD + body)
    p = plan(ws, "p", {}, now=NOW)
    assert isinstance(_node(p, "lp"), LoopNode)


def test_to_node_into_a_loop_body_is_an_error():
    # slicing is over top-level nodes; a loop-body step id ('review') is not sliceable.
    with pytest.raises(ConfigError) as exc:
        plan(BREASE_WS, "brease-rebuild", {"url": "https://acme.com", "mode": "redesign"}, to_node="review", now=NOW)
    assert "review" in str(exc.value)


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


# --------------------------------------------------------------------------- #
# The workspace `requires` version pin (docs/DISTRIBUTION.md §3) — enforced at
# plan time: a cairn.toml `requires` range the installed cairn does not satisfy
# refuses with a ConfigError naming both versions.
# --------------------------------------------------------------------------- #


def _pin_requires(ws: Path, spec: str) -> None:
    """Prepend a top-level `requires` pin to the workspace cairn.toml."""
    toml = ws / "cairn.toml"
    toml.write_text(f'requires = "{spec}"\n' + toml.read_text(encoding="utf-8"), encoding="utf-8")


def test_requires_pin_unsatisfied_refuses_at_plan_time(hello_ws):
    import cairn

    _pin_requires(hello_ws, ">=99.0")
    with pytest.raises(ConfigError) as exc:
        plan(hello_ws, "hello", {}, now=NOW)
    msg = str(exc.value)
    assert "requires" in msg
    assert ">=99.0" in msg  # the required range …
    assert cairn.__version__ in msg  # … and the installed version, both named


def test_requires_pin_satisfied_plans_green(hello_ws):
    # a representative pin satisfied by the installed cairn — kept 0.x-wide so it
    # does not rot each minor release (the <0.2 form broke on the 0.2.0 bump).
    _pin_requires(hello_ws, ">=0.1,<1")
    p = plan(hello_ws, "hello", {}, now=NOW)
    assert _ids(p.nodes) == ["greet", "tone", "compose"]


def test_requires_pin_malformed_spec_is_a_config_error(hello_ws):
    _pin_requires(hello_ws, ">=banana")
    with pytest.raises(ConfigError) as exc:
        plan(hello_ws, "hello", {}, now=NOW)
    assert "requires" in str(exc.value)


def test_requires_pin_must_be_a_string(hello_ws):
    toml = hello_ws / "cairn.toml"
    toml.write_text("requires = 1\n" + toml.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        plan(hello_ws, "hello", {}, now=NOW)
    assert "requires" in str(exc.value)


# --------------------------------------------------------------------------- #
# Range-scoped tool preflight (docs/TOOLING-AND-GROWTH §2) — plan warns (never
# errors, never runs a check) when an in-range step is scoped by a declared
# tool's `needed_by`; a `needed_by` naming no step/pipeline is a dangling-scope
# lint. Presence checks stay doctor's job; plan stays offline and fast.
# --------------------------------------------------------------------------- #

_TWO_STEPS = (
    "  - { id: one, run: 'echo', produces: [a] }\n"
    "  - { id: two, run: 'echo', needs: [a], produces: [b] }\n"
)


def _tool_msgs(p) -> list[str]:
    return [w.message for w in p.warnings if "tool" in w.message]


def test_in_range_tool_scope_warns_naming_step_and_tool(tmp_path):
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["two"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    p = plan(ws, "p", {}, now=NOW)
    msgs = _tool_msgs(p)
    assert any(
        "'two'" in m and "'crawl4ai'" in m and "unverified" in m and "cairn doctor" in m
        for m in msgs
    ), msgs
    # a plain warning, never an error — the plan still emits its nodes
    assert not [w for w in p.warnings if w.level == "error"]
    assert _ids(p.nodes) == ["one", "two"]


def test_out_of_range_tool_scope_does_not_warn(tmp_path):
    # `two` is sliced off by --to one; its tool must not warn.
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["two"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    p = plan(ws, "p", {}, now=NOW, to_node="one")
    assert _ids(p.nodes) == ["one"]
    assert _tool_msgs(p) == []


def test_dangling_needed_by_is_a_lint_warning(tmp_path):
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["ghost"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    p = plan(ws, "p", {}, now=NOW)
    msgs = _tool_msgs(p)
    assert any("'ghost'" in m and "dangling" in m for m in msgs), msgs


def test_no_tools_declared_means_no_tool_warnings(tmp_path):
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS)
    p = plan(ws, "p", {}, now=NOW)
    assert _tool_msgs(p) == []


def test_unscoped_tool_does_not_range_warn(tmp_path):
    # crawl4ai with no needed_by is workspace-global — doctor's job, not a range warning.
    tools = '[tools.crawl4ai]\ncheck = "true"\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    p = plan(ws, "p", {}, now=NOW)
    assert _tool_msgs(p) == []


def test_pipeline_scoped_tool_warns_for_the_whole_plan(tmp_path):
    # `needed_by = ["p"]` names the pipeline itself, not a step.
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["p"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    p = plan(ws, "p", {}, now=NOW)
    msgs = _tool_msgs(p)
    assert any("pipeline 'p'" in m and "'crawl4ai'" in m and "unverified" in m for m in msgs), msgs


def test_dropped_step_tool_scope_does_not_warn_and_is_not_dangling(tmp_path):
    # `two` is dropped by its own when:; its tool scope must not warn (it won't run) and must
    # not be flagged dangling (the step exists in the pipeline source).
    steps = (
        "  - { id: one, run: 'echo', produces: [a] }\n"
        "  - { id: two, run: 'echo', needs: [a], produces: [b], when: \"params.x == 'never'\" }\n"
    )
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["two"]\n'
    ws = _write_ws(tmp_path, _HEADER + steps, tools=tools)
    p = plan(ws, "p", {}, now=NOW)
    assert _tool_msgs(p) == []


def test_all_local_scopes_never_scan_sibling_pipelines(tmp_path, monkeypatch):
    # Every needed_by target resolves against the CURRENT pipeline (its name + its own step
    # ids, already in hand) — the workspace-wide fallback scan must not run at all, so sibling
    # pipeline files are never re-read on the common in-pipeline-scoping path.
    import cairn.kernel.plan as plan_mod

    def _boom(workspace_dir):
        raise AssertionError("workspace scan ran despite all-local scopes")

    monkeypatch.setattr(plan_mod, "_workspace_scope_names", _boom)
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["two", "p"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    # belt and braces: an unreadable sibling would also blow up any accidental scan
    (ws / "pipelines" / "broken.yaml").write_text("steps: [:::not yaml", encoding="utf-8")
    p = plan(ws, "p", {}, now=NOW)  # no raise → the fallback stayed lazy
    msgs = _tool_msgs(p)
    assert not any("dangling" in m for m in msgs)
    assert any("'two'" in m and "unverified" in m for m in msgs)


def test_dangling_check_tolerates_a_malformed_sibling_pipeline(tmp_path):
    # A residual (non-local) target forces the workspace fallback scan; a malformed sibling
    # pipelines/broken.yaml must be skipped, not crash the plan — the dangling warning still
    # lands and the plan still emits.
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["ghost"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    (ws / "pipelines" / "broken.yaml").write_text("steps: [:::not yaml", encoding="utf-8")
    p = plan(ws, "p", {}, now=NOW)
    msgs = _tool_msgs(p)
    assert any("'ghost'" in m and "dangling" in m for m in msgs), msgs
    assert _ids(p.nodes) == ["one", "two"]


# --------------------------------------------------------------------------- #
# plan.tool_requirements — the additive, range-scoped subset `cairn run` hard-stops
# on (docs/TOOLING-AND-GROWTH §2). Same in-range match as the warnings; carried
# structurally (whole check/install) so run-time can probe without re-deriving.
# --------------------------------------------------------------------------- #


def _reqs(p) -> dict:
    return {r.tool: r for r in p.tool_requirements}


def test_tool_requirements_carries_in_range_scoped_tool(tmp_path):
    tools = '[tools.crawl4ai]\ncheck = "true"\ninstall = "uv sync"\nneeded_by = ["two"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    reqs = _reqs(plan(ws, "p", {}, now=NOW))
    assert "crawl4ai" in reqs
    r = reqs["crawl4ai"]
    assert r.check == "true" and r.install == "uv sync" and r.targets == ("two",)


def test_tool_requirements_excludes_unscoped_tool(tmp_path):
    # unscoped (no needed_by) is doctor's global concern — never a run-time hard-stop.
    tools = '[tools.crawl4ai]\ncheck = "true"\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    assert plan(ws, "p", {}, now=NOW).tool_requirements == ()


def test_tool_requirements_excludes_out_of_range_tool(tmp_path):
    # `two` sliced off by --to one → its scoped tool is not a run-time requirement (never probed).
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["two"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    assert plan(ws, "p", {}, now=NOW, to_node="one").tool_requirements == ()


def test_tool_requirements_includes_pipeline_scope(tmp_path):
    tools = '[tools.crawl4ai]\ncheck = "true"\nneeded_by = ["p"]\n'
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS, tools=tools)
    reqs = _reqs(plan(ws, "p", {}, now=NOW))
    assert reqs["crawl4ai"].targets == ("p",)


def test_tool_requirements_absent_when_no_tools_declared(tmp_path):
    ws = _write_ws(tmp_path, _HEADER + _TWO_STEPS)
    assert plan(ws, "p", {}, now=NOW).tool_requirements == ()


# --------------------------------------------------------------------------- #
# Unknown agent-YAML keys warn (silent-data-loss trap: behavior text an author
# THINKS the agent reads — cairn ignores it, so say so).
# --------------------------------------------------------------------------- #


def test_unknown_agent_key_warns_naming_key_and_file(tmp_path):
    steps = "  - { id: one, agent: worker, produces: [a] }\n"
    agent = "tier: balanced\nprompt: 'do the thing'\ntools: { allow: [read] }\n"
    p = _plan_p(tmp_path, steps, agents={"worker": agent})
    hits = [w for w in p.warnings if "prompt" in w.message]
    assert hits and hits[0].level == "warning"
    assert "worker" in hits[0].message and "worker.yaml" in hits[0].message


def test_description_key_does_not_warn(tmp_path):
    # `description:` is a harmless human-facing convention the scaffold already uses.
    steps = "  - { id: one, agent: worker, produces: [a] }\n"
    agent = "description: 'a worker'\ntier: balanced\ntools: { allow: [read] }\n"
    p = _plan_p(tmp_path, steps, agents={"worker": agent})
    assert not any("description" in w.message for w in p.warnings)


def test_known_read_keys_do_not_warn(tmp_path):
    steps = "  - { id: one, agent: worker, produces: [a] }\n"
    agent = (
        "tier: balanced\neffort: medium\n"
        "escalate: { when: \"params.x == 'hi'\", tier: reasoning }\n"
        "skills: []\ntools: { allow: [read] }\nenv: [OK_TOKEN]\nreturns: schemas/s.json\n"
    )
    p = _plan_p(tmp_path, steps, agents={"worker": agent})
    assert not any("ignores" in w.message for w in p.warnings)
