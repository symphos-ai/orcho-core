"""
verification_command.py — Stage 3 generic engine that *executes* one declared
verification ``command`` and returns a flat command-receipt payload.

Sibling of :mod:`pipeline.verification_env` (which executes env-assertions); both
share the env-runtime resolver :func:`pipeline.verification_env.resolve_env_runtime`
so interpreter / effective-cwd / process-env resolution stays single-sourced.

Load-bearing subject separation (F1): the subprocess runs in ``eff_cwd`` — the
declared env ``cwd`` (which may be ``{project}``, a dependency dir, or a
subdirectory) — and that path is the *only* thing recorded as ``receipt.cwd``.
Git provenance (the typed ``subject`` and diagnostic ``git.checkout_head``) is
taken from ``ctx.checkout`` (the run worktree, the verification *subject*),
NEVER from ``eff_cwd``. Mixing the two would let a differential receipt attribute
a baseline diff to the wrong tree.

This module never raises outward: an ``OSError`` / ``SubprocessError`` (incl.
timeout) degrades to ``exit_code=None`` with a ``detail``.
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.io.bounded_proc import (
    Completed,
    OnOutput,
    SpawnFailure,
    TimedOut,
    run_bounded,
)
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
    resolve_placeholders,
)
from pipeline.verification_dependencies import capture_dependency_provenance
from pipeline.verification_env import (
    map_python_token,
    resolve_env_runtime,
    run_env_assertions,
)
from pipeline.verification_subject import (
    VerificationSubjectAvailable,
    capture_verification_subject,
)

if TYPE_CHECKING:
    from pipeline.verification_progress import GateProgressContext

# Command wall-clock budget used when the contract declares none. A hung
# command degrades to a failed receipt (exit_code=None) rather than blocking the
# run indefinitely. A command whose honest runtime approaches this ceiling
# declares its own ``timeout`` (validated positive int) — the default is a
# backstop against hangs, not a statement about any project's suite.
_DEFAULT_TIMEOUT_S = 600

# How the execution itself ended, independent of what the command decided.
# ``completed`` means the process ran to its own exit code (0 or not); the other
# three mean there is no exit code to read, for three operator-distinct reasons.
COMMAND_OUTCOMES: tuple[str, ...] = ("completed", "timeout", "error", "empty")


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
    progress: GateProgressContext | None = None,
) -> dict[str, Any]:
    """Execute one declared command natively and return its receipt payload.

    ``cmd_spec`` is the raw ``verification.commands[command_name]`` dict (already
    normalised by :class:`VerificationContract`). ``baseline_head`` is supplied
    by the caller (the required-gate differential subject) — the executor never
    derives it. ``log_dir`` opts into writing the full stdout+stderr to
    ``<log_dir>/<safe_command>.log``; ``tail_chars`` bounds the inline tails.

    ``progress`` opts into live, coalesced ``gate.progress`` publication while
    the command runs (ADR 0190): when supplied, a
    :class:`pipeline.verification_progress.GateProgressAggregator` is wired to
    the streaming executor as its output callback. It is purely observational —
    the receipt shape, the outcomes, and #305 timeout partial-output retention
    are unchanged whether or not it is supplied, and an emit/presenter failure
    never alters the command's pass/fail result. When absent behavior is
    identical to before (``sdk/verify.py`` and existing tests unaffected).

    Returns a flat dict (NOT written to disk here): ``kind``, ``command``,
    ``env``, ``cwd`` (= eff_cwd), ``placeholders`` (checkout/project), ``argv``,
    ``env_overrides``, ``assertions``, ``exit_code``, ``duration_s``,
    ``stdout_tail`` / ``stderr_tail``, ``log_path``, ``parity``, ``outcome``
    (how the *execution* ended — see :data:`COMMAND_OUTCOMES` — as opposed to
    what the command decided), typed
    ``subject``, diagnostic ``git`` (``checkout_head`` / ``baseline_head`` —
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

    timeout_s = int(cmd_spec.get("timeout") or _DEFAULT_TIMEOUT_S)
    from pipeline.verification_progress import observe_gate

    with observe_gate(progress) as on_output:
        exit_code, stdout, stderr, duration_s, detail, outcome = _execute(
            argv, eff_cwd, sub_env, timeout_s=timeout_s, on_output=on_output,
        )

    log_path = _write_log(log_dir, command_name, stdout, stderr)

    assertions: list[dict[str, Any]] = []
    if env_declared:
        env_result = run_env_assertions(env_name, env_spec, ctx)
        assertions = env_result.get("assertions", [])

    parity = cmd_spec.get("parity", "absolute")

    # F1 — git provenance is always taken from the run worktree (ctx.checkout),
    # the verification subject, never from eff_cwd.
    subject = capture_verification_subject(Path(ctx.checkout), baseline_ref=baseline_head) if ctx.checkout else None
    identity = subject.identity if isinstance(subject, VerificationSubjectAvailable) else None
    # A missing historical baseline makes the durable subject unavailable, but
    # must not erase the existing diagnostic checkout HEAD from CommandOutcome.
    diagnostic_identity = identity
    if diagnostic_identity is None and ctx.checkout:
        diagnostic = capture_verification_subject(Path(ctx.checkout))
        diagnostic_identity = diagnostic.identity if isinstance(diagnostic, VerificationSubjectAvailable) else None

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
        "outcome": outcome,
        "git": {
            "checkout_head": diagnostic_identity.observed_head_oid if diagnostic_identity else None,
            # This is diagnostic provenance only; the typed subject remains
            # the sole freshness proof and may be unavailable for this
            # baseline even when the caller supplied one.
            "baseline_head": identity.baseline_oid if identity else None,
        },
        "subject": subject,
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
        argv.append(map_python_token(resolved, python))
    return argv


def _coerce_stream(value: Any) -> str:
    """Normalise a captured stream to text without ever raising.

    ``None`` -> ``""``; ``str`` -> unchanged; ``bytes`` -> a lossily decoded
    string (utf-8 with ``errors="replace"``) so incomplete/invalid encoded
    bytes never raise and no repr-style ``b'...'`` wrapping leaks into text
    output. This is the single normaliser applied on the completed, timeout, and
    spawn-failure paths, so ``run_bounded``'s captured bytes (or the str/None a
    monkeypatched boundary hands back) are preserved verbatim as text (#305).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _execute(
    argv: list[str], eff_cwd: str, sub_env: dict[str, str],
    *, timeout_s: int = _DEFAULT_TIMEOUT_S, on_output: OnOutput | None = None,
) -> tuple[int | None, str, str, float, str, str]:
    """Run ``argv`` without a shell; degrade failures to ``exit_code=None``.

    Streams via :func:`core.io.bounded_proc.run_bounded` so a caller can observe
    live output (the ``on_output`` callback) without changing the captured
    result: the returned tails and log are byte-for-byte identical whether or
    not a callback is supplied. ``run_bounded`` also owns the process tree, so a
    timeout / cancellation kills the whole subtree (no surviving grandchild).

    Returns the trailing ``outcome`` as a typed member of
    :data:`COMMAND_OUTCOMES`. Every non-``completed`` outcome carries the same
    ``exit_code=None`` as before, so the *why* is no longer recoverable only
    from prose in ``detail``: a command that never finished within its budget is
    a different operator problem from one whose binary could not be spawned.

    On timeout the reader threads have usually already captured whatever the
    child flushed before the kill; that output is preserved via
    :func:`_coerce_stream` (#305) while ``exit_code`` stays ``None`` and
    ``outcome`` stays ``"timeout"`` — the receipt must never reinterpret
    captured output as command completion. stdout and stderr stay distinct.
    """
    if not argv:
        return None, "", "", 0.0, "empty command (nothing to run)", "empty"
    start = time.monotonic()
    # text=False: bounded_proc returns raw bytes so ``on_output`` sees byte
    # chunks (the aggregator owns the incremental decode); we normalise the
    # captured streams to text here via the single :func:`_coerce_stream`.
    outcome = run_bounded(
        argv,
        timeout_s=float(timeout_s),
        cwd=eff_cwd or None,
        env=sub_env,
        text=False,
        on_output=on_output,
    )
    duration_s = time.monotonic() - start
    if isinstance(outcome, Completed):
        return (
            outcome.returncode,
            _coerce_stream(outcome.stdout),
            _coerce_stream(outcome.stderr),
            duration_s,
            "",
            "completed",
        )
    if isinstance(outcome, TimedOut):
        return (
            None,
            _coerce_stream(outcome.stdout),
            _coerce_stream(outcome.stderr),
            duration_s,
            f"command timed out after {timeout_s}s",
            "timeout",
        )
    # SpawnFailure: the binary could not be launched; no exit code exists.
    assert isinstance(outcome, SpawnFailure)
    return (
        None, "", "", duration_s,
        f"subprocess error: {outcome.error}", "error",
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
