"""Interactive profile selection and enforcement for the `orcho` CLI.

Owns the stdin side of the profile picker: the gating checks, the lazy
catalog load, the input loop, and the operator-facade enforcement policy
(:func:`require_profile_or_exit`). All menu rendering is delegated to
:mod:`cli._profile_menu`, which holds the pure presentation logic. Helpers
here stay conservative — they no-op unless stdin is a TTY and
``--no-interactive`` is unset, so CI / MCP / piped invocations keep their
existing non-blocking behaviour.
"""
from __future__ import annotations

import argparse
import enum
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from cli._profile_menu import (
    AUTO_DETECT_CHOICE,
    _print_profile_details,
    _render_menu,
    render_autodetect_result,
)
from core.io.ansi import C, get_color_enabled, is_color_active
from core.io.journey_prompt import bold, paint
from core.io.terminal_input import drain_paste_burst

if TYPE_CHECKING:
    from pipeline.runtime.work_kind_detection import AutoDetectResolution

# A cross recommendation is only surfaced as the three-choice block when the
# semantic-profile detection cleared this confidence floor (matches the shipped
# ``auto_detect.confidence_threshold`` default). Below it, the recommendation is
# still recorded in meta but the operator is not prompted to widen delivery.
_CROSS_CONFIDENCE_FLOOR = 0.7


class CrossRunRequested(Exception):
    """Raised when the operator picks 'Start cross run' in the auto-detect block.

    This is an explicit *terminal directive* for the current mono invocation,
    not a mono-run continuation: ``orcho run`` must NOT fall through into
    ``run_pipeline`` and must NOT persist a cross ``delivery_scope`` on a mono
    run that never starts. The CLI catches it and launches a *fresh* cross
    process from the projected projects (see :mod:`cli._cross_launch`), which is
    what keeps the mono run from ever starting (F2). Carries the projected
    delivery-project aliases for the launcher / tests.
    """

    def __init__(self, *, projects: tuple[str, ...]) -> None:
        self.projects = projects
        super().__init__(", ".join(projects))


class ProfilePromptResult(enum.Enum):
    """Outcome of :func:`prompt_for_profile_if_needed`.

    * ``SELECTED`` — the operator picked a profile (by number or exact
      name) and ``args.profile`` was set.
    * ``ABORTED`` — the menu was shown on a TTY but the operator declined
      (empty line, Ctrl-C / Ctrl-D, or exhausted the retry budget).
    * ``SKIPPED`` — the menu was never shown (``--profile`` already set,
      ``--resume`` / ``--from-run-plan``, ``--no-interactive``, non-TTY
      stdin, empty catalog after filtering, or a catalog load failure).
    """

    SELECTED = "selected"
    ABORTED = "aborted"
    SKIPPED = "skipped"


