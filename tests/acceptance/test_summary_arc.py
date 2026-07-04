"""tests/acceptance/test_summary_arc.py — the ``--output summary`` arc.

Two things are pinned here:

1. **Summary grammar (goldens).** A ``--mock``/``feature`` run in ``summary``
   mode prints the compact append-only arc — ``▶``/``✓`` pairs per phase,
   ``▶id``/``✓id`` pairs per subtask, one-line gate/verdict rows, and the
   ``[DONE]`` rollup — and does NOT print the three-line ``────``/``[PHASE]``
   banners, the multi-line ``ORCHO subtask`` headers, or raw agent JSON echo.
   The reject scenario (``--mock-validate-plan-reject 1``) shows the
   ``✗ validate_plan · REJECTED`` → ``✓ validate_plan · APPROVED`` arc.

2. **Durable-sink mode-independence.** ``--output`` is a *presentation* knob;
   it must not change any durable sink. This is proven NOT by comparing two
   arbitrary runs, but as an invariant: the SAME mock scenario is run three
   times (summary / live / debug) with a **pinned** ``$ORCHO_RUN_ID`` and
   **byte-identical paths** (same project, workspace, run dir — reset between
   runs), and pinned git commit dates so commit SHAs are reproducible. After
   normalising ONLY an explicit allowlist of run-specific fields
   (durations/elapsed, wall-clock timestamps, duration-derived artifact
   byte-sizes, and the tmp-root path prefix), ``events.jsonl`` /
   ``progress.log`` / ``output.log`` diff empty pairwise summary↔live AND
   summary↔debug. Any mode-dependent difference in a field NOT on the
   allowlist fails the test — it is a presentation regression leaking into a
   durable sink, not a reason to widen the allowlist.
"""
from __future__ import annotations

import contextlib
import difflib
import io
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.io.ansi import strip_ansi

# A fixed run id (borrowed shape from tests/integration/control_loop/_harness.py)
# so ``session_ts`` == the run-dir name and every mode run shares one id/path.
FIXED_RUN_ID = "20260502_000000"

# Pinned git dates → reproducible commit SHAs, so a git SHA embedded in a
# durable sink (e.g. the diff apply-check ``baseline_ref``) is identical across
# runs and needs no scrubbing.
_FIXED_GIT_ENV = {
    "GIT_AUTHOR_DATE": "2026-05-02T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-05-02T00:00:00 +0000",
}

_DURABLE_FILES = ("events.jsonl", "progress.log", "output.log")


# ── normalizer (explicit run-specific allowlist) ──────────────────────────
# Only these are scrubbed. Everything else must match across modes or the test
# fails — an unlisted mode-dependent field is a durable-sink regression.
#
# ``events.jsonl`` JSON keys whose value is run-specific — the contract-T6
# allowlist (durations/elapsed + wall-clock timestamps); the tmp-root path
# prefix is scrubbed separately in ``_scrub_events``:
#   * timestamps: ``ts`` / ``created_at``
#   * durations:  ``duration`` / ``duration_s`` / ``elapsed`` / ``took_ms``
#
# ``size_bytes`` (the byte count on an ``artifact.created`` event) is NOT on
# this global allowlist: a raw byte count is not a timestamp/duration/path.
# It is scrubbed only *narrowly* in ``_scrub_events`` — solely for the two
# proof-file artifacts (``evidence.json`` / ``evidence.md``) whose byte-size
# is strictly derivative of the duration/timestamp allowlist (their bodies
# embed formatted durations and wall-clock timestamps whose rendered length
# varies run-to-run). That derivativeness is PROVEN by
# ``test_artifact_bodies_are_mode_independent`` below. The size_bytes of every
# OTHER artifact (``plan`` / ``parsed_plan`` / ``diff``) stays intact, so a
# real mode-dependent size leak there still fails the parity — the allowlist
# is never silently widened to all events.
_EVENT_SCRUB_KEYS = frozenset({
    "ts", "created_at", "duration", "duration_s", "elapsed", "took_ms",
})

# The ``artifact.created`` files whose byte-size the events parity scrubs. Their
# bodies are captured per mode and proven mode-independent (see the proof test)
# so the ``size_bytes`` scrub above is backed by a real content check, not taken
# on faith. JSON artifacts are scrubbed by key; text artifacts by regex.
_PROOF_FILES = ("evidence.json", "evidence.md")

