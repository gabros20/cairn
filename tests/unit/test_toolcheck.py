"""The shared ``[tools]`` check-runner (docs/TOOLING-AND-GROWTH §2) — one definition of
"run a tool's ``check`` probe", used by both ``cairn doctor`` and ``cairn run``'s hard-stop."""

from __future__ import annotations

from cairn.kernel.toolcheck import run_tool_check


def test_exit_zero_is_verified():
    assert run_tool_check("true") is True


def test_nonzero_exit_is_unverified():
    assert run_tool_check("false") is False


def test_check_runs_in_a_shell():
    # a compound command proves it's `/bin/sh -c`, not a bare exec.
    assert run_tool_check("test 1 -eq 1 && exit 0") is True


def test_timeout_is_unverified_not_a_raise():
    assert run_tool_check("sleep 5", timeout=0.05) is False


def test_doctor_run_check_delegates_here():
    # doctor keeps a thin `_run_check` alias so its call sites read unchanged; it must share
    # semantics with the shared runner (identical booleans).
    from cairn.kernel import doctor

    assert doctor._run_check("true") is True
    assert doctor._run_check("false") is False