def prompt_for_profile_if_needed(
    args: argparse.Namespace,
    *,
    profile_filter: Callable[[object], bool] | None = None,
    include_auto_detect: bool = False,
) -> ProfilePromptResult:
    """Offer an interactive profile picker when none was supplied.

    Mutates ``args.profile`` in place when the operator picks a profile by
    number or exact name, and reports the outcome via
    :class:`ProfilePromptResult`.

    Returns ``SKIPPED`` (no menu, no ``input()``) when:

    * ``--profile`` is already set;
    * ``--resume`` / ``--from-run-plan`` is set (profile inherits from the
      persisted ``meta.json``, so a menu would clobber that inheritance);
    * ``--no-interactive`` is set;
    * stdin is not a TTY (CI, pipes, MCP transports);
    * the profile catalog is empty (or empty after ``profile_filter``) or
      fails to load.

    Returns ``ABORTED`` when the menu was shown on a TTY but the operator
    declined (empty line, Ctrl-C / Ctrl-D, or exhausted the retry budget on
    invalid input); ``args.profile`` stays ``None`` in that case.

    Returns ``SELECTED`` when the operator picked a profile.

    For this operator facade a missing profile is no longer silently mapped
    to ``feature``: the caller (see :func:`require_profile_or_exit`) treats
    ``SKIPPED`` / ``ABORTED`` as terminal. The canonical ``feature``
    fresh-run default survives only for the programmatic SDK path and direct
    ``orcho-run`` / ``orcho-cross`` invocations.

    ``profile_filter`` keeps only profiles for which the predicate is true
    (used by ``orcho cross`` to hide cross-incompatible profiles). ``None``
    means no filtering. Filtered-out profile names never reach the rendered
    menu (lines, sub-headers, or the ``?N`` detail view).

    ``include_auto_detect`` (set by the ``orcho run`` picker) renders a
    synthetic first-class ``auto-detect`` row above the manual profiles;
    picking it sets ``args.profile`` to the ``auto-detect`` selector token,
    which the downstream dispatch resolves to a concrete profile. Manual
    profiles stay fully visible. ``orcho cross`` leaves it unset.

    The menu renders one work kind per line — number, name, an optional
    ``[default]`` chip on ``feature``, and a ``{default_mode} · {isolation} ·
    {tagline}`` subtitle derived from the profile metadata and the first
    sentence of the description — framed by a journey-style divider.
    Entering ``?N`` (or ``? <name>``, or a bare ``?`` for all) prints the
    full wrapped description and re-shows the prompt *without* spending one of
    the three invalid-input attempts.

    Styling routes through :mod:`core.io.journey_prompt` helpers so the prompt
    obeys the shared color policy.
    """
    if getattr(args, "profile", None):
        return ProfilePromptResult.SKIPPED
    if getattr(args, "resume", None) or getattr(args, "from_run_plan", None):
        return ProfilePromptResult.SKIPPED
    if getattr(args, "no_interactive", False):
        return ProfilePromptResult.SKIPPED
    if not sys.stdin.isatty():
        return ProfilePromptResult.SKIPPED

    try:
        from core.infra.paths import CONFIG_DIR
        from pipeline.profiles.loader import load_profiles_v2_with_plugins

        profiles = load_profiles_v2_with_plugins(
            CONFIG_DIR / "pipeline_profiles_v2.json"
        )
    except Exception:
        return ProfilePromptResult.SKIPPED

    names = sorted(profiles)
    # ADR 0085: internal/system profiles (e.g. ``correction``) are never
    # offered in the fresh-run picker — they require run context an
    # interactive first run cannot supply. Excluded unconditionally, BEFORE
    # the caller's ``profile_filter``, so they never reach the menu, the
    # sub-headers, or the ``?N`` detail view. Explicit ``--profile`` and
    # resume / from-run-plan inheritance still reach them.
    names = [
        name for name in names
        if not getattr(profiles[name], "internal", False)
    ]
    if profile_filter is not None:
        names = [name for name in names if profile_filter(profiles[name])]
    if not names:
        return ProfilePromptResult.SKIPPED

    color = get_color_enabled()
    color = color if color is not None else is_color_active()

    ordered_names = _render_menu(
        names, profiles, color=color, include_auto_detect=include_auto_detect,
    )

    count = len(ordered_names)
    empty_hint = "empty for auto-detect" if include_auto_detect else "empty to abort"
    prompt = bold(
        f"Select profile [1-{count}] — number or name "
        f"(?N for details, {empty_hint}): ",
        color=color,
    )
    invalid = 0
    while invalid < 3:
        try:
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()  # newline after ^C / ^D for clean shell prompt
            return ProfilePromptResult.ABORTED
        choice = drain_paste_burst(raw, stdin=sys.stdin).strip()
        if not choice:
            if include_auto_detect:
                # Default choice: a bare Enter runs auto-detect (it leads the
                # menu and carries the [default] chip). The detector then
                # recommends a work kind / operating mode.
                args.profile = AUTO_DETECT_CHOICE
                return ProfilePromptResult.SELECTED
            print()  # newline for clean shell prompt
            return ProfilePromptResult.ABORTED
        if choice.startswith("?"):
            _print_profile_details(
                choice[1:].strip(), ordered_names, profiles, color=color
            )
            continue  # detail queries never spend an invalid attempt
        if choice.isdigit():
            picked = int(choice)
            if 1 <= picked <= count:
                args.profile = ordered_names[picked - 1]
                return ProfilePromptResult.SELECTED
        elif choice in ordered_names:
            args.profile = choice
            return ProfilePromptResult.SELECTED
        invalid += 1
    return ProfilePromptResult.ABORTED


