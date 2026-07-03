"""ShellExecutor — how run:/deterministic steps execute. The prompt_file CONTENT is the
rendered command string, run via /bin/sh -c; model/effort are ignored."""

from __future__ import annotations

from cairn.executors.shell import ShellExecutor
from cairn.kernel.types import Capabilities, Result

from test_executors_base import make_inv


def test_runs_the_command_file_content_and_ignores_model(tmp_path):
    command = (
        'printf "canary=%s\\n" "$CAIRN_CANARY"\n'
        'echo \'<<<STEP {"status":"done","summary":"shell ran","artifacts":[]} STEP>>>\'\n'
    )
    inv = make_inv(tmp_path, prompt=command, model="ignored-model")
    result = ShellExecutor().invoke(inv)

    assert isinstance(result, Result)
    assert result.exit_code == 0
    assert result.step == {"status": "done", "summary": "shell ran", "artifacts": []}
    assert "canary=canary-value" in inv.log_path.read_text()


def test_command_runs_in_the_run_dir(tmp_path):
    inv = make_inv(tmp_path, prompt="pwd")
    ShellExecutor().invoke(inv)
    assert str(inv.cwd) in inv.log_path.read_text()


def test_nonzero_exit_is_propagated(tmp_path):
    inv = make_inv(tmp_path, prompt="exit 5")
    assert ShellExecutor().invoke(inv).exit_code == 5


def test_resolve_model_is_trivial(tmp_path):
    assert ShellExecutor().resolve_model("reasoning", "high") == ("shell", None)
    assert ShellExecutor().resolve_model("anything", "low") == ("shell", None)


def test_capabilities():
    caps = ShellExecutor().capabilities
    assert caps == Capabilities(blocking_hooks=False, output_schema=False, session_capture=None)


def test_doctor_is_trivially_healthy():
    assert ShellExecutor().doctor() == []


def test_guards_and_workspace_are_noops(tmp_path):
    ex = ShellExecutor()
    assert ex.install_guards([], object()) is None
    assert ex.render_workspace(object()) is None


def test_name():
    assert ShellExecutor().name == "shell"
