# SPDX-License-Identifier: Apache-2.0
"""pipeline.evidence.verification_receipt — durable verification receipts.

A developer-side phase (``implement`` / ``repair_changes``) records a
**verification-environment receipt** describing the environment it ran its
work / checks in: which interpreter, which working directory, which checks
and commands, and whether any throwaway environment lived *outside* the
source checkout. The contract is fixed by ADR 0076.

Receipts are written under the run output directory (``state.output_dir``)
— never the source checkout — at
``<run_dir>/verification_receipts/<phase>_round<N>.json``, mirroring the
``phase_handoff_decisions/`` convention. The writer never creates a venv
or any environment itself; it only records what the phase did, so it
cannot pollute ``git status`` in the checkout.

Receipt shape (ADR 0076 / T6)::

    {
      "phase": "repair_changes",
      "round": 1,
      "kind": "verification_environment",
      "cwd": "/abs/path",
      "python": "3.12.4 (/abs/.../python)",
      "checks":   [{"name", "expected", "actual", "passed"}],
      "commands": [{"argv", "exit_code"}],
      "temp_env_outside_checkout": true
    }

:func:`summarize_verification_receipts` is the brief projection consumed by
the evidence bundle (this module's :func:`pipeline.evidence.collector`
wiring) and the reviewer prompt/context (T7).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.verification_subject import (
    VerificationSubjectAvailable,
    VerificationSubjectIdentity,
    VerificationSubjectUnavailable,
    is_usable_verification_subject,
)

__all__ = [
    "COMMAND_RECEIPTS_DIRNAME",
    "COMMAND_RECEIPT_SCHEMA_VERSION",
    "ENV_RECEIPTS_DIRNAME",
    "RECEIPTS_DIRNAME",
    "VERIFICATION_COMMAND_KIND",
    "VERIFICATION_ENV_KIND",
    "VERIFICATION_RECEIPT_KIND",
    "EnvProvenanceFailure",
    "collect_environment_checks",
    "command_receipt_passed",
    "environment_provenance_failures",
    "load_command_receipts",
    "load_env_assertion_receipts",
    "load_verification_receipts",
    "summarize_command_receipts",
    "summarize_verification_receipts",
    "write_command_receipt",
    "write_env_assertion_receipt",
    "write_phase_verification_receipt",
    "write_verification_receipt",
]

RECEIPTS_DIRNAME = "verification_receipts"
VERIFICATION_RECEIPT_KIND = "verification_environment"

# Operator env-assertion receipts (Stage 2 / ADR 0078) live in a DISTINCT
# directory from RECEIPTS_DIRNAME. The evidence collector reads only
# ``verification_receipts/``; placing env-assertion receipts under their own
# directory keeps the new kind OUT of the schema-validated evidence v1 bundle
# by *physical location*, not by filtering. Do not point the collector here.
ENV_RECEIPTS_DIRNAME = "verification_env_receipts"
VERIFICATION_ENV_KIND = "verification_env_assertions"

# Stage 3 native command-receipts (ADR 0080) live in YET ANOTHER distinct
# directory, by the same isolation principle as ENV_RECEIPTS_DIRNAME above: the
# evidence collector reads only ``verification_receipts/``, so a command-receipt's
# VERIFICATION_COMMAND_KIND never enters the evidence v1 bundle — kept out by
# *physical location*, not by filtering. Do NOT point the collector here.
COMMAND_RECEIPTS_DIRNAME = "verification_command_receipts"
COMMAND_RECEIPT_EXECUTIONS_DIRNAME = "executions"
VERIFICATION_COMMAND_KIND = "verification_command"
# v2 adds the top-level ``dependencies`` block (per-declared-dependency
# cross-repo provenance — name/path/head/dirty/changed_files_count/
# changed_files_fingerprint/depends_on). This is a run-local durable artifact
# only; it does NOT enter the evidence v1 bundle / MCP wire (the digest in
# summarize_command_receipts deliberately omits it).
COMMAND_RECEIPT_SCHEMA_VERSION = 3


def _python_identity() -> str:
    """Human-readable interpreter identity: ``<version> (<executable>)``."""
    version = sys.version.split()[0] if sys.version else "?"
    return f"{version} ({sys.executable})"


def collect_environment_checks(
    cwd: Path | str,
    *,
    contract: Any = None,
    ctx: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Probe the verification environment and return ``(checks, commands)``.

    Project-aware provenance selection (ADR 0125 / 0108), in order:

    (a) **Core checkout** — when ``{cwd}/pipeline/__init__.py`` exists, run the
        ``pipeline_import`` import-invariant in a subprocess from ``cwd`` (which
        is on ``sys.path[0]`` for ``-c``, so a checkout-local ``pipeline/``
        shadows any installed copy). ``passed`` is True only when the subprocess
        actually imported the *expected* ``pipeline/__init__.py`` — a stable Orcho
        run against a checkout must not silently verify against the install's
        ``pipeline``. Under per-run isolation (ADR 0112 §3) the expected tree is
        the run's worktree, NOT ``cwd``: a receipt written with ``cwd`` at the
        canonical sibling (a clean tree with none of the run's diff) then
        mismatches the worktree's ``pipeline`` and fails provenance instead of
        vacuously matching the sibling's own copy. For a single-checkout run the
        expected tree IS ``cwd`` and this is the load-bearing core invariant,
        unchanged.
    (b) **Non-core checkout with declared assertions** — when there is no local
        ``pipeline`` but ``contract`` declares a verification env (its
        ``default_env``, else the single declared env) carrying non-empty
        ``assertions``, execute those declared assertions (via
        :func:`pipeline.verification_env.run_env_assertions`) against ``ctx`` and
        map each result into a receipt check. This lets an MCP-shaped checkout
        prove its own import provenance (e.g. ``orcho_mcp`` from ``{checkout}/src``
        and ``pipeline`` from a dependency checkout) without the core-only probe.
    (c) **Otherwise** — neither a local ``pipeline`` nor declared assertions:
        record NO failing provenance check (a single informational, non-failing
        ``environment_provenance`` check), so a checkout that simply has nothing
        to assert never produces a false provenance failure.

    Never raises: subprocess / IO / resolution failure degrades to a
    non-failing set, never a false failure. ``commands`` always carries at least
    one diagnostic entry.
    """
    cwd_path = Path(cwd)
    expected_init = cwd_path / "pipeline" / "__init__.py"

    # (a) core checkout-local invariant. Under per-run isolation the expected tree
    # is the worktree (fail-closed; ADR 0112 §3), so ``ctx``'s isolated source is
    # threaded in — for a single-checkout run it is absent and behaviour is
    # unchanged.
    if expected_init.is_file():
        return _core_pipeline_import_checks(
            cwd_path,
            expected_init,
            isolated=getattr(ctx, "isolated_source", None),
        )

    # (b) non-core checkout with declared env assertions.
    declared = _declared_env_provenance_checks(cwd_path, contract, ctx)
    if declared is not None:
        return declared

    # (c) nothing to assert — never a false provenance failure.
    return _non_core_noop_checks()


