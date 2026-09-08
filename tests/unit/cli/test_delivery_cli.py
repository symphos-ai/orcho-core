"""``orcho delivery gate`` / ``orcho delivery decide`` — thin facades over the SDK.

Builds a real parked delivery gate (git worktree + ``meta.commit_delivery``)
the way ``tests/unit/sdk/test_delivery_decision.py`` does, then drives the
CLI facades and asserts exit codes, printed fields, and that a refused
decision leaves the checkout untouched. The helpers are deliberately copied
here (not shared through conftest) to keep the fixture blast radius small.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cli.orcho import build_parser, cmd_delivery_decide, cmd_delivery_gate
from core.io.ansi import strip_ansi
from pipeline.engine.commit_delivery import resolve_commit_delivery


@pytest.fixture(autouse=True)
def _adr0119_legacy_bypass_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin delivery to the ADR 0119 ``bypass`` opt-out for this slice.

    ADR 0119 shipped ``branch_policy=worktree_branch`` as the delivery default,
    which publishes an isolated run's own branch instead of committing onto the
    target checkout; ``decide_delivery`` replays under the live config. These
    tests assert the "commit onto the checkout" behavior, so they run under
    ``bypass`` (the ADR's explicit legacy opt-out).
    """
    import pipeline.engine.delivery_branch as _db

    monkeypatch.setattr(_db, "normalize_branch_policy", lambda _raw: "bypass")


