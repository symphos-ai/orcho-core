"""
verification_env.py — Stage 2 generic engine that *executes* the declared
env-assertions of one ``verification_env``.

Unlike :mod:`pipeline.verification_contract` (read-only Stage 1 projection), this
module runs subprocesses to prove facts about a *declared* checkout/project — not
about the bare host. The load-bearing rule: when an env declares no ``cwd``, the
effective working directory is ``ctx.checkout`` (fallback ``ctx.project``), never
the CLI/test process cwd. Import/version subprocesses run *from* that effective
cwd so ``sys.path[0]`` resolves the declared package, and relative path/file
assertions resolve against it too.

The assertion vocabulary is generic and dispatched by key — Python is one
interpreter, not a hardcoded path:

- ``{"import": M, "path_equals": p}`` / ``{"import": M, "path_under": d}``
- ``{"path_exists": p}``
- ``{"file_exists": p}``
- ``{"command_exists": name}``
- ``{"version": [argv...], "contains": substr}``

Every assertion yields ``{name, kind, expected, actual, passed, detail}``. An
unknown key is a failed check, never a crash. Subprocesses run with a timeout and
never raise outward: an ``OSError`` / ``SubprocessError`` (incl. timeout)
degrades to ``passed=False`` with a ``detail``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any

from pipeline.verification_contract import PlaceholderContext, resolve_placeholders

# Subprocess wall-clock budget. A hung interpreter or version tool degrades to a
# failed assertion rather than blocking the run.
_TIMEOUT_S = 60


def resolve_env_runtime(
    env_spec: dict[str, Any],
    ctx: PlaceholderContext,
) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """Resolve the subprocess runtime declared by one ``verification_env``.

    Shared DRY seam between the Stage 2 env-assertion engine
    (:func:`run_env_assertions`) and the Stage 3 command executor
    (:func:`pipeline.verification_command.run_command`). Returns
    ``(python, eff_cwd, sub_env, overrides)`` where:

    * ``python`` — interpreter path, placeholder-resolved; defaults to the
      current ``sys.executable``.
    * ``eff_cwd`` — effective working dir: declared ``cwd`` (placeholder-
      resolved), else ``ctx.checkout``, else ``ctx.project``. This is ONLY the
      subprocess cwd; it is not the git provenance subject. Under per-run
      isolation a cwd that resolves to the canonical sibling is redirected to the
      worktree checkout (fail-closed; ADR 0112 §3), never silently run against the
      clean source tree.
    * ``sub_env`` — ``os.environ`` merged with placeholder-resolved ``env``
      overrides.
    * ``overrides`` — the resolved ``env`` overrides on their own (for receipts).
    """
    python_decl = env_spec.get("python")
    python = (
        resolve_placeholders(str(python_decl), ctx)
        if python_decl
        else sys.executable
    )

    cwd_decl = env_spec.get("cwd")
    if cwd_decl:
        eff_cwd = resolve_placeholders(str(cwd_decl), ctx)
    elif ctx.checkout:
        eff_cwd = ctx.checkout
    else:
        eff_cwd = ctx.project

    # Fail-closed cwd binding (ADR 0112 §3): when the repo runs in an isolated
    # per-run worktree, a cwd that resolves to the canonical sibling
    # (``ctx.project`` fallback or an explicit ``{project}`` cwd) is redirected to
    # the worktree checkout — and an unbindable worktree raises rather than
    # verifying a clean tree vacuously. No-op for single-checkout runs and for a
    # cwd already pointing at the worktree.
    isolated = getattr(ctx, "isolated_source", None)
    if isolated is not None:
        from pipeline.engine.worktree_source import resolve_isolated_repo_source

        repo_name = os.path.basename(isolated.source_repo_path) or "verification-env"
        eff_cwd = resolve_isolated_repo_source(
            repo_name=repo_name, candidate=eff_cwd, isolated=isolated,
        )

    overrides: dict[str, str] = {}
    raw_env = env_spec.get("env")
    if isinstance(raw_env, dict):
        for key, value in raw_env.items():
            overrides[str(key)] = resolve_placeholders(str(value), ctx)
    sub_env = dict(os.environ)
    sub_env.update(overrides)

    return python, eff_cwd, sub_env, overrides


def run_env_assertions(
    env_name: str,
    env_spec: dict[str, Any],
    ctx: PlaceholderContext,
) -> dict[str, Any]:
    """Execute the declared assertions of one ``verification_env``.

    ``env_spec`` is the raw ``verification_envs[env_name]`` dict. Recognised
    keys: ``python`` (interpreter path, placeholder-resolved; defaults to the
    current ``sys.executable``), ``cwd`` (effective working dir; defaults to
    ``ctx.checkout`` then ``ctx.project``), ``env`` (process env overrides,
    values placeholder-resolved), and ``assertions`` (the dispatch list).

    Returns a plain dict with ``subject`` (env + checkout/project), ``cwd``
    (the effective resolved dir), ``interpreter`` (``"<version> (<exe>)"`` when
    it resolves, else the raw path), ``env_overrides`` (resolved dict),
    ``assertions`` (per-assertion result dicts), and ``all_passed``.
    """
    python, eff_cwd, sub_env, overrides = resolve_env_runtime(env_spec, ctx)

    interpreter = _interpreter_identity(python, eff_cwd, sub_env)

    results: list[dict[str, Any]] = []
    raw_assertions = env_spec.get("assertions")
    if isinstance(raw_assertions, (list, tuple)):
        for raw in raw_assertions:
            results.append(
                _evaluate(
                    raw,
                    python=python,
                    eff_cwd=eff_cwd,
                    sub_env=sub_env,
                    ctx=ctx,
                ),
            )

    return {
        "subject": {
            "env": env_name,
            "checkout": ctx.checkout,
            "project": ctx.project,
        },
        "cwd": eff_cwd,
        "interpreter": interpreter,
        "env_overrides": overrides,
        "assertions": results,
        "all_passed": all(r["passed"] for r in results),
    }


def _evaluate(
    raw: Any,
    *,
    python: str,
    eff_cwd: str,
    sub_env: dict[str, str],
    ctx: PlaceholderContext,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _result(
            "<malformed>", "unknown", None, None, False,
            f"assertion must be a dict, got {type(raw).__name__}",
        )

    if "import" in raw:
        module = resolve_placeholders(str(raw["import"]), ctx)
        if "path_equals" in raw:
            expected = resolve_placeholders(str(raw["path_equals"]), ctx)
            return _import_assert(
                module, expected, "import_path_equals",
                python=python, eff_cwd=eff_cwd, sub_env=sub_env,
            )
        if "path_under" in raw:
            expected = resolve_placeholders(str(raw["path_under"]), ctx)
            return _import_assert(
                module, expected, "import_path_under",
                python=python, eff_cwd=eff_cwd, sub_env=sub_env,
            )
        return _result(
            module, "unknown", None, None, False,
            "import assertion needs path_equals or path_under",
        )

    if "path_exists" in raw:
        return _path_assert(
            resolve_placeholders(str(raw["path_exists"]), ctx),
            eff_cwd, "path_exists",
        )
    if "file_exists" in raw:
        return _path_assert(
            resolve_placeholders(str(raw["file_exists"]), ctx),
            eff_cwd, "file_exists",
        )

    if "command_exists" in raw:
        name = resolve_placeholders(str(raw["command_exists"]), ctx)
        found = shutil.which(name, path=sub_env.get("PATH"))
        return _result(
            name, "command_exists", "present", found, found is not None,
            "" if found else "not found on PATH",
        )

    if "version" in raw:
        return _version_assert(raw, eff_cwd=eff_cwd, sub_env=sub_env, ctx=ctx)

    keys = sorted(str(k) for k in raw)
    return _result(
        str(keys), "unknown", None, None, False,
        f"unknown assertion keys: {keys}",
    )


def _import_assert(
    module: str,
    expected: str,
    kind: str,
    *,
    python: str,
    eff_cwd: str,
    sub_env: dict[str, str],
) -> dict[str, Any]:
    code = (
        "import importlib, sys\n"
        f"m = importlib.import_module({module!r})\n"
        "sys.stdout.write(m.__file__ or '')"
    )
    rc, out, _err, exc = _run([python, "-c", code], eff_cwd, sub_env)
    if exc is not None:
        return _result(module, kind, expected, None, False, f"subprocess error: {exc}")
    if rc != 0:
        return _result(
            module, kind, expected, None, False,
            f"import failed (rc={rc}): {_err.strip()[:200]}",
        )
    actual = os.path.realpath(out.strip()) if out.strip() else None
    exp = os.path.realpath(expected) if expected else expected
    if kind == "import_path_equals":
        passed = actual is not None and actual == exp
    else:
        passed = actual is not None and _is_under(actual, exp)
    return _result(
        module, kind, exp, actual, passed,
        "" if passed else "path mismatch",
    )


def _path_assert(path: str, eff_cwd: str, kind: str) -> dict[str, Any]:
    abspath = path if os.path.isabs(path) else os.path.join(eff_cwd or "", path)
    passed = (
        os.path.isfile(abspath)
        if kind == "file_exists"
        else os.path.exists(abspath)
    )
    return _result(
        path, kind, "exists", abspath, passed,
        "" if passed else "does not exist",
    )


def _version_assert(
    raw: dict[str, Any],
    *,
    eff_cwd: str,
    sub_env: dict[str, str],
    ctx: PlaceholderContext,
) -> dict[str, Any]:
    argv_raw = raw.get("version")
    contains = resolve_placeholders(str(raw.get("contains", "")), ctx)
    if not isinstance(argv_raw, (list, tuple)) or not argv_raw:
        return _result(
            "version", "version_contains", contains, None, False,
            "version must be a non-empty argv list",
        )
    argv = [resolve_placeholders(str(a), ctx) for a in argv_raw]
    rc, out, err, exc = _run(argv, eff_cwd, sub_env)
    if exc is not None:
        return _result(
            argv[0], "version_contains", contains, None, False,
            f"subprocess error: {exc}",
        )
    combined = (out or "") + (err or "")
    passed = bool(contains) and contains in combined
    detail = "" if passed else (
        "substring not found" if contains else "empty contains substring"
    )
    return _result(
        argv[0], "version_contains", contains, combined.strip()[:200],
        passed, detail,
    )


def _interpreter_identity(
    python: str, eff_cwd: str, sub_env: dict[str, str],
) -> str:
    code = (
        "import sys\n"
        "sys.stdout.write(sys.version.split()[0] + '\\n' + sys.executable)"
    )
    rc, out, _err, _exc = _run([python, "-c", code], eff_cwd, sub_env)
    if rc == 0 and out.strip():
        lines = out.strip().splitlines()
        version = lines[0] if lines else ""
        exe = lines[1] if len(lines) > 1 else python
        return f"{version} ({exe})"
    return python


def _is_under(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def _run(
    argv: list[str], cwd: str, env: dict[str, str],
) -> tuple[int | None, str, str, str | None]:
    try:
        proc = subprocess.run(  # noqa: S603 — argv is declared, not shell
            argv,
            cwd=cwd or None,
            env=env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr, None


def _result(
    name: str,
    kind: str,
    expected: Any,
    actual: Any,
    passed: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "detail": detail,
    }
