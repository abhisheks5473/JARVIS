"""Shell access: the most useful tool here, and the most dangerous.

You chose full desktop control, so this is not sandboxed to the workspace.
That makes the guardrails load-bearing rather than decorative:

  * the hard-deny list is checked here as well as in the approval gate, so a
    refused command dies even if the gate is ever misconfigured,
  * every call is gated and shown to you in full before it runs,
  * there is a wall-clock timeout, so a hung command cannot wedge the agent,
  * output is truncated before it reaches the model, because a command that
    prints 200k lines would otherwise eat the token budget in one turn,
  * output is treated as untrusted -- a command that prints a downloaded file
    is an injection vector like any other,
  * secrets in output are redacted before the model ever sees them, and the
    API key is stripped from the subprocess environment. An agent that can
    read its own credentials can leak them.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..security.approval import redact, shell_hard_denied
from .base import ToolError, tool

MAX_OUTPUT_CHARS = 6000
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 180


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    half = MAX_OUTPUT_CHARS // 2
    omitted = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:half]}\n...[{omitted} chars omitted]...\n{text[-half:]}", True


def _run(argv: list[str], command: str, cwd: str, timeout: int) -> dict:
    blocked = shell_hard_denied(command)
    if blocked:
        raise ToolError(
            "this command is on the never-run list and was refused",
            hint=(
                "it matched a hard-deny rule; tell the user what you tried to "
                "run and why, and do not attempt a variant of it"
            ),
        )

    workdir = Path(cwd).expanduser() if cwd else Path.home()
    if not workdir.is_dir():
        raise ToolError(
            f"working directory does not exist: {workdir}",
            hint="give an existing directory, or omit cwd to use the home folder",
        )

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(workdir),
            # Never inherit the API key into a subprocess. A command that
            # prints its environment should not be able to print your key.
            env={k: v for k, v in os.environ.items() if "API_KEY" not in k.upper()},
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            f"command timed out after {timeout}s",
            hint=(
                "it may be waiting for input; interactive commands cannot be "
                "used here"
            ),
        ) from None
    except FileNotFoundError:
        raise ToolError(
            "the interpreter for that command was not found",
            hint="check the executable name, or tell the user it is not installed",
        ) from None

    stdout, out_truncated = _truncate(completed.stdout or "")
    stderr, err_truncated = _truncate(completed.stderr or "")

    return {
        "exit_code": completed.returncode,
        "stdout": redact(stdout),
        "stderr": redact(stderr),
        "truncated": out_truncated or err_truncated,
        "cwd": str(workdir),
        "succeeded": completed.returncode == 0,
    }


@tool(group="shell", untrusted_output=True)
def run_powershell(command: str, cwd: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a PowerShell command and return its output.

    This is the right shell on Windows for anything involving the filesystem,
    services, or system state. Requires the user's approval every time.

    Do not use it for interactive commands, anything that opens a GUI prompt,
    or anything that waits for input -- those will simply time out. Do not use
    it to delete files; call delete_path, which is recoverable.

    Args:
        command: The PowerShell command line to execute.
        cwd: Working directory. Defaults to the user's home folder.
        timeout: Seconds to wait before giving up. Maximum 180.
    """
    if not command.strip():
        raise ToolError("empty command", hint="give a command to run")
    return _run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        command,
        cwd,
        timeout,
    )


@tool(group="shell", untrusted_output=True)
def run_shell(command: str, cwd: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a command through the classic Windows command prompt.

    Prefer run_powershell for almost everything. Use this only for older tools
    that genuinely need cmd.exe semantics. Requires the user's approval.

    Args:
        command: The command line to execute.
        cwd: Working directory. Defaults to the user's home folder.
        timeout: Seconds to wait before giving up. Maximum 180.
    """
    if not command.strip():
        raise ToolError("empty command", hint="give a command to run")
    return _run(["cmd.exe", "/c", command], command, cwd, timeout)


@tool(group="dev", untrusted_output=True)
def git_command(subcommand: str, repo_path: str = "", timeout: int = 60) -> dict:
    """Run a read-oriented git command in a repository.

    Use for status, log, diff, branch and show. Pushing, force-resetting and
    history rewriting are refused here -- tell the user to do those
    themselves.

    Args:
        subcommand: The git subcommand and its arguments, e.g. "status --short"
            or "log --oneline -10". Do not include the word git itself.
        repo_path: Path to the repository. Defaults to the home folder.
        timeout: Seconds to wait before giving up.
    """
    lowered = subcommand.strip().lower()
    forbidden = (
        "push",
        "reset --hard",
        "clean -",
        "filter-branch",
        "rebase",
        "gc --prune",
    )
    if any(f in lowered for f in forbidden):
        raise ToolError(
            f"refusing to run a history-changing or remote git command: {subcommand}",
            hint="report this to the user and let them run it themselves",
        )
    return _run(
        ["git", *subcommand.split()],
        f"git {subcommand}",
        repo_path or str(Path.home()),
        timeout,
    )