def require_profile_or_exit(
    args: argparse.Namespace,
    *,
    profile_filter: Callable[[object], bool] | None = None,
    include_auto_detect: bool = False,
) -> int | None:
    """Enforce profile selection for the operator CLI facade.

    Runs :func:`prompt_for_profile_if_needed`, then for a *fresh* run (no
    ``--resume`` and no ``--from-run-plan``) with no profile chosen, decides
    how the facade should terminate:

    * operator declined the interactive menu (``ABORTED``) → print a brief
      cancellation note and return exit code ``0`` (clean exit);
    * the menu could not be shown (``SKIPPED`` in a non-interactive / non-TTY
      context):

      - with ``include_auto_detect`` (the ``orcho run`` facade) → default to
        the ``auto-detect`` selector and proceed (``None``). A headless run
        then infers a work kind + mode from the project rather than dead-ending;
        the downstream resolver errors clearly if detection fails.
      - otherwise (e.g. the cross facade) → print an actionable error naming
        ``--profile`` / ``orcho profiles list`` on stderr and return ``2``.

    Returns ``None`` when the caller should proceed: a profile is set, or the
    run is a ``--resume`` / ``--from-run-plan`` (profile inherits from
    ``meta.json``).

    An empty / whitespace-only ``--profile`` is normalized to "not set" so it
    cannot bypass enforcement and silently reach the downstream ``feature``
    fresh-run default.

    ``include_auto_detect`` is forwarded to
    :func:`prompt_for_profile_if_needed` so the ``orcho run`` picker offers the
    first-class ``auto-detect`` selector; an explicit ``--profile auto-detect``
    is treated as a set profile (returns ``None``) and never re-prompts.

    Styling routes through :func:`core.io.journey_prompt.paint`.
    """
    raw_profile = getattr(args, "profile", None)
    if raw_profile is not None and not str(raw_profile).strip():
        args.profile = None

    result = prompt_for_profile_if_needed(
        args,
        profile_filter=profile_filter,
        include_auto_detect=include_auto_detect,
    )

    if getattr(args, "resume", None) or getattr(args, "from_run_plan", None):
        return None
    if getattr(args, "profile", None) is not None:
        return None

    if result is ProfilePromptResult.ABORTED:
        print(paint("Aborted: no profile selected.", C.GREY))
        return 0

    # SKIPPED: no interactive terminal to show the picker (headless / CI / MCP).
    if include_auto_detect:
        # The `orcho run` facade: a missing --profile is not a dead end. Default
        # to the auto-detect selector so a headless run infers a work kind + mode
        # from the project instead of exiting 2. The token is resolved downstream
        # (pipeline.project.cli / auto_detect), which errors clearly if detection
        # fails — no silent `feature` default, so profile enforcement holds.
        args.profile = AUTO_DETECT_CHOICE
        print(
            paint(
                f"No --profile given; using '{AUTO_DETECT_CHOICE}' "
                "(work kind & mode inferred from the project).",
                C.GREY,
            )
        )
        return None

    # Other facades (e.g. cross) keep explicit selection — but say why and how.
    print(
        paint(
            "profile: no --profile given and no interactive terminal to pick "
            "one.\n  Pass --profile <name> (see `orcho profiles list`).",
            C.RED,
        ),
        file=sys.stderr,
    )
    return 2


