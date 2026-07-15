"""sdk.verify — execute declared verification envs and commands.

The public surface ties together already-built pieces: the generic assertion
engine (:mod:`pipeline.verification_env`), the native command executor
(:mod:`pipeline.verification_command`), the receipt writers
(:mod:`pipeline.evidence.verification_receipt`), and the read-only contract
projection (:mod:`pipeline.verification_contract`). Each entry point resolves a
run, proves the run belongs to the requested project, and loads the project's
contract.

Subject separation: the contract is always loaded from the *canonical* project
(``{project}``), while all verification entry points resolve one fail-closed
physical subject (`{checkout}`) from run metadata. Git provenance on a
command-receipt is taken from that subject, never from the command's working
directory.

Boundary discipline (ADR 0021): returns a typed result, raises typed
:class:`~sdk.errors.OrchoError` subclasses, never prints, never calls
``sys.exit``. A resolution failure (project↔run mismatch, missing contract,
unknown env / command, empty required set) raises *before* any receipt is
written, so a misleading receipt can never land.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sdk.errors import OrchoError
from sdk.runs import _CWD_DEFAULT, find_run, load_meta

if TYPE_CHECKING:
    from pipeline.verification_contract import VerificationContract
    from sdk.types import RunRef


class VerifyEnvError(OrchoError):
    """A verify precondition failed; nothing was written.

    Raised for project↔run mismatch, a run without a recorded project, a
    missing verification contract, an unknown / unset env, an unknown command,
    or an empty required set. The ``exit_code`` of 2 distinguishes these
    operator-fixable errors from a generic failure.
    """

    exit_code = 2


SubjectSource = Literal[
    "run_metadata", "controller_override", "canonical_non_isolated",
]


@dataclass(frozen=True, slots=True)
class VerificationSubject:
    """The physical checkout a verification run is allowed to prove."""

    checkout: str
    source: SubjectSource


@dataclass(frozen=True, slots=True)
class VerifyEnvResult:
    """Typed outcome of one ``orcho verify env`` execution."""

    env: str
    run_id: str
    receipt_path: Path | None
    all_passed: bool
    subject: dict[str, str] = field(default_factory=dict)
    assertions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Typed result of executing one declared command."""

    command: str
    env: str
    exit_code: int | None
    passed: bool
    parity: str
    receipt_path: Path | None
    duration_s: float | None
    stdout_tail: str
    stderr_tail: str
    checkout_head: str | None
    baseline_head: str | None
    # Cross-repo provenance tags this command was tested against, one per
    # depended-on declared dependency (``<name>@<short-head>`` with a ``+dirty``
    # suffix when the dependency tree was dirty). Empty when the command depends
    # on no declared dependency. Derived from the receipt's ``dependencies``
    # block (entries with ``depends_on`` true). ADR 0084.
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerifyRunResult:
    """Typed outcome of one ``orcho verify run`` execution."""

    run_id: str
    outcomes: list[CommandOutcome]
    all_passed: bool
    subject_checkout: str = ""
    subject_source: SubjectSource = "canonical_non_isolated"


@dataclass(frozen=True, slots=True)
class VerifyListResult:
    """Typed outcome of ``orcho verify list`` — declared commands, no execution."""

    run_id: str
    commands: list[dict[str, Any]]
    subject_checkout: str = ""
    subject_source: SubjectSource = "canonical_non_isolated"


