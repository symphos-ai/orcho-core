"""
verification_command.py — Stage 3 generic engine that *executes* one declared
verification ``command`` and returns a flat command-receipt payload.

Sibling of :mod:`pipeline.verification_env` (which executes env-assertions); both
share the env-runtime resolver :func:`pipeline.verification_env.resolve_env_runtime`
so interpreter / effective-cwd / process-env resolution stays single-sourced.

Load-bearing subject separation (F1): the subprocess runs in ``eff_cwd`` — the
declared env ``cwd`` (which may be ``{project}``, a dependency dir, or a
subdirectory) — and that path is the *only* thing recorded as ``receipt.cwd``.
Git provenance (``git.checkout_head`` / ``git.changed_files_fingerprint``) is
taken from ``ctx.checkout`` (the run worktree, the verification *subject*),
NEVER from ``eff_cwd``. Mixing the two would let a differential receipt attribute
a baseline diff to the wrong tree.

This module never raises outward: an ``OSError`` / ``SubprocessError`` (incl.
timeout) degrades to ``exit_code=None`` with a ``detail``.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from core.io.git_helpers import git_head
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
    resolve_placeholders,
)
from pipeline.verification_dependencies import (
    capture_dependency_provenance,
    changed_files_fingerprint,
)
from pipeline.verification_env import resolve_env_runtime, run_env_assertions

# Command wall-clock budget. A hung command degrades to a failed receipt
# (exit_code=None) rather than blocking the run indefinitely.
_TIMEOUT_S = 600


def run_command(
    command_name: str,
    cmd_spec: dict[str, Any],
    contract: VerificationContract,
    ctx: PlaceholderContext,
    *,
    required: bool = False,
    baseline_head: str | None = None,
    log_dir: Path | None = None,
    tail_chars: int = 4000,
) -> dict[str, Any]:
    """Execute one declared command natively and return its receipt payload.

    ``cmd_spec`` is the raw ``verification.commands[command_name]`` dict (already
    normalised by :class:`VerificationContract`). ``baseline_head`` is supplied
    by the caller (the required-gate differential subject) — the executor never
    derives it. ``log_dir`` opts into writing the full stdout+stderr to
    ``<log_dir>/<safe_command>.log``; ``tail_chars`` bounds the inline tails.

    Returns a flat dict (NOT written to disk here): ``kind``, ``command``,
    ``env``, ``cwd`` (= eff_cwd), ``placeholders`` (checkout/project), ``argv``,
    ``env_overrides``, ``assertions``, ``exit_code``, ``duration_s``,
    ``stdout_tail`` / ``stderr_tail``, ``log_path``, ``parity``, ``git``
    (``checkout_head`` / ``baseline_head`` / ``changed_files_fingerprint`` — all
    relative to ``ctx.checkout``), and ``dependencies`` (a sibling of ``git``:
    per-declared-dependency cross-repo provenance — ``git`` stays the subject's
    own differential lens, ``dependencies`` records the depended-on repos). Never
    raises.
    """
    env_name = cmd_spec.get("env") or contract.default_env
    env_declared = bool(env_name) and env_name in contract.verification_envs
    env_spec = contract.verification_envs.get(env_name, {}) if env_declared else {}
    python, eff_cwd, sub_env, env_overrides = resolve_env_runtime(env_spec, ctx)

    argv = _resolve_argv(cmd_spec.get("run", ""), ctx, python=python)

    exit_code, stdout, stderr, duration_s, detail = _execute(argv, eff_cwd, sub_env)

    log_path = _write_log(log_dir, command_name, stdout, stderr)

    assertions: list[dict[str, Any]] = []
    if env_declared:
        env_result = run_env_assertions(env_name, env_spec, ctx)
        assertions = env_result.get("assertions", [])

    parity = cmd_spec.get("parity", "absolute")

    # F1 — git provenance is always taken from the run worktree (ctx.checkout),
    # the verification subject, never from eff_cwd.
    subject_checkout = ctx.checkout or ""
    checkout_head = git_head(subject_checkout) if subject_checkout else None
    fingerprint = (
        changed_files_fingerprint(subject_checkout)
        if checkout_head is not None
        else None
    )

    dependencies = capture_dependency_provenance(
        ctx,
        argv=argv,
        eff_cwd=eff_cwd,
        python=python,
        env_overrides=env_overrides,
    )

    return {
        "kind": "verification_command",
        "command": command_name,
        "env": env_name,
        "cwd": eff_cwd,
        "placeholders": {"checkout": ctx.checkout, "project": ctx.project},
        "argv": argv,
        "env_overrides": env_overrides,
        "assertions": assertions,
        "exit_code": exit_code,
        "duration_s": duration_s,
        "stdout_tail": _tail(stdout, tail_chars),
        "stderr_tail": _tail(stderr, tail_chars),
        "log_path": str(log_path) if log_path is not None else None,
        "parity": parity,
        "detail": detail,
        "git": {
            "checkout_head": checkout_head,
            "baseline_head": baseline_head,
            "changed_files_fingerprint": fingerprint,
        },
        "dependencies": dependencies,
    }


def _resolve_argv(
    run_decl: Any, ctx: PlaceholderContext, *, python: str,
) -> list[str]:
    """Build the argv: split strings via ``shlex`` (lists kept verbatim),
    placeholder-resolve each token, then map the ``python`` token to the
    declared interpreter."""
    if isinstance(run_decl, (list, tuple)):
        raw_argv = [str(a) for a in run_decl]
    else:
        raw_argv = shlex.split(str(run_decl))
    argv: list[str] = []
    for tok in raw_argv:
        resolved = resolve_placeholders(tok, ctx)
        if resolved == "python":
            resolved = python
        argv.append(resolved)
    return argv


def _execute(
    argv: list[str], eff_cwd: str, sub_env: dict[str, str],
) -> tuple[int | None, str, str, float, str]:
    """Run ``argv`` without a shell; degrade failures to ``exit_code=None``."""
    if not argv:
        return None, "", "", 0.0, "empty command (nothing to run)"
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 — argv is declared, not shell
            argv,
            cwd=eff_cwd or None,
            env=sub_env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "", "", time.monotonic() - start, (
            f"command timed out after {_TIMEOUT_S}s"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", "", time.monotonic() - start, f"subprocess error: {exc}"
    return (
        proc.returncode,
        proc.stdout or "",
        proc.stderr or "",
        time.monotonic() - start,
        "",
    )


def _write_log(
    log_dir: Path | None, command_name: str, stdout: str, stderr: str,
) -> Path | None:
    if log_dir is None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", command_name) or "command"
    path = log_dir / f"{safe}.log"
    body = stdout
    if stderr:
        body = f"{body}\n--- stderr ---\n{stderr}" if body else stderr
    path.write_text(body, encoding="utf-8")
    return path


def _tail(text: str, tail_chars: int) -> str:
    if not text or tail_chars <= 0:
        return ""
    return text[-tail_chars:]