# Wall-clock timestamp / duration keys embedded in the artifact BODIES. A
# superset of ``_EVENT_SCRUB_KEYS`` minus ``size_bytes`` (a size never appears
# inside these bodies): evidence carries per-item ``started_at`` / ``ended_at``
# and a ``retention_until`` deadline (now + TTL) on top of the event keys. All
# are wall-clock timestamps on the T6 allowlist.
_ARTIFACT_SCRUB_KEYS = frozenset({
    "ts", "created_at", "started_at", "ended_at", "retention_until",
    "duration", "duration_s", "total_duration_s", "elapsed", "took_ms",
})

_TS_BRACKET_RE = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{2}:\d{2}")
_DURATION_RE = re.compile(r"duration=\d+\.\d+s")
_TIME_RE = re.compile(r"time=\d+\.?\d*s")

# Broad ISO timestamp (optional fractional seconds, optional tz offset) for the
# artifact-body proof, where wall-clock timestamps appear both as JSON string
# values and inside rendered markdown.
_ISO_TS_ANY_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+\-]\d{2}:\d{2})?"
)
_DUR_SECONDS_RE = re.compile(r"\d+\.\d+s")


def _scrub_events(text: str, tmp_root: str) -> str:
    """Canonicalise ``events.jsonl``: scrub allowlisted keys + tmp-root path.

    Parses each line as JSON, replaces the value of any allowlisted key with a
    constant, rewrites the tmp-root prefix inside string values, and re-emits
    with sorted keys so key ordering can never be a spurious diff.
    """
    def _rec(node: object) -> object:
        if isinstance(node, dict):
            # ``size_bytes`` is scrubbed NARROWLY: only on the two proof-file
            # ``artifact.created`` payloads whose byte-size is proven strictly
            # derivative of the timestamp/duration allowlist (see the proof
            # test). Every other artifact keeps its real size, so a genuine
            # mode-dependent size difference still fails the parity.
            proof_size = (
                "size_bytes" in node
                and Path(str(node.get("path") or "")).name in _PROOF_FILES
            )
            out: dict[str, object] = {}
            for k, v in node.items():
                if k in _EVENT_SCRUB_KEYS or (k == "size_bytes" and proof_size):
                    out[k] = "<SCRUB>"
                else:
                    out[k] = _rec(v)
            return out
        if isinstance(node, list):
            return [_rec(v) for v in node]
        if isinstance(node, str):
            return node.replace(tmp_root, "<TMP>")
        return node

    out: list[str] = []
    for line in text.splitlines():
        if line.strip():
            out.append(json.dumps(_rec(json.loads(line)), sort_keys=True))
    return "\n".join(out)


def _scrub_text(text: str, tmp_root: str) -> str:
    """Canonicalise a text sink: tmp-root path + timestamps + durations."""
    text = text.replace(tmp_root, "<TMP>")
    text = _TS_BRACKET_RE.sub("[<TS>]", text)
    text = _ISO_TS_RE.sub("<ISO_TS>", text)
    text = _DURATION_RE.sub("duration=<DUR>", text)
    text = _TIME_RE.sub("time=<DUR>", text)
    return text


def _normalize(name: str, text: str, tmp_root: str) -> str:
    if name == "events.jsonl":
        return _scrub_events(text, tmp_root)
    return _scrub_text(text, tmp_root)


def _scrub_artifact(name: str, text: str, tmp_root: str) -> str:
    """Canonicalise an ``artifact.created`` body for the size-derivative proof.

    Scrubs ONLY the wall-clock/duration allowlist — no size, no other field.
    JSON artifacts (``evidence.json``) are parsed and scrubbed by key so a
    numeric duration/timestamp value collapses regardless of its digit count;
    text artifacts (``evidence.md``) are scrubbed by regex over ISO timestamps
    and ``<n>.<n>s`` durations. If a mode-dependent CONTENT change existed it
    would survive here and fail the parity — which is exactly why the
    ``size_bytes`` scrub in the events normalizer is safe.
    """
    if name.endswith(".json"):
        def _rec(node: object) -> object:
            if isinstance(node, dict):
                return {
                    k: ("<SCRUB>" if k in _ARTIFACT_SCRUB_KEYS else _rec(v))
                    for k, v in node.items()
                }
            if isinstance(node, list):
                return [_rec(v) for v in node]
            if isinstance(node, str):
                return node.replace(tmp_root, "<TMP>")
            return node

        return json.dumps(_rec(json.loads(text)), sort_keys=True)
    text = text.replace(tmp_root, "<TMP>")
    text = _ISO_TS_ANY_RE.sub("<TS>", text)
    text = _DUR_SECONDS_RE.sub("<DUR>", text)
    return text


