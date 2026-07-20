"""The ``shell`` executor — how ``run:`` / deterministic steps execute.

There is no agent and no model here: the walker writes the rendered command string into the
step's ``prompt_file`` (so ``logs/<step>.prompt.md`` uniformly records what ran), and this
executor runs that content via ``/bin/sh -c``. That is the one sanctioned shell invocation in
the system — it IS a shell step, and the command was plan-verified.
"""

from __future__ import annotations

from cairn.executors.base import (
    Capabilities,
    Finding,
    Invocation,
    Result,
    parse_step_sentinel,
    run_process,
)


class ShellExecutor:
    name = "shell"
    capabilities = Capabilities(
        blocking_hooks=False,  # n/a for a shell step
        output_schema=False,
        session_capture=None,
        installs_hooks=False,  # n/a — no hook mechanism, install_guards below is a no-op
    )

    def __init__(self, config=None) -> None:
        # Accepts an (ignored) ExecutorConfig so the walker can construct every executor
        # uniformly; a shell step has no tiers/model to resolve.
        self._config = config

    def doctor(self) -> list[Finding]:
        return []  # /bin/sh is always present — trivially healthy

    def resolve_model(self, tier: str, effort: str) -> tuple[str, str | None]:
        return ("shell", None)

    def invoke(self, inv: Invocation) -> Result:
        command = inv.prompt_file.read_text(encoding="utf-8")
        exit_code, output, duration_s = run_process(
            ["/bin/sh", "-c", command],
            stdin_text=None,
            env=inv.env,
            cwd=inv.cwd,
            timeout_s=inv.timeout_s,
            log_path=inv.log_path,
            redactor=inv.redactor,
        )
        return Result(
            step=parse_step_sentinel(output),
            exit_code=exit_code,
            duration_s=duration_s,
        )

    def install_guards(self, guards, ws, run_dir) -> None:
        return None  # shell has no native hook; the shim layer covers it

    def render_workspace(self, ws) -> None:
        return None