def _core_pipeline_import_checks(
    cwd_path: Path,
    expected_init: Path,
    *,
    isolated: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The core ``pipeline_import`` probe (branch (a)), fail-closed under isolation.

    The import subprocess always runs from ``cwd_path`` (what the phase actually
    used), but the *expected* ``pipeline/__init__.py`` is the worktree's whenever
    ``isolated`` declares per-run isolation (ADR 0112 §3): a receipt whose ``cwd``
    is the canonical sibling then yields ``actual`` (the sibling's pipeline) that
    cannot match the worktree's expected, so a wrong-tree cwd fails rather than
    vacuously matching its own copy. A declared-but-unbound worktree (empty path)
    is a fail-closed provenance failure too — nothing legitimate to import — which
    the preflight/resolver also hard-errors before implement. Without isolation
    the expected tree is ``cwd_path`` and the historical core invariant is
    byte-identical.
    """
    import subprocess

    if isolated is not None and getattr(isolated, "is_declared", False):
        worktree_path = str(getattr(isolated, "worktree_path", "") or "")
        if not worktree_path:
            # Declared isolation with no bindable worktree: fail closed.
            unbound_checks: list[dict[str, Any]] = [
                {
                    "name": "pipeline_import",
                    "expected": "<unbound isolated worktree>",
                    "actual": None,
                    "passed": False,
                }
            ]
            unbound_commands: list[dict[str, Any]] = [
                {
                    "argv": ["environment_provenance", "unbound-isolated-worktree"],
                    "exit_code": None,
                }
            ]
            return unbound_checks, unbound_commands
        expected_init = Path(worktree_path) / "pipeline" / "__init__.py"

    expected = str(expected_init.resolve())
    argv = [
        sys.executable,
        "-c",
        "import pipeline, sys; sys.stdout.write(pipeline.__file__)",
    ]
    actual: str | None = None
    exit_code: int | None = None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        exit_code = proc.returncode
        raw = (proc.stdout or "").strip()
        if raw:
            actual = str(Path(raw.splitlines()[-1].strip()).resolve())
    except (OSError, subprocess.SubprocessError):
        pass

    passed = actual is not None and actual == expected
    checks: list[dict[str, Any]] = [
        {
            "name": "pipeline_import",
            "expected": expected,
            "actual": actual,
            "passed": passed,
        }
    ]
    commands: list[dict[str, Any]] = [{"argv": argv, "exit_code": exit_code}]
    return checks, commands


def _select_provenance_env(
    contract: Any,
    envs: Mapping[str, Any],
) -> str | None:
    """The env whose assertions provenance should run: default_env, else the
    single declared env, else ``None`` (ambiguous → no declared probe)."""
    default_env = str(getattr(contract, "default_env", "") or "")
    if default_env and default_env in envs:
        return default_env
    if len(envs) == 1:
        return next(iter(envs))
    return None


def _declared_env_provenance_checks(
    cwd_path: Path,
    contract: Any,
    ctx: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Run the declared env's assertions into ``(checks, commands)``; branch (b).

    Returns ``None`` when there is no usable contract / env / assertions, so the
    caller falls through to the non-core no-op (branch (c)). Never raises: any
    resolution / subprocess error degrades to ``None`` (no false failure).
    """
    if contract is None or ctx is None:
        return None
    try:
        envs = getattr(contract, "verification_envs", None)
        if not isinstance(envs, Mapping) or not envs:
            return None
        env_name = _select_provenance_env(contract, envs)
        if env_name is None:
            return None
        env_spec = envs.get(env_name)
        if not isinstance(env_spec, Mapping):
            return None
        raw_assertions = env_spec.get("assertions")
        if not isinstance(raw_assertions, (list, tuple)) or not raw_assertions:
            return None

        from pipeline.verification_env import run_env_assertions

        result = run_env_assertions(env_name, dict(env_spec), ctx)
        assertions = result.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            return None
        checks: list[dict[str, Any]] = [
            {
                "name": str(a.get("name", "")),
                "expected": a.get("expected"),
                "actual": a.get("actual"),
                "passed": bool(a.get("passed", False)),
            }
            for a in assertions
            if isinstance(a, Mapping)
        ]
        if not checks:
            return None
        commands: list[dict[str, Any]] = [
            {
                "argv": ["verification_env_assertions", env_name],
                "exit_code": 0 if result.get("all_passed") else 1,
            }
        ]
        return checks, commands
    except Exception:  # noqa: BLE001 — provenance probe must never raise/false-fail
        return None


def _non_core_noop_checks() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Branch (c): one informational, non-failing check (no false provenance fail)."""
    checks: list[dict[str, Any]] = [
        {
            "name": "environment_provenance",
            "expected": None,
            "actual": None,
            "passed": True,
        }
    ]
    commands: list[dict[str, Any]] = [
        {
            "argv": ["environment_provenance", "no-local-pipeline-or-assertions"],
            "exit_code": 0,
        }
    ]
    return checks, commands


def write_phase_verification_receipt(
    *,
    output_dir: Path | str | None,
    phase: str,
    round: int | None,
    cwd: Path | str,
    contract: Any = None,
    ctx: Any = None,
) -> Path | None:
    """Run the environment checks then write the phase's receipt.

    Thin convenience the ``implement`` / ``repair_changes`` handlers call
    *after* their work so both phases persist a receipt under identical
    conditions, always carrying a real check + command (never empty). The
    optional ``contract`` / ``ctx`` make provenance selection project-aware
    (see :func:`collect_environment_checks`); both default to ``None`` so the
    existing core ``pipeline_import`` / non-core no-op paths are unaffected.
    """
    checks, commands = collect_environment_checks(cwd, contract=contract, ctx=ctx)
    return write_verification_receipt(
        output_dir=output_dir,
        phase=phase,
        round=round,
        cwd=cwd,
        checks=checks,
        commands=commands,
    )


def _normalize_checks(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in checks:
        out.append(
            {
                "name": str(c.get("name", "")),
                "expected": c.get("expected"),
                "actual": c.get("actual"),
                "passed": bool(c.get("passed", False)),
            }
        )
    return out


def _normalize_commands(
    commands: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in commands:
        argv = c.get("argv")
        if isinstance(argv, (list, tuple)):
            argv_norm: Any = [str(a) for a in argv]
        else:
            argv_norm = str(argv) if argv is not None else ""
        out.append({"argv": argv_norm, "exit_code": c.get("exit_code")})
    return out


def write_verification_receipt(
    *,
    output_dir: Path | str | None,
    phase: str,
    round: int | None,
    cwd: Path | str,
    checks: Sequence[Mapping[str, Any]] = (),
    commands: Sequence[Mapping[str, Any]] = (),
    python: str | None = None,
    temp_env_outside_checkout: bool = True,
) -> Path | None:
    """Write one verification-environment receipt under the run output dir.

    Returns the written path, or ``None`` when ``output_dir`` is unset
    (a dry-run / isolation path with no run directory). Creates the
    ``verification_receipts/`` directory lazily. Writes ONLY under
    ``output_dir`` — never the ``cwd`` checkout.
    """
    if output_dir is None:
        return None
    round_n = int(round) if round is not None else 1
    run_dir = Path(output_dir)
    receipts_dir = run_dir / RECEIPTS_DIRNAME
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "phase": str(phase),
        "round": round_n,
        "kind": VERIFICATION_RECEIPT_KIND,
        "cwd": str(cwd),
        "python": python or _python_identity(),
        "checks": _normalize_checks(checks),
        "commands": _normalize_commands(commands),
        "temp_env_outside_checkout": bool(temp_env_outside_checkout),
    }
    path = receipts_dir / f"{phase}_round{round_n}.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def _normalize_assertions(
    assertions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in assertions:
        out.append(
            {
                "name": str(a.get("name", "")),
                "kind": str(a.get("kind", "")),
                "expected": a.get("expected"),
                "actual": a.get("actual"),
                "passed": bool(a.get("passed", False)),
                "detail": str(a.get("detail", "")),
            }
        )
    return out


def _normalize_dependencies(value: Any) -> list[dict[str, Any]]:
    """Normalise the command-receipt ``dependencies`` block (schema v2).

    Tolerant by design (the writer never trusts the caller's shape): a non-list
    ``value`` degrades to ``[]``, and each entry is coerced to the fixed key set
    ``name`` / ``path`` (str), ``head`` / ``changed_files_fingerprint`` (str or
    ``None``), ``dirty`` (bool or ``None``), ``changed_files_count`` (int or
    ``None``), ``depends_on`` (bool). A dependency's changed *file names* are
    never part of this shape — only the bool / count / fingerprint summary.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        head = entry.get("head")
        dirty = entry.get("dirty")
        out.append(
            {
                "name": str(entry.get("name", "")),
                "path": str(entry.get("path", "")),
                "head": str(head) if head is not None else None,
                "dirty": bool(dirty) if dirty is not None else None,
                "depends_on": bool(entry.get("depends_on", False)),
                "subject": _normalize_subject(entry.get("subject")),
            }
        )
    return out


def _normalize_subject(value: Any) -> dict[str, Any]:
    """Serialize a typed capture outcome at the durable receipt boundary."""
    # ``write_scheduled_command_receipt`` can snapshot an already persisted
    # flat receipt.  Accept that serialized representation as well as the
    # typed capture result used by new command executions.
    if isinstance(value, Mapping):
        normalized = subject_identity(value)
        if normalized is not None:
            value = normalized
        elif value.get("status") == "unavailable":
            return {"status": "unavailable", "reason": str(value.get("reason", "identity_unavailable"))}
    if isinstance(value, VerificationSubjectAvailable):
        value = value.identity
    if isinstance(value, VerificationSubjectIdentity) and is_usable_verification_subject(value):
        return {"status": "available", "identity": {
            "version": value.version, "object_format": value.object_format,
            "tree_oid": value.tree_oid, "observed_head_oid": value.observed_head_oid,
            "baseline_oid": value.baseline_oid,
        }}
    reason = value.reason if isinstance(value, VerificationSubjectUnavailable) else "identity_unavailable"
    return {"status": "unavailable", "reason": str(reason)}


def subject_identity(value: Any) -> VerificationSubjectIdentity | None:
    """Tolerantly parse a serialized available subject; malformed is unusable."""
    raw = value.get("identity") if isinstance(value, Mapping) and value.get("status") == "available" else None
    if not isinstance(raw, Mapping):
        return None
    identity = VerificationSubjectIdentity(
        version=raw.get("version"), object_format=raw.get("object_format"),
        tree_oid=raw.get("tree_oid"), observed_head_oid=raw.get("observed_head_oid"),
        baseline_oid=raw.get("baseline_oid"),
    )
    return identity if is_usable_verification_subject(identity) else None


def _sanitize_filename_stem(value: str) -> str:
    """Reduce an env name to a safe, flat filename stem.

    Keeps alphanumerics, ``-`` and ``_``; every other character (including path
    separators) becomes ``_`` so the receipt can never escape its directory.
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in value)
    return safe.strip("_") or "env"


def write_env_assertion_receipt(
    *,
    output_dir: Path | str | None,
    result: Mapping[str, Any],
) -> Path | None:
    """Write one operator env-assertion receipt under the run output dir.

    ``result`` is the dict returned by
    :func:`pipeline.verification_env.run_env_assertions` (``subject`` / ``cwd``
    / ``interpreter`` / ``env_overrides`` / ``assertions`` / ``all_passed``).
    Returns the written path, or ``None`` when ``output_dir`` is unset.

    Writes ONLY under ``<output_dir>/verification_env_receipts/`` — a directory
    distinct from ``verification_receipts/`` (which the evidence collector
    reads), so this receipt's :data:`VERIFICATION_ENV_KIND` never enters the
    evidence v1 bundle. Never writes under ``cwd`` / the source checkout.
    """
    if output_dir is None:
        return None
    subject_raw = result.get("subject") or {}
    subject = {
        "checkout": str(subject_raw.get("checkout", "")),
        "project": str(subject_raw.get("project", "")),
    }
    env_name = str(subject_raw.get("env", "")) or "env"
    overrides_raw = result.get("env_overrides") or {}
    env_overrides = (
        {str(k): str(v) for k, v in overrides_raw.items()}
        if isinstance(overrides_raw, Mapping)
        else {}
    )
    assertions_raw = result.get("assertions") or []
    assertions = (
        _normalize_assertions(assertions_raw) if isinstance(assertions_raw, (list, tuple)) else []
    )

    receipt = {
        "kind": VERIFICATION_ENV_KIND,
        "env": env_name,
        "subject": subject,
        "cwd": str(result.get("cwd", "")),
        "interpreter": str(result.get("interpreter", "")),
        "env_overrides": env_overrides,
        "assertions": assertions,
        "all_passed": bool(result.get("all_passed", False)),
        "temp_env_outside_checkout": True,
    }

    receipts_dir = Path(output_dir) / ENV_RECEIPTS_DIRNAME
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipts_dir / f"verify_env_{_sanitize_filename_stem(env_name)}.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def load_env_assertion_receipts(run_dir: Path | str) -> list[dict[str, Any]]:
    """Load every operator env-assertion receipt under the env-receipt dir.

    Reads ONLY ``<run_dir>/verification_env_receipts/`` (never the evidence
    collector's ``verification_receipts/``). Tolerant: returns ``[]`` when the
    directory is absent and skips unreadable / malformed files. Sorted by env.
    """
    receipts_dir = Path(run_dir) / ENV_RECEIPTS_DIRNAME
    if not receipts_dir.is_dir():
        return []
    receipts: list[dict[str, Any]] = []
    for entry in sorted(receipts_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            receipts.append(data)
    receipts.sort(key=lambda r: str(r.get("env", "")))
    return receipts


def load_verification_receipts(run_dir: Path | str) -> list[dict[str, Any]]:
    """Load every receipt under ``<run_dir>/verification_receipts/``.

    Tolerant: returns ``[]`` when the directory is absent and skips
    unreadable / malformed files. Sorted by ``(phase, round)``.
    """
    receipts_dir = Path(run_dir) / RECEIPTS_DIRNAME
    if not receipts_dir.is_dir():
        return []
    receipts: list[dict[str, Any]] = []
    for entry in sorted(receipts_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            receipts.append(data)
    receipts.sort(key=lambda r: (str(r.get("phase", "")), int(r.get("round", 0) or 0)))
    return receipts


@dataclass(frozen=True)
class EnvProvenanceFailure:
    """One failed environment-provenance check from a phase receipt.

    Carries enough operator-evidence to act without reading raw logs: the
    owning ``phase`` / ``round``, the failing ``check`` name (e.g.
    ``pipeline_import``), its ``expected`` / ``actual`` values, and the
    ``receipt_path`` of the ``verification_environment`` receipt it came from.
    """

    phase: str
    round: int
    check: str
    expected: str | None
    actual: str | None
    receipt_path: str


def environment_provenance_failures(
    run_dir: Path | str,
) -> tuple[EnvProvenanceFailure, ...]:
    """Read-only scan of ``verification_environment`` receipts for failed checks.

    Reads the same receipts as :func:`load_verification_receipts` and emits one
    :class:`EnvProvenanceFailure` per check whose ``passed`` is not truthy
    (``verification_environment`` receipts do not store an ``all_passed`` rollup,
    so failure is derived from ``checks[].passed`` directly). A receipt with no
    failing check yields nothing.

    Pure and never-raising: any IO / JSON error degrades to ``()`` (consumed by
    projections that must not raise). The receipt path is reconstructed from the
    writer convention ``<run_dir>/verification_receipts/<phase>_round<N>.json``.
    """
    try:
        receipts = load_verification_receipts(run_dir)
        receipts_dir = Path(run_dir) / RECEIPTS_DIRNAME
        failures: list[EnvProvenanceFailure] = []
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            kind = str(receipt.get("kind", VERIFICATION_RECEIPT_KIND))
            if kind != VERIFICATION_RECEIPT_KIND:
                continue
            checks = receipt.get("checks")
            if not isinstance(checks, list):
                continue
            phase = str(receipt.get("phase", ""))
            try:
                round_n = int(receipt.get("round", 0) or 0)
            except (TypeError, ValueError):
                round_n = 0
            receipt_path = str(receipts_dir / f"{phase}_round{round_n}.json")
            for check in checks:
                if not isinstance(check, Mapping):
                    continue
                if check.get("passed"):
                    continue
                expected = check.get("expected")
                actual = check.get("actual")
                failures.append(
                    EnvProvenanceFailure(
                        phase=phase,
                        round=round_n,
                        check=str(check.get("name", "")),
                        expected=str(expected) if expected is not None else None,
                        actual=str(actual) if actual is not None else None,
                        receipt_path=receipt_path,
                    )
                )
        return tuple(failures)
    except (OSError, ValueError, TypeError):
        return ()


def write_command_receipt(
    *,
    output_dir: Path | str | None,
    result: Mapping[str, Any],
) -> Path | None:
    """Write one native command-receipt under the run output dir.

    ``result`` is the flat dict returned by
    :func:`pipeline.verification_command.run_command` (``command`` / ``env`` /
    ``cwd`` / ``placeholders`` / ``argv`` / ``env_overrides`` / ``assertions`` /
    ``exit_code`` / ``duration_s`` / ``stdout_tail`` / ``stderr_tail`` /
    ``log_path`` / ``parity`` / ``git`` / ``dependencies``). Returns the written
    path, or ``None`` when ``output_dir`` is unset.

    Writes ONLY under ``<output_dir>/verification_command_receipts/`` — a
    directory distinct from ``verification_receipts/`` (which the evidence
    collector reads), so this receipt's :data:`VERIFICATION_COMMAND_KIND` never
    enters the evidence v1 bundle. Never writes under the source checkout.
    """
    if output_dir is None:
        return None

    command = str(result.get("command", "")) or "command"

    placeholders_raw = result.get("placeholders")
    placeholders = (
        {
            "checkout": str(placeholders_raw.get("checkout", "")),
            "project": str(placeholders_raw.get("project", "")),
        }
        if isinstance(placeholders_raw, Mapping)
        else {"checkout": "", "project": ""}
    )

    argv_raw = result.get("argv")
    argv = [str(a) for a in argv_raw] if isinstance(argv_raw, (list, tuple)) else []

    overrides_raw = result.get("env_overrides")
    env_overrides = (
        {str(k): str(v) for k, v in overrides_raw.items()}
        if isinstance(overrides_raw, Mapping)
        else {}
    )

    assertions_raw = result.get("assertions")
    assertions = (
        _normalize_assertions(assertions_raw) if isinstance(assertions_raw, (list, tuple)) else []
    )

    git_raw = result.get("git")
    git_raw = git_raw if isinstance(git_raw, Mapping) else {}
    git = {
        "checkout_head": git_raw.get("checkout_head"),
        "baseline_head": git_raw.get("baseline_head"),
    }

    log_path = result.get("log_path")

    receipt = {
        "schema_version": COMMAND_RECEIPT_SCHEMA_VERSION,
        "kind": VERIFICATION_COMMAND_KIND,
        "command": command,
        "env": str(result.get("env", "")),
        "cwd": str(result.get("cwd", "")),
        "placeholders": placeholders,
        "argv": argv,
        "env_overrides": env_overrides,
        "assertions": assertions,
        "exit_code": result.get("exit_code"),
        "duration_s": result.get("duration_s"),
        "stdout_tail": str(result.get("stdout_tail", "")),
        "stderr_tail": str(result.get("stderr_tail", "")),
        "log_path": str(log_path) if log_path is not None else None,
        "parity": str(result.get("parity", "absolute")),
        "detail": str(result.get("detail", "")),
        "git": git,
        "subject": _normalize_subject(result.get("subject")),
        "dependencies": _normalize_dependencies(result.get("dependencies")),
    }

    receipts_dir = Path(output_dir) / COMMAND_RECEIPTS_DIRNAME
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipts_dir / f"{_sanitize_filename_stem(command)}.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return path


def write_scheduled_command_receipt(
    *,
    output_dir: Path | str | None,
    result: Mapping[str, Any],
    hook: str,
    phase: str,
) -> Path | None:
    """Write the latest receipt and one immutable scheduled-execution copy.

    The flat ``<command>.json`` receipt remains the sole authoritative input to
    readiness and delivery. The nested execution copy exists only for the
    scheduled-gate ledger to reference; command-receipt loaders deliberately do
    not recurse into this directory.
    """
    latest = write_command_receipt(output_dir=output_dir, result=result)
    if latest is None:
        return None

    command = str(result.get("command", "")) or "command"
    identity = "--".join(
        _sanitize_filename_stem(value)
        for value in (command, hook, phase or "none")
    )
    executions_dir = latest.parent / COMMAND_RECEIPT_EXECUTIONS_DIRNAME
    executions_dir.mkdir(parents=True, exist_ok=True)
    encoded = latest.read_text(encoding="utf-8")
    attempt = 1
    while True:
        evidence = executions_dir / f"{identity}--{attempt:04d}.json"
        try:
            with evidence.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        except FileExistsError:
            attempt += 1
            continue
        return evidence


def load_command_receipts(run_dir: Path | str) -> list[dict[str, Any]]:
    """Load every command-receipt under the command-receipt dir.

    Reads ONLY ``<run_dir>/verification_command_receipts/`` (never the evidence
    collector's ``verification_receipts/``). Tolerant: returns ``[]`` when the
    directory is absent and skips unreadable / malformed files. Sorted by
    ``command``.
    """
    receipts_dir = Path(run_dir) / COMMAND_RECEIPTS_DIRNAME
    if not receipts_dir.is_dir():
        return []
    receipts: list[dict[str, Any]] = []
    for entry in sorted(receipts_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            receipts.append(data)
    receipts.sort(key=lambda r: str(r.get("command", "")))
    return receipts


def command_receipt_passed(receipt: Mapping[str, Any] | None) -> bool:
    """Return the execution pass rollup for one command receipt.

    This is deliberately not a freshness verdict.  It reports a command whose
    execution and assertions passed even if its identity is ``unverifiable``;
    callers that need freshness must use :func:`classify_receipt` with current
    subject and dependency identities.  This preserves execution-event and
    evidence-summary semantics without allowing unavailable proof to satisfy a
    readiness or routing decision.
    """
    from pipeline.verification_failure import classify_receipt

    return classify_receipt(receipt).status in {"present", "unverifiable"}


def summarize_command_receipts(run_dir: Path | str) -> list[dict[str, Any]]:
    """Compact per-command digest for CLI / reviewer context.

    One entry per receipt: ``command`` / ``env`` / ``exit_code`` / ``parity``,
    a ``passed`` rollup (the authoritative :func:`command_receipt_passed`: exit 0
    AND every declared assertion passed AND an empty execution ``detail``), and
    ``has_baseline`` (whether a differential baseline head was persisted).
    """
    summaries: list[dict[str, Any]] = []
    for receipt in load_command_receipts(run_dir):
        passed = command_receipt_passed(receipt)
        git = receipt.get("git") or {}
        git = git if isinstance(git, dict) else {}
        summaries.append(
            {
                "command": receipt.get("command"),
                "env": receipt.get("env"),
                "exit_code": receipt.get("exit_code"),
                "parity": receipt.get("parity", "absolute"),
                "passed": passed,
                "has_baseline": bool(git.get("baseline_head")),
            }
        )
    return summaries


def summarize_verification_receipts(run_dir: Path | str) -> list[dict[str, Any]]:
    """Brief per-receipt summary for evidence / reviewer context (T7).

    One compact entry per receipt: ``phase`` / ``round`` / ``kind`` plus
    check pass-counts, an ``all_passed`` rollup, and a command count. The
    full receipts stay on disk; this is the digest reviewers read.
    """
    summaries: list[dict[str, Any]] = []
    for receipt in load_verification_receipts(run_dir):
        checks = receipt.get("checks") or []
        checks = checks if isinstance(checks, list) else []
        passed = sum(1 for c in checks if isinstance(c, dict) and c.get("passed"))
        commands = receipt.get("commands") or []
        commands = commands if isinstance(commands, list) else []
        summaries.append(
            {
                "phase": receipt.get("phase"),
                "round": receipt.get("round"),
                "kind": receipt.get("kind", VERIFICATION_RECEIPT_KIND),
                "checks_total": len(checks),
                "checks_passed": passed,
                "all_passed": len(checks) == passed,
                "commands_run": len(commands),
                "temp_env_outside_checkout": bool(
                    receipt.get("temp_env_outside_checkout", True),
                ),
            }
        )
    return summaries