# ── run harness ───────────────────────────────────────────────────────────


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@orcho.invalid"],
        ["git", "config", "user.name", "Orcho Test"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(cmd, cwd=path, check=True)
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _reset_run_globals() -> None:
    """Reset the module-level logging / event / transcript singletons.

    Mirrors ``test_full_mock_flow``'s teardown plus the transcript
    phase-header continuity (so a header synthesised in one run cannot bleed
    into the next).
    """
    import agents.stream as _stream
    import core.io.transcript as _transcript
    import core.observability.events as _events
    import core.observability.logging as _logging

    _logging._progress_log = None
    _stream._agent_log = None
    _events.clear_phase_context()
    _events.init_event_store(None)
    _transcript.reset_phase_header_continuity()


def _run_mode(
    *, mode: str, project: Path, ws: Path, run_dir: Path, reject_rounds: int,
) -> tuple[str, dict[str, str]]:
    """Run the mock feature scenario once in ``mode`` at fixed paths.

    Resets the project (delivery commits onto it), the workspace (worktrees),
    and the run dir to a clean state at the SAME paths, pins ``$ORCHO_RUN_ID``
    and the git dates, applies the output mode, runs in-process, and returns
    ``(stdout, {durable_file: text})``.
    """
    import pipeline.engine.delivery_branch as _delivery_branch
    from agents.runtimes import make_provider
    from core.observability.logging import apply_output_mode
    from pipeline.plugins import PluginConfig
    from pipeline.project_orchestrator import run_pipeline

    buf = io.StringIO()
    # The pinned git dates must wrap BOTH the fresh ``git init`` commit (whose
    # SHA becomes the delivery ``baseline_ref``) and the run's delivery commit,
    # so every commit SHA embedded in a durable sink is reproducible.
    with patch.dict(os.environ, _FIXED_GIT_ENV):
        # Fresh, byte-identical paths for every mode run.
        shutil.rmtree(project, ignore_errors=True)
        _init_git_repo(project)
        shutil.rmtree(ws, ignore_errors=True)
        run_dir.mkdir(parents=True)

        _reset_run_globals()
        apply_output_mode(mode)

        provider = make_provider(
            True, latency=0.0, mock_validate_plan_reject_rounds=reject_rounds,
        )
        with (
            contextlib.redirect_stdout(buf),
            patch("pipeline.project.session_run.load_plugin",
                  return_value=PluginConfig(name="Summary Arc", language="Python")),
            patch("core.io.git_helpers.has_uncommitted", return_value=True),
            patch("core.io.git_helpers.git_diff_stat", return_value="1 file changed"),
            # ADR 0119 legacy opt-out: commit onto the checkout (this scenario
            # predates worktree-branch delivery, same as test_full_mock_flow).
            patch.object(_delivery_branch, "normalize_branch_policy",
                         lambda _raw: "bypass"),
            patch.dict(os.environ, {"ORCHO_RUN_ID": FIXED_RUN_ID}),
        ):
            run_pipeline(
                task="demo summary arc",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=2,
                profile_name="feature",
                provider=provider,
            )

    files = {
        name: (run_dir / name).read_text(encoding="utf-8")
        if (run_dir / name).exists() else ""
        for name in (*_DURABLE_FILES, *_PROOF_FILES)
    }
    return buf.getvalue(), files


@pytest.fixture(autouse=True)
def _restore_output_mode():
    """Isolate the process-level output mode / echo across tests."""
    import agents as _agents
    from core.observability.logging import apply_output_mode, get_output_mode

    before = get_output_mode()
    try:
        yield
    finally:
        apply_output_mode(before)
        _reset_run_globals()
        _agents.set_stdout_echo(False)


@pytest.fixture(scope="module")
def arc_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Run the happy scenario in all three modes + the reject scenario once.

    All runs share ONE tmp root and reset to identical paths between runs, so
    durable sinks are path-identical across modes. Returns the stdout of each
    happy mode run, the reject-summary stdout, the per-mode durable files, and
    the tmp-root string (for the normalizer).
    """
    root = tmp_path_factory.mktemp("summary_arc")
    project = root / "proj"
    ws = root / "ws"
    run_dir = ws / "runs" / FIXED_RUN_ID

    happy_stdout: dict[str, str] = {}
    happy_files: dict[str, dict[str, str]] = {}
    for mode in ("summary", "live", "debug"):
        stdout, files = _run_mode(
            mode=mode, project=project, ws=ws, run_dir=run_dir, reject_rounds=0,
        )
        happy_stdout[mode] = stdout
        happy_files[mode] = files

    reject_stdout, _ = _run_mode(
        mode="summary", project=project, ws=ws, run_dir=run_dir, reject_rounds=1,
    )

    return {
        "tmp_root": str(root),
        "happy_stdout": happy_stdout,
        "happy_files": happy_files,
        "reject_stdout": reject_stdout,
    }


# ── scenario 1: happy summary golden ──────────────────────────────────────


def test_happy_summary_positive_grammar(arc_runs) -> None:
    out = strip_ansi(arc_runs["happy_stdout"]["summary"])

    # ▶/✓ pairs per phase (compact starts + verdict/rollup closes).
    assert re.search(r"(?m)^▶ plan$", out)
    assert re.search(r"(?m)^▶ implement$", out)
    assert "✓ validate_plan · APPROVED" in out
    assert "✓ review_changes · APPROVED" in out
    # Plan contract closes the ▶ plan start as one counters line.
    assert re.search(
        r"(?m)^✓ plan · contract: \d+ tasks.*· acceptance \d+ · risks \d+$", out
    )
    # ▶id/✓id pairs per subtask + the implement rollup line.
    for sid in ("inspect-target", "apply-fix", "verify"):
        assert re.search(rf"(?m)^  ▶ {sid} · ", out), f"missing ▶ {sid}"
        assert re.search(
            rf"(?m)^  ✓ {sid} · done · \d+/\d+ criteria", out
        ), f"missing ✓ {sid}"
    assert re.search(r"(?m)^✓ implement · \d+/\d+ subtasks · \d+ files changed$", out)
    # [DONE] rollup stays (its phase-chip line + the compact ▶ done header).
    assert re.search(r"(?m)^▶ done$", out)
    assert "plan=ok | validate_plan=ok | implement=ok" in out


def test_happy_summary_negative_no_verbose_scaffolding(arc_runs) -> None:
    out = strip_ansi(arc_runs["happy_stdout"]["summary"])

    # No three-line ────/[PHASE]/──── banners.
    assert "────────" not in out, "summary must not print the rule-line banner"
    for header in (
        "[PLAN] PLAN", "[VALIDATE_PLAN]", "[IMPLEMENT] IMPLEMENT",
        "[REVIEW_CHANGES]", "[FINAL_ACCEPTANCE]",
    ):
        assert header not in out, f"summary leaked the {header!r} banner header"
    # No multi-line ORCHO subtask START/DONE/ATTESTATION headers.
    assert "ORCHO subtask" not in out
    # No raw agent JSON echo (a verdict/findings envelope would carry these).
    assert '"verdict":' not in out
    assert '"short_summary":' not in out


# ── scenario 2: reject arc ────────────────────────────────────────────────


def test_reject_summary_verdict_sequence(arc_runs) -> None:
    out = strip_ansi(arc_runs["reject_stdout"])
    lines = out.splitlines()

    reject_idx = next(
        (i for i, ln in enumerate(lines) if "✗ validate_plan · REJECTED" in ln),
        None,
    )
    approve_idx = next(
        (i for i, ln in enumerate(lines) if "✓ validate_plan · APPROVED" in ln),
        None,
    )
    assert reject_idx is not None, "reject arc missing ✗ validate_plan · REJECTED"
    assert approve_idx is not None, "reject arc missing ✓ validate_plan · APPROVED"
    assert reject_idx < approve_idx, "REJECTED must precede the replan APPROVED"
    # The rejection headline carries the first finding id (F1 …).
    assert re.search(r"✗ validate_plan · REJECTED · F1", lines[reject_idx])
    # The replan APPROVED is a round-2 verdict, so it MUST carry the ``R2``
    # round token — the caller has to thread the active round into the
    # summary renderer (a plain ``✓ validate_plan · APPROVED`` here means the
    # round was dropped, regressing the repeated-verdict grammar).
    assert re.search(r"✓ validate_plan · APPROVED · R2\b", lines[approve_idx]), (
        f"round-2 approve missing R2 token: {lines[approve_idx]!r}"
    )


# ── invariant: durable sinks are mode-independent ─────────────────────────


@pytest.mark.parametrize("other_mode", ["live", "debug"])
@pytest.mark.parametrize("durable", _DURABLE_FILES)
def test_durable_sinks_mode_independent(arc_runs, durable: str, other_mode: str) -> None:
    """summary↔live AND summary↔debug: normalized durable sink diffs empty.

    Only the explicit run-specific allowlist is scrubbed; any surviving
    difference is a mode-dependent leak into a durable sink and fails here
    (the fix is to stop gating the sink on mode, never to widen the allowlist).
    """
    tmp_root = arc_runs["tmp_root"]
    files = arc_runs["happy_files"]

    summary_norm = _normalize(durable, files["summary"][durable], tmp_root)
    other_norm = _normalize(durable, files[other_mode][durable], tmp_root)

    if summary_norm != other_norm:
        diff = "\n".join(
            difflib.unified_diff(
                summary_norm.splitlines(),
                other_norm.splitlines(),
                fromfile=f"summary/{durable}",
                tofile=f"{other_mode}/{durable}",
                lineterm="",
            )
        )
        pytest.fail(
            f"{durable} differs between summary and {other_mode} after "
            f"normalizing the run-specific allowlist — a presentation "
            f"regression leaked into a durable sink:\n{diff[:4000]}"
        )


def test_durable_sinks_are_nonempty(arc_runs) -> None:
    """Guard the invariant: the sinks must exist and carry content, else the
    empty-diff above would pass vacuously."""
    files = arc_runs["happy_files"]
    for mode in ("summary", "live", "debug"):
        for durable in _DURABLE_FILES:
            assert files[mode][durable].strip(), f"{mode}/{durable} is empty"


# ── contract: ``size_bytes`` is derivative of the timestamp/duration allowlist ─


@pytest.mark.parametrize("other_mode", ["live", "debug"])
@pytest.mark.parametrize("artifact", _PROOF_FILES)
def test_artifact_bodies_are_mode_independent(
    arc_runs, artifact: str, other_mode: str,
) -> None:
    """Prove the events ``size_bytes`` scrub hides nothing mode-dependent.

    The events parity normalizer scrubs ``size_bytes`` (an ``artifact.created``
    byte count) even though a raw size is not literally on the T6 allowlist.
    This test earns that scrub: it re-reads the FULL artifact body per mode and
    diffs after scrubbing ONLY the wall-clock timestamp / duration allowlist —
    no size, no other field. An empty diff proves the byte-size difference is
    strictly derivative of already-allowed duration/timestamp fields (the body
    embeds formatted durations, ``started_at``/``ended_at`` and a
    ``retention_until`` deadline whose rendered length varies run-to-run). Any
    genuine mode-dependent content change would survive this scrub and fail
    here — so ``size_bytes`` cannot mask a presentation leak.
    """
    tmp_root = arc_runs["tmp_root"]
    files = arc_runs["happy_files"]

    assert files["summary"][artifact].strip(), f"summary/{artifact} is empty"
    summary_norm = _scrub_artifact(artifact, files["summary"][artifact], tmp_root)
    other_norm = _scrub_artifact(artifact, files[other_mode][artifact], tmp_root)

    if summary_norm != other_norm:
        diff = "\n".join(
            difflib.unified_diff(
                summary_norm.splitlines(),
                other_norm.splitlines(),
                fromfile=f"summary/{artifact}",
                tofile=f"{other_mode}/{artifact}",
                lineterm="",
            )
        )
        pytest.fail(
            f"{artifact} body differs between summary and {other_mode} after "
            f"scrubbing only the timestamp/duration allowlist — the size_bytes "
            f"scrub in the events parity would be masking a real mode-dependent "
            f"content change:\n{diff[:4000]}"
        )