@pytest.fixture(autouse=True)
def _runspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "runs").mkdir(exist_ok=True)
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@orcho.invalid")
    _git(repo, "config", "user.name", "Orcho Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def _worktree(repo: Path, run_dir: Path) -> Path:
    from core.io.git_helpers import create_worktree

    result = create_worktree(
        repo=repo,
        base_ref=_git(repo, "rev-parse", "HEAD"),
        target_path=run_dir / "checkout",
        branch_name="orcho/run/r1",
    )
    assert result.ok, result.error
    return run_dir / "checkout"


def _park(
    tmp_path: Path, *, verdict: str = "APPROVED", run_id: str = "r1",
) -> tuple[Path, Path, Path]:
    """Build ``tmp_path/runs`` with a producer-parked delivery gate.

    Returns ``(runs_dir, repo, worktree)``. The meta is the deferred-delivery
    producer's own park (ADR 0175 addendum): ``halted`` /
    ``commit_delivery_pending`` with a ``pending`` gate whose action is still
    ``none``, which core treats as decidable in place.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    wt = _worktree(repo, run_dir)
    (wt / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    release_entry: dict = {"verdict": verdict, "short_summary": "feat: x"}
    if verdict == "REJECTED":
        release_entry["release_blockers"] = [
            {
                "id": "RB1",
                "severity": "P1",
                "title": "Data loss on apply",
                "body": "The delivery path drops user rows.",
                "required_fix": "Preserve existing rows during delivery.",
                "why_blocks_release": "Shipping would destroy user data.",
            },
        ]
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id=run_id,
        session={"status": "done", "phases": {"final_acceptance": release_entry}},
        commit_config={"enabled": True, "auto_in_ci": "approve", "add_untracked": True},
        no_interactive=True,
        decision_mode="defer",
    )
    ctx = decision.to_dict()
    ctx["decided_at"] = "2026-07-29T09:31:22+00:00"
    meta = {
        "status": "halted",
        "halt_reason": "commit_delivery_pending",
        "commit_delivery": ctx,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return runs_dir, repo, wt


def _meta(runs_dir: Path, run_id: str = "r1") -> dict:
    return json.loads((runs_dir / run_id / "meta.json").read_text(encoding="utf-8"))


def _parse(*argv: str):
    return build_parser().parse_args(["delivery", *argv])


# ── parser ───────────────────────────────────────────────────────────────────


def test_parser_registers_gate_and_decide() -> None:
    args = _parse("gate", "r1", "--json")
    assert args.func is cmd_delivery_gate
    assert args.delivery_cmd == "gate"
    assert args.run_id == "r1"
    assert args.json is True
    assert args.workspace is None

    args = _parse("decide", "r1", "approve", "--note", "ok", "--json")
    assert args.func is cmd_delivery_decide
    assert args.delivery_cmd == "decide"
    assert args.action == "approve"
    assert args.note == "ok"
    assert args.json is True


@pytest.mark.parametrize("action", ["approve", "apply", "skip", "halt", "fix"])
def test_parser_accepts_every_sdk_action(action: str) -> None:
    assert _parse("decide", "r1", action).action == action


def test_parser_rejects_unknown_action(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse("decide", "r1", "merge")
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_delivery_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit) as exc:
        _parse()
    assert exc.value.code == 2


# ── gate ─────────────────────────────────────────────────────────────────────


def test_gate_on_approved_park_is_decidable(tmp_path: Path, capsys) -> None:
    _park(tmp_path)

    rc = cmd_delivery_gate(_parse("gate", "r1"))

    out = strip_ansi(capsys.readouterr().out)
    assert rc == 0
    assert "Delivery gate:   r1" in out
    assert "Run status:      halted" in out
    assert "Gate kind:       delivery" in out
    assert "Decidable:       yes" in out
    assert "Available:       approve, apply, skip, halt" in out
    assert "Default action:  approve" in out
    assert "Release verdict: APPROVED" in out
    assert "Requested at:    2026-07-29T09:31:22+00:00" in out
    assert "Gate status:     pending" in out
    assert "Changed:         app.txt" in out
    assert "orcho delivery decide r1" in out


def test_gate_json_is_one_object_with_state_and_gate_facts(
    tmp_path: Path, capsys,
) -> None:
    _park(tmp_path)

    rc = cmd_delivery_gate(_parse("gate", "r1", "--json"))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert captured.err == ""
    assert payload["run_id"] == "r1"
    assert payload["decidable"] is True
    assert payload["kind"] == "delivery"
    assert payload["available_actions"] == ["approve", "apply", "skip", "halt"]
    assert payload["gate"]["status"] == "pending"
    assert payload["gate"]["changed_paths"] == ["app.txt"]


def test_gate_without_gate_exits_1(tmp_path: Path, capsys) -> None:
    run_dir = tmp_path / "runs" / "r9"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")

    rc = cmd_delivery_gate(_parse("gate", "r9"))

    out = strip_ansi(capsys.readouterr().out)
    assert rc == 1
    assert "Gate kind:       none" in out
    assert "Decidable:       no" in out
    assert "no parked delivery gate" in out


def test_gate_on_stopped_run_exits_3_with_sdk_reason(tmp_path: Path, capsys) -> None:
    runs_dir, _, _ = _park(tmp_path)
    meta_path = runs_dir / "r1" / "meta.json"
    meta = _meta(runs_dir)
    meta["status"] = "failed"
    meta["halt_reason"] = "operator_halt"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    rc = cmd_delivery_gate(_parse("gate", "r1"))

    out = strip_ansi(capsys.readouterr().out)
    assert rc == 3
    assert "Run status:      failed" in out
    assert "Decidable:       no" in out
    assert "Available:       -" in out
    assert (
        "Reason:          run status 'failed' is stopped; resume first before "
        "deciding delivery"
    ) in out
    assert "cannot be decided right now" in out


def test_gate_unknown_run_reports_to_stderr(tmp_path: Path, capsys) -> None:
    rc = cmd_delivery_gate(_parse("gate", "nope", "--json"))

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "nope" in captured.err


# ── decide ───────────────────────────────────────────────────────────────────


def test_decide_approve_commits_and_marks_done(tmp_path: Path, capsys) -> None:
    runs_dir, repo, _ = _park(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")

    rc = cmd_delivery_decide(_parse("decide", "r1", "approve"))

    out = strip_ansi(capsys.readouterr().out)
    head_after = _git(repo, "rev-parse", "HEAD")
    assert rc == 0
    assert head_after != head_before
    assert "Accepted:        yes" in out
    assert "Action:          approve" in out
    assert f"Git commit:      {head_after}" in out
    assert "Run status:      done" in out
    meta = _meta(runs_dir)
    assert meta["status"] == "done"
    assert meta["commit_delivery"]["commit_sha"] == head_after


def test_decide_approve_json_is_one_object(tmp_path: Path, capsys) -> None:
    _, repo, _ = _park(tmp_path)

    rc = cmd_delivery_decide(_parse("decide", "r1", "approve", "--json"))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert captured.err == ""
    assert payload["accepted"] is True
    assert payload["action"] == "approve"
    assert payload["terminal_outcome"] == "done"
    assert payload["commit_sha"] == _git(repo, "rev-parse", "HEAD")


def test_decide_approve_on_rejected_gate_is_refused_by_sdk(
    tmp_path: Path, capsys,
) -> None:
    runs_dir, repo, wt = _park(tmp_path, verdict="REJECTED")
    head_before = _git(repo, "rev-parse", "HEAD")
    wt_head_before = _git(wt, "rev-parse", "HEAD")
    wt_status_before = _git(wt, "status", "--porcelain")

    rc = cmd_delivery_decide(_parse("decide", "r1", "approve"))

    out = strip_ansi(capsys.readouterr().out)
    assert rc == 1
    assert "Accepted:        no" in out
    assert "Blocker:         release_blocked" in out
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(wt, "rev-parse", "HEAD") == wt_head_before
    assert _git(wt, "status", "--porcelain") == wt_status_before
    meta = _meta(runs_dir)
    assert meta["status"] == "halted"
    assert meta["halt_reason"] == "commit_delivery_pending"


def test_decide_halt_with_note_halts_the_run(tmp_path: Path, capsys) -> None:
    runs_dir, repo, _ = _park(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")

    rc = cmd_delivery_decide(_parse("decide", "r1", "halt", "--note", "x"))

    out = strip_ansi(capsys.readouterr().out)
    assert rc == 0
    assert "Accepted:        yes" in out
    assert "Action:          halt" in out
    assert "Run status:      halted" in out
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _meta(runs_dir)["status"] == "halted"


def test_decide_unknown_run_is_a_usage_error(tmp_path: Path, capsys) -> None:
    rc = cmd_delivery_decide(_parse("decide", "nope", "approve", "--json"))

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.startswith("delivery decide: ")
    assert "nope" in captured.err


def test_decide_without_gate_is_a_typed_refusal(tmp_path: Path, capsys) -> None:
    run_dir = tmp_path / "runs" / "r9"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")

    rc = cmd_delivery_decide(_parse("decide", "r9", "approve"))

    out = strip_ansi(capsys.readouterr().out)
    assert rc == 1
    assert "Blocker:         no_pending_delivery_gate" in out