def _prompt_topology_choice_number(*, color: bool) -> int:
    """Read a ``1``/``2``/``3`` topology choice from stdin.

    Empty input, Ctrl-C / Ctrl-D, or an exhausted retry budget all default to
    ``3`` (continue strict mono) — the conservative non-widening choice.
    """
    prompt = bold("Choice [1-3] (empty = 3, strict mono): ", color=color)
    invalid = 0
    while invalid < 3:
        try:
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return 3
        choice = drain_paste_burst(raw, stdin=sys.stdin).strip()
        if not choice:
            return 3
        if choice in {"1", "2", "3"}:
            return int(choice)
        invalid += 1
    return 3


def resolve_topology_choice(
    resolution: AutoDetectResolution,
    *,
    interactive: bool,
    color: bool | None = None,
    choice_fn: Callable[[], int] | None = None,
) -> AutoDetectResolution:
    """Apply the operator's topology choice to a cross-recommended resolution.

    Returns ``resolution`` unchanged unless the detector recommended a
    ``cross_recommended`` topology with non-empty projected projects and a
    semantic-profile confidence at or above :data:`_CROSS_CONFIDENCE_FLOOR`.
    For such a resolution:

    * **non-interactive** — the recommendation is only *recorded*: no block is
      printed, no cross run starts, and delivery is not widened. The returned
      ``delivery_scope`` stays ``strict_mono``;
    * **interactive** — print the ``Auto-detect result`` block and the three
      choices, read the operator's pick (1/2/3), and map it to a delivery scope
      via :func:`pipeline.project.auto_detect.apply_topology_choice`.
      Choice ``1`` (start cross) never converts the current mono process into a
      cross run and never persists a cross ``delivery_scope`` on the mono run:
      it prints a ready ``orcho cross`` command and raises
      :class:`CrossRunRequested` so the caller stops *before* ``run_pipeline``.

    This runs *after* the semantic profile is resolved, so the existing
    profile-confirm semantics are untouched; the topology axis never changes
    ``actual_profile`` / ``actual_mode``. ``choice_fn`` is injectable for tests.

    Raises:
        CrossRunRequested: the operator chose ``1`` (start cross). The caller
            must not continue the mono run; the ``orcho cross`` command has
            already been printed.
    """
    # Lazy import keeps this module free of the runtime vocabulary at import
    # time and avoids any import-order coupling with the dispatch module.
    from pipeline.project.auto_detect import (
        TopologyChoice,
        apply_topology_choice,
    )
    from pipeline.runtime.run_shape import RunTopology

    if resolution.recommended_topology is not RunTopology.CROSS_RECOMMENDED:
        return resolution
    if not resolution.delivery_projects:
        return resolution
    confidence = resolution.confidence
    if confidence is None or confidence < _CROSS_CONFIDENCE_FLOOR:
        return resolution

    # Non-interactive: never prompt, never start cross, never widen delivery.
    # The cross topology is still recorded in meta via the resolution echo.
    if not interactive:
        return resolution

    if color is None:
        resolved = get_color_enabled()
        color = resolved if resolved is not None else is_color_active()

    render_autodetect_result(resolution, color=color)

    number = (
        choice_fn() if choice_fn is not None
        else _prompt_topology_choice_number(color=color)
    )
    choice = TopologyChoice.from_number(number)

    if choice is TopologyChoice.START_CROSS:
        # Terminal directive for THIS mono process: it must not fall through
        # into ``run_pipeline`` and must never persist a cross ``delivery_scope``
        # on a mono run that never starts (F2). Raise so the caller stops here
        # and launches a *fresh* cross run from the projected projects. Path
        # resolution + launch live at the caller (``cli._cross_launch``), which
        # has the task text and the current project path.
        raise CrossRunRequested(projects=resolution.delivery_projects)

    return apply_topology_choice(resolution, choice)