def _resolve_run_project_contract(
    *,
    project: str | None,
    run_id: str | None,
    workspace: str | None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> tuple[RunRef, Path, str, VerificationContract | None, str, dict[str, Any]]:
    """Resolve run, validate project match, load contract; raise before any write.

    Shared resolution seam for :func:`verify_env`, :func:`verify_list`, and
    :func:`verify_run`. Returns ``(ref, run_dir, project_dir, contract, ws,
    meta)`` where ``contract`` may be ``None`` (undeclared) — each caller decides
    what shape of contract it requires. Raises :class:`VerifyEnvError` for a
    project↔run mismatch or a run with no recorded project.

    ``cwd`` is forwarded to :func:`find_run` for run discovery. It defaults to
    the walk-up sentinel (unchanged behaviour for the CLI verify callers);
    embedders that must NOT bind to an arbitrary process cwd — the MCP server —
    pass ``cwd=None`` to disable walk-up, exactly as the other SDK read
    accessors do.
    """
    from core.infra.platform import workspace_dir as resolve_workspace
    from pipeline.plugins import load_plugin
    from pipeline.verification_contract import VerificationContract

    ref = find_run(run_id, workspace=workspace, cwd=cwd)
    run_dir = ref.run_dir
    meta = load_meta(run_dir)
    meta_project = meta.get("project")

    if project is not None:
        if not meta_project or Path(project).resolve() != Path(meta_project).resolve():
            raise VerifyEnvError(
                "project does not match run; pass explicit --run-id",
            )
        project_dir = project
    else:
        if not meta_project:
            raise VerifyEnvError(
                "run has no recorded project; pass --project",
            )
        project_dir = str(meta_project)

    plugin = load_plugin(project_dir)
    contract = VerificationContract.from_plugin(plugin)
    ws = workspace or resolve_workspace() or project_dir
    return ref, run_dir, project_dir, contract, str(ws), meta


def _is_readable_directory(path: Path) -> bool:
    """Return whether ``path`` is a directory usable as a verification cwd.

    Kept as a seam because permission bits are not deterministic under every
    test runner (notably when it runs as an elevated user).
    """
    return path.is_dir() and os.access(path, os.R_OK | os.X_OK)


def _same_checkout(left: Path, right: Path) -> bool:
    """Compare checkout identities without requiring either path to exist."""
    return left.resolve(strict=False) == right.resolve(strict=False)


def _checked_checkout(value: str, *, label: str) -> Path:
    path = Path(value)
    if not _is_readable_directory(path):
        raise VerifyEnvError(f"{label} is not a readable directory: {value!r}")
    return path.resolve()


def _verification_identity_meta(
    *, run_dir: Path, meta: Mapping[str, Any], project_dir: str,
) -> Mapping[str, Any]:
    """Resolve retained correction identity through durable parent lineage.

    A correction child can be inspected before its own session has persisted
    the reused ``worktree`` block. In that narrow state, the child already
    records a correction follow-up relationship, so its physical subject is
    the nearest parent metadata carrying the retained identity. Arbitrary
    runs and mismatched projects never inherit a parent subject.
    """
    current_dir = run_dir
    current_meta = meta
    visited = {run_dir.name}

    while not isinstance(current_meta.get("worktree"), Mapping):
        if (
            current_meta.get("profile") != "correction"
            or current_meta.get("resume_mode") != "followup"
        ):
            return current_meta

        parent_id = current_meta.get("parent_run_id")
        if not isinstance(parent_id, str) or not parent_id.strip():
            raise VerifyEnvError(
                "correction follow-up metadata is missing parent_run_id",
            )
        if Path(parent_id).name != parent_id:
            raise VerifyEnvError("correction follow-up parent_run_id is invalid")
        if parent_id in visited:
            raise VerifyEnvError("correction follow-up lineage contains a cycle")
        visited.add(parent_id)

        parent_dir = current_dir.parent / parent_id
        recorded_parent_dir = current_meta.get("parent_run_dir")
        if (
            isinstance(recorded_parent_dir, str)
            and recorded_parent_dir
            and not _same_checkout(Path(recorded_parent_dir), parent_dir)
        ):
            raise VerifyEnvError(
                "correction follow-up parent_run_dir conflicts with parent_run_id",
            )
        if not (parent_dir / "meta.json").is_file():
            raise VerifyEnvError(
                f"correction follow-up parent metadata is unavailable: {parent_id}",
            )
        try:
            parent_meta = load_meta(parent_dir)
        except Exception as exc:  # noqa: BLE001 — normalise SDK read failure
            raise VerifyEnvError(
                f"correction follow-up parent metadata is unavailable: {parent_id}",
            ) from exc

        parent_project = parent_meta.get("project")
        if (
            not isinstance(parent_project, str)
            or not _same_checkout(Path(parent_project), Path(project_dir))
        ):
            raise VerifyEnvError(
                "correction follow-up parent project does not match run project",
            )
        current_dir = parent_dir
        current_meta = parent_meta

    return current_meta


def resolve_verification_subject(
    *,
    meta: Mapping[str, Any],
    project_dir: str,
    subject_checkout: str | None = None,
) -> VerificationSubject:
    """Resolve one physical verification subject without canonical fallback.

    An explicitly non-isolated run is the only metadata shape that authorises
    the canonical project.  Recorded isolated identity must remain readable and
    exact. Metadata without a recorded identity requires a non-canonical
    controller override.
    """
    canonical = _checked_checkout(project_dir, label="canonical project")
    override = (
        _checked_checkout(subject_checkout, label="subject_checkout override")
        if subject_checkout
        else None
    )
    worktree = meta.get("worktree")
    isolation = worktree.get("isolation") if isinstance(worktree, Mapping) else None
    recorded_path = worktree.get("path") if isinstance(worktree, Mapping) else None

    if isolation == "off":
        if override is not None and not _same_checkout(override, canonical):
            raise VerifyEnvError(
                "subject_checkout override conflicts with non-isolated run identity",
            )
        return VerificationSubject(str(canonical), "canonical_non_isolated")

    if isinstance(recorded_path, str) and recorded_path:
        recorded = _checked_checkout(recorded_path, label="recorded isolated checkout")
        if _same_checkout(recorded, canonical):
            raise VerifyEnvError("isolated run metadata points at the canonical project")
        if override is not None and not _same_checkout(override, recorded):
            raise VerifyEnvError(
                "subject_checkout override conflicts with recorded isolated checkout",
            )
        return VerificationSubject(str(recorded), "run_metadata")

    if isolation is not None:
        raise VerifyEnvError("isolated run metadata has no recorded worktree path")

    if override is None:
        raise VerifyEnvError(
            "run metadata does not establish a verification subject; "
            "a non-canonical subject_checkout override is required",
        )
    if _same_checkout(override, canonical):
        raise VerifyEnvError(
            "subject_checkout override cannot turn ambiguous metadata into canonical subject",
        )
    return VerificationSubject(str(override), "controller_override")


def _dependency_tags(deps: Any) -> tuple[str, ...]:
    """Compact ``<name>@<short-head>`` tags for depended-on dependencies.

    Reads a receipt's ``dependencies`` block tolerantly: only entries with a
    truthy ``depends_on`` and a recorded ``head`` contribute, in stored order. A
    ``+dirty`` suffix marks a dirty dependency tree. Non-list / junk → ``()``.
    """
    if not isinstance(deps, (list, tuple)):
        return ()
    tags: list[str] = []
    for entry in deps:
        if not isinstance(entry, Mapping) or not entry.get("depends_on"):
            continue
        name = entry.get("name")
        head = entry.get("head")
        if not name or not head:
            continue
        tag = f"{name}@{str(head)[:7]}"
        if entry.get("dirty"):
            tag += "+dirty"
        tags.append(tag)
    return tuple(tags)


def _baseline_head_for_meta(meta: dict[str, Any]) -> str | None:
    """The run worktree's base ref (``meta['worktree']['base_ref']``) or ``None``.

    A differential gate compares the resolved subject's HEAD against this
    baseline, both relative to the same recorded worktree identity.
    """
    worktree = meta.get("worktree")
    if isinstance(worktree, dict):
        base_ref = worktree.get("base_ref")
        if base_ref:
            return str(base_ref)
    return None


def _commands_for_gate_sets(contract: VerificationContract, refs: tuple[str, ...]) -> set[str]:
    commands: set[str] = set()
    for ref in refs:
        gate_set = contract.gate_sets.get(ref)
        if gate_set is not None:
            commands.update(gate_set.commands)
    return commands


def manual_or_operator_only_commands(contract: VerificationContract) -> set[str]:
    """Raw set of commands marked ``manual_only`` or closed behind an
    unrequested operator gate-set, BEFORE subtracting ``required``/automatic.

    This raw view is what required-receipt auto-run needs: a command that is
    both ``required`` and ``manual_only`` must stay manual (never auto-run),
    whereas ``orcho verify run`` historically subtracts ``required`` and runs
    it. Keep raw membership here and let each caller decide whether to apply
    the ``- automatic - required`` subtraction.
    """
    manual: set[str] = set()
    for sched in contract.schedule:
        if sched.hook != "manual_only":
            continue
        manual.update(sched.commands)
        manual.update(_commands_for_gate_sets(contract, sched.gate_sets))

    requested_operator_sets = set(contract.operator_sets)
    operator_only: set[str] = set()
    for rule in contract.selection:
        if rule.kind != "operator":
            continue
        gated_sets = tuple(s for s in rule.include if s not in requested_operator_sets)
        operator_only.update(_commands_for_gate_sets(contract, gated_sets))

    return manual | operator_only


def _manual_or_operator_only_commands(contract: VerificationContract) -> set[str]:
    """Commands that should not run in accidental ``verify run`` all-mode.

    Stage 3's original "run every declared command" remains the fallback for
    contracts without Stage 4 scheduling. Once a contract explicitly marks a
    command as ``manual_only`` or places it behind an operator opt-in set, the
    command is intentionally available for explicit invocation but should not be
    pulled into a bare ``orcho verify run``.
    """
    automatic: set[str] = set(contract.required)
    for sched in contract.schedule:
        if sched.hook == "manual_only":
            continue
        automatic.update(sched.commands)
        automatic.update(_commands_for_gate_sets(contract, sched.gate_sets))

    return manual_or_operator_only_commands(contract) - automatic - set(contract.required)


def _default_verify_run_names(
    contract: VerificationContract, *, include_manual: bool,
) -> list[str]:
    names = sorted(contract.commands)
    if include_manual:
        return names
    excluded = _manual_or_operator_only_commands(contract)
    return [name for name in names if name not in excluded]


def verify_env(
    *,
    project: str | None = None,
    env: str | None = None,
    run_id: str | None = None,
    workspace: str | None = None,
    subject_checkout: str | None = None,
) -> VerifyEnvResult:
    """Execute one verification_env's assertions and write an env-receipt.

    Resolution order (intentional — the receipt only lands once every check
    below passes):

    1. Resolve the run (``run_id`` or newest) and load its ``meta.json``.
    2. Determine the project. With an explicit ``project``, the run must
       belong to it: both paths are normalised via ``Path.resolve()`` and
       compared to ``meta['project']``; a missing / empty / mismatched
       ``meta['project']`` raises :class:`VerifyEnvError` with no write.
       Without ``project``, the run's ``meta['project']`` is used (a missing
       value raises).
    3. Load the project plugin and project a verification contract; a
       missing contract / no ``verification_envs`` raises.
    4. Select the env (explicit ``env`` else ``contract.default_env``); an
       empty / unknown env raises.
    5. Resolve the physical subject before assertions or receipt writes.
    6. Build a :class:`PlaceholderContext` with the resolved ``checkout``.
    7. Run the assertions and write the env-receipt under the run directory.
    """
    from pipeline.evidence.verification_receipt import write_env_assertion_receipt
    from pipeline.verification_contract import placeholder_context_for
    from pipeline.verification_env import run_env_assertions

    ref, run_dir, project_dir, contract, ws, meta = _resolve_run_project_contract(
        project=project, run_id=run_id, workspace=workspace,
    )
    if contract is None or not contract.verification_envs:
        raise VerifyEnvError(
            f"no verification contract declared for project {project_dir!r}",
        )

    env_name = env or contract.default_env
    if not env_name or env_name not in contract.verification_envs:
        known = sorted(contract.verification_envs)
        raise VerifyEnvError(
            f"verification_env {env_name!r} is not declared (known: {known!r})",
        )
    env_spec = contract.verification_envs[env_name]
    identity_meta = _verification_identity_meta(
        run_dir=run_dir, meta=meta, project_dir=project_dir,
    )
    subject_resolution = resolve_verification_subject(
        meta=identity_meta,
        project_dir=project_dir,
        subject_checkout=subject_checkout,
    )

    ctx = placeholder_context_for(
        contract,
        checkout=subject_resolution.checkout,
        project=project_dir,
        workspace=ws,
        run_dir=str(run_dir),
    )

    result = run_env_assertions(env_name, env_spec, ctx)
    receipt_path = write_env_assertion_receipt(output_dir=run_dir, result=result)

    return VerifyEnvResult(
        env=env_name,
        run_id=ref.run_id,
        receipt_path=receipt_path,
        all_passed=bool(result.get("all_passed", False)),
        subject={
            **dict(result.get("subject") or {}),
            "checkout": subject_resolution.checkout,
            "source": subject_resolution.source,
        },
        assertions=list(result.get("assertions") or []),
    )


def verify_list(
    *,
    project: str | None = None,
    run_id: str | None = None,
    workspace: str | None = None,
) -> VerifyListResult:
    """List declared commands with their run-text placeholder-resolved.

    Pure projection: resolves the run/project/contract, builds a
    :class:`PlaceholderContext` whose ``{checkout}`` is the resolved physical
    subject and whose ``{project}`` is the canonical project, then reports each
    declared command's ``name`` / ``env`` /
    ``run_resolved`` / ``required`` flag. Executes nothing and writes nothing.
    An undeclared / command-less contract raises :class:`VerifyEnvError`.
    """
    from pipeline.verification_contract import (
        placeholder_context_for,
        resolve_placeholders,
    )

    ref, run_dir, project_dir, contract, ws, meta = _resolve_run_project_contract(
        project=project, run_id=run_id, workspace=workspace,
    )
    if contract is None or not contract.commands:
        raise VerifyEnvError(
            f"no verification commands declared for project {project_dir!r}",
        )

    identity_meta = _verification_identity_meta(
        run_dir=run_dir, meta=meta, project_dir=project_dir,
    )
    subject_resolution = resolve_verification_subject(
        meta=identity_meta,
        project_dir=project_dir,
    )
    ctx = placeholder_context_for(
        contract,
        checkout=subject_resolution.checkout,
        project=project_dir,
        workspace=ws,
        run_dir=str(run_dir),
    )

    commands: list[dict[str, Any]] = []
    for name in sorted(contract.commands):
        spec = contract.commands[name]
        env_name = spec.get("env") or contract.default_env
        run_resolved = resolve_placeholders(str(spec.get("run", "")), ctx)
        commands.append({
            "name": name,
            "env": env_name,
            "run_resolved": run_resolved,
            "required": name in contract.required,
        })

    return VerifyListResult(
        run_id=ref.run_id,
        commands=commands,
        subject_checkout=subject_resolution.checkout,
        subject_source=subject_resolution.source,
    )


def verify_run(
    *,
    project: str | None = None,
    run_id: str | None = None,
    workspace: str | None = None,
    commands: list[str] | None = None,
    required_only: bool = False,
    include_manual: bool = False,
    subject_checkout: str | None = None,
) -> VerifyRunResult:
    """Execute declared commands natively and persist one receipt each.

    No env-override: each command's env is its declared ``env`` (else the
    contract's ``default_env``). Commands run in the run worktree
    (``{checkout}``) while the contract is loaded from the canonical project, and
    git provenance on each receipt is taken from the worktree subject with
    ``baseline_head`` drawn from ``meta['worktree']['base_ref']``.

    ``subject_checkout`` is an internal controller seam. It may confirm an
    isolated recorded identity or establish a non-canonical subject when the
    metadata is incomplete; it never overrides conflicting identity.

    Command selection (resolved — and validated — *before* any execution):

    * ``required_only`` → exactly ``contract.required``; an empty required set
      raises :class:`VerifyEnvError`.
    * explicit ``commands`` → those names; an unknown name raises.
    * otherwise → every declared command except commands explicitly marked
      ``manual_only`` or guarded by an unrequested operator gate set. Pass
      ``include_manual=True`` to restore the full declared-command sweep.

    Returns a :class:`VerifyRunResult`; ``all_passed`` is True only when every
    command exited 0.
    """
    from pipeline.evidence.verification_receipt import (
        COMMAND_RECEIPTS_DIRNAME,
        write_command_receipt,
    )
    from pipeline.verification_command import run_command
    from pipeline.verification_contract import placeholder_context_for

    ref, run_dir, project_dir, contract, ws, meta = _resolve_run_project_contract(
        project=project, run_id=run_id, workspace=workspace,
    )
    if contract is None or not contract.commands:
        raise VerifyEnvError(
            f"no verification commands declared for project {project_dir!r}",
        )

    if required_only:
        names = list(contract.required)
        if not names:
            raise VerifyEnvError(
                "verification.required is empty; nothing to run",
            )
    elif commands:
        names = []
        for name in commands:
            if name not in contract.commands:
                known = sorted(contract.commands)
                raise VerifyEnvError(
                    f"command {name!r} is not declared (known: {known!r})",
                )
            names.append(name)
    else:
        names = _default_verify_run_names(contract, include_manual=include_manual)

    identity_meta = _verification_identity_meta(
        run_dir=run_dir, meta=meta, project_dir=project_dir,
    )
    subject_resolution = resolve_verification_subject(
        meta=identity_meta,
        project_dir=project_dir,
        subject_checkout=subject_checkout,
    )
    baseline_head = _baseline_head_for_meta(dict(identity_meta))
    ctx = placeholder_context_for(
        contract,
        checkout=subject_resolution.checkout,
        project=project_dir,
        workspace=ws,
        run_dir=str(run_dir),
    )
    log_dir = Path(run_dir) / COMMAND_RECEIPTS_DIRNAME

    outcomes: list[CommandOutcome] = []
    for name in names:
        spec = contract.commands[name]
        result = run_command(
            name,
            spec,
            contract,
            ctx,
            required=name in contract.required,
            baseline_head=baseline_head,
            log_dir=log_dir,
        )
        receipt_path = write_command_receipt(output_dir=run_dir, result=result)
        git = result.get("git") or {}
        outcomes.append(CommandOutcome(
            command=name,
            env=str(result.get("env", "")),
            exit_code=result.get("exit_code"),
            passed=result.get("exit_code") == 0,
            parity=str(result.get("parity", "absolute")),
            receipt_path=receipt_path,
            duration_s=result.get("duration_s"),
            stdout_tail=str(result.get("stdout_tail", "")),
            stderr_tail=str(result.get("stderr_tail", "")),
            checkout_head=git.get("checkout_head"),
            baseline_head=git.get("baseline_head"),
            dependencies=_dependency_tags(result.get("dependencies")),
        ))

    all_passed = all(o.exit_code == 0 for o in outcomes)
    return VerifyRunResult(
        run_id=ref.run_id,
        outcomes=outcomes,
        all_passed=all_passed,
        subject_checkout=subject_resolution.checkout,
        subject_source=subject_resolution.source,
    )
