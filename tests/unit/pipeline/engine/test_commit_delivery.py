"""Commit delivery executor tests (ADR 0032 / ADR 0043)."""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from core.io.git_helpers import create_worktree
from pipeline.engine import commit_delivery
from pipeline.engine.commit_delivery import (
    CommitDeliveryDecision,
    apply_commit_delivery,
    resolve_commit_delivery,
)
from pipeline.engine.pre_run_dirty import (
    apply_pre_run_dirty_seed,
    resolve_pre_run_dirty_intake,
)
from pipeline.project.run import _PipelineRun


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo,
        check=True,
    )
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return _head(repo)


def _head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_message(repo: Path) -> str:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%B", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _status(repo: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _new_worktree(repo: Path, run_dir: Path, *, run_id: str = "r1") -> Path:
    result = create_worktree(
        repo=repo,
        base_ref=_head(repo),
        target_path=run_dir / "checkout",
        branch_name=f"orcho/run/{run_id}",
    )
    assert result.ok, result.error
    return run_dir / "checkout"


def _session(summary: str = "feat: update app") -> dict:
    return {
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "short_summary": summary,
            },
        },
    }


def _resolve_and_apply(
    *,
    repo: Path,
    worktree: Path,
    run_dir: Path,
    action: str,
    baseline_ref: str = "HEAD",
):
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={
            "enabled": True,
            "auto_in_ci": action,
            "add_untracked": True,
        },
        no_interactive=True,
        baseline_ref=baseline_ref,
    )
    return apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
    )


def test_approve_applies_diff_and_commits_clean_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        action="approve",
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha
    assert delivered.commit_sha != old_head
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"
    assert _status(repo) == ""
    artifact = json.loads((run_dir / "commit_decisions" / "r1.json").read_text())
    assert artifact["commit_status"] == "committed"
    assert artifact["final_message"] == "feat: update app"


def test_delivery_commit_carries_signoff_trailer(tmp_path: Path) -> None:
    # ``git commit -s`` appends a ``Signed-off-by:`` trailer matching the
    # committer identity after the message body, without disturbing it.
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        action="approve",
    )

    assert delivered.status == "committed"
    message = _commit_message(repo)
    assert message.startswith("feat: update app")
    assert message.rstrip().endswith(
        "Signed-off-by: Orcho Test <test@orcho.invalid>"
    )


def test_delivery_commit_does_not_duplicate_existing_signoff(
    tmp_path: Path,
) -> None:
    # ``git commit -s`` de-duplicates an identical committer trailer already
    # present in the message body — no bespoke dedup is needed.
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    trailer = "Signed-off-by: Orcho Test <test@orcho.invalid>"
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
        },
        no_interactive=True,
        baseline_ref="HEAD",
    )
    # Committer already supplied an identical trailer in the message body.
    decision = dataclasses.replace(
        decision,
        final_message=f"feat: update app\n\n{trailer}",
    )
    delivered = apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
    )

    assert delivered.status == "committed"
    message = _commit_message(repo)
    assert message.count(trailer) == 1


def test_apply_transports_diff_without_committing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        action="apply",
    )

    assert delivered.status == "applied_uncommitted"
    assert _head(repo) == old_head
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"
    assert _status(repo) == "M app.txt"


def test_approve_in_place_delivery_commits_run_owned_dirty_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=repo,
        run_dir=run_dir,
        action="approve",
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha
    assert delivered.commit_sha != old_head
    assert delivered.target_dirty_paths == ()
    assert delivered.files_staged == ("app.txt",)
    assert _status(repo) == ""


def test_apply_in_place_delivery_keeps_run_owned_dirty_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=repo,
        run_dir=run_dir,
        action="apply",
    )

    assert delivered.status == "applied_uncommitted"
    assert delivered.target_dirty_paths == ()
    assert _head(repo) == old_head
    assert _status(repo) == "M app.txt"


def test_approve_in_place_delivery_stages_untracked_run_owned_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=repo,
        run_dir=run_dir,
        action="approve",
    )

    assert delivered.status == "committed"
    assert delivered.target_dirty_paths == ()
    assert delivered.files_staged == ("new.txt",)
    assert delivered.untracked_delivered == ("new.txt",)
    assert _status(repo) == ""


def test_in_place_delivery_still_blocks_unrelated_dirty_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(
        commit_delivery,
        "_target_dirty_paths",
        lambda _p: (" M app.txt", "?? unrelated.txt"),
    )

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=repo,
        run_dir=run_dir,
        action="approve",
    )

    assert delivered.status == "target_dirty"
    assert delivered.target_dirty_paths == ("?? unrelated.txt",)


def test_apply_transports_untracked_files_without_committing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "new.txt").write_text("new\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        action="apply",
    )

    assert delivered.status == "applied_uncommitted"
    assert delivered.untracked_delivered == ("new.txt",)
    assert _head(repo) == old_head
    assert (repo / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert _status(repo) == "?? new.txt"
    artifact = json.loads((run_dir / "commit_decisions" / "r1.json").read_text())
    assert artifact["untracked_delivered"] == ["new.txt"]


def test_approve_transports_untracked_files_and_commits(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "new.txt").write_text("new\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        action="approve",
    )

    assert delivered.status == "committed"
    assert delivered.untracked_delivered == ("new.txt",)
    assert delivered.commit_sha != old_head
    assert (repo / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert _status(repo) == ""
    files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "new.txt" in files


def test_python_bytecode_untracked_files_are_not_deliverable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    cache_dir = worktree / ".orcho" / "multiagent" / "__pycache__"
    cache_dir.mkdir(parents=True)
    (cache_dir / "plugin.cpython-312.pyc").write_bytes(b"bytecode")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={
            "enabled": True,
            "auto_in_ci": "apply",
            "add_untracked": True,
        },
        no_interactive=True,
    )

    assert decision.status == "no_diff"
    assert decision.untracked_paths == ()


def test_pre_existing_untracked_in_project_triggers_target_guard(
    tmp_path: Path,
) -> None:
    """B1.2 supersedes the in-engine collision protection: a pre-
    existing untracked file in ``project_dir`` now triggers the
    target-dirty guard *before* ``_transport_untracked`` runs. The
    operator-visible outcome is the same (no overwrite, local file
    preserved) but surfaced earlier via the guard.

    The defence-in-depth collision check inside ``_transport_untracked``
    remains; it is exercised directly in
    ``test_transport_untracked_collision_defends_in_depth`` below.
    """
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "new.txt").write_text("from run\n", encoding="utf-8")
    (repo / "new.txt").write_text("local file\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        action="apply",
    )

    assert delivered.status == "target_dirty"
    assert any("new.txt" in line for line in delivered.target_dirty_paths)
    assert _head(repo) == old_head
    assert (repo / "new.txt").read_text(encoding="utf-8") == "local file\n"


def test_transport_untracked_collision_defends_in_depth(
    tmp_path: Path,
) -> None:
    """Direct unit on the defence-in-depth collision check.

    Calls ``_transport_untracked`` against a hand-built decision so
    the target-dirty guard does not pre-empt it. Ensures the lower
    layer still refuses to overwrite an existing destination file.
    """
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    (worktree / "new.txt").write_text("from run\n", encoding="utf-8")
    (repo / "new.txt").write_text("local file\n", encoding="utf-8")

    from pipeline.engine.commit_delivery import (
        CommitDeliveryDecision,
        _transport_untracked,
    )

    decision = CommitDeliveryDecision(
        action="apply",
        status="pending",
        run_id="r1",
        decision_id="r1",
        project_path=repo,
        source_path=worktree,
        baseline_ref="HEAD",
        untracked_paths=("new.txt",),
        include_untracked=True,
    )
    result = _transport_untracked(decision)
    assert result.error
    assert "destination already exists" in result.error
    assert (repo / "new.txt").read_text(encoding="utf-8") == "local file\n"


def test_interactive_default_is_apply_when_operator_accepts_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={
            "enabled": True,
            "interactive_default": "apply",
            "auto_in_ci": "approve",
            "add_untracked": True,
        },
        no_interactive=False,
        input_fn=lambda _prompt: "",
        output_fn=lambda _line: None,
    )

    assert decision.action == "apply"
    assert decision.final_message is None


def _menu_blob(
    *,
    repo: Path,
    worktree: Path,
    run_dir: Path,
    monkeypatch,
    branch_policy: str | None,
) -> str:
    """Render the interactive delivery menu and return its joined text."""
    from core.io.ansi import strip_ansi

    lines: list[str] = []
    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    cfg: dict = {
        "enabled": True,
        "interactive_default": "skip",
        "auto_in_ci": "approve",
        "add_untracked": True,
    }
    if branch_policy is not None:
        cfg["branch_policy"] = branch_policy
    resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config=cfg,
        no_interactive=False,
        input_fn=lambda _prompt: "",
        output_fn=lines.append,
    )
    return strip_ansi("\n".join(lines))


def test_interactive_menu_published_branch_describes_push_and_pr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # An isolated (per_run) worktree under the default ``worktree_branch`` policy
    # publishes the run branch and opens a PR — the checkout is never touched.
    # The menu must say so and must NOT promise a checkout commit.
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    blob = _menu_blob(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        monkeypatch=monkeypatch,
        branch_policy=None,
    )

    assert "delivery branch" in blob
    assert "open a pull request" in blob
    assert "your project checkout is NOT modified" in blob
    # The stale wording that lied about a publish as a checkout commit is gone.
    assert "Apply the diff to the project checkout AND create a commit." not in blob


def test_interactive_menu_bypass_keeps_checkout_commit_wording(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # ``bypass`` commits in place on the checkout, so the historical wording
    # ("apply the diff to the project checkout AND create a commit") is correct
    # and must be preserved.
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    blob = _menu_blob(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        monkeypatch=monkeypatch,
        branch_policy="bypass",
    )

    assert "Apply the diff to the project checkout AND create a commit." in blob
    assert "your project checkout is NOT modified" not in blob
    assert "open a pull request" not in blob


def test_interactive_prompt_marks_tracked_and_untracked_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    (worktree / "new.py").write_text("print('new')\n", encoding="utf-8")
    lines: list[str] = []

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={
            "enabled": True,
            "interactive_default": "apply",
            "auto_in_ci": "approve",
            "add_untracked": True,
        },
        no_interactive=False,
        input_fn=lambda _prompt: "",
        output_fn=lines.append,
    )

    assert decision.action == "apply"
    assert decision.changed_paths == ("app.txt",)
    assert decision.untracked_paths == ("new.py",)
    assert "  M  app.txt" in lines
    assert "  ?? new.py" in lines


def test_non_interactive_default_stays_approve(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={
            "enabled": True,
            "interactive_default": "apply",
            "auto_in_ci": "approve",
            "add_untracked": True,
        },
        no_interactive=True,
    )

    assert decision.action == "approve"
    assert decision.final_message == "feat: update app"
    assert decision.commit_message_strategy == "release_summary"


def test_llm_generate_strategy_uses_generated_commit_message(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session("release summary should not be used"),
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
            "default_strategy": "llm_generate",
        },
        no_interactive=True,
        commit_message_generator=lambda _decision: (
            "fix(delivery): use agent commit message\n"
        ),
    )
    delivered = apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
    )

    assert delivered.status == "committed"
    assert delivered.final_message == "fix(delivery): use agent commit message"
    assert delivered.commit_message_strategy == "llm_generate"
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject == "fix(delivery): use agent commit message"
    artifact = json.loads(
        (run_dir / "commit_decisions" / "r1.json").read_text(encoding="utf-8")
    )
    assert artifact["strategy"] == "llm_generate"
    assert artifact["final_message"] == "fix(delivery): use agent commit message"


def test_llm_generate_strategy_falls_back_to_release_summary(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session("docs: keep summary default"),
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
            "default_strategy": "llm_generate",
        },
        no_interactive=True,
        commit_message_generator=lambda _decision: " \n",
    )

    assert decision.final_message == "docs: keep summary default"
    assert decision.commit_message_strategy == "release_summary"


def test_skip_leaves_diff_only_in_run_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        action="skip",
    )

    assert delivered.status == "skipped"
    assert _head(repo) == old_head
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert _status(repo) == ""


def test_halt_records_delivery_gate_halt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo,
        worktree=worktree,
        run_dir=run_dir,
        action="halt",
    )

    assert delivered.status == "halted"
    artifact = json.loads((run_dir / "commit_decisions" / "r1.json").read_text())
    assert artifact["action"] == "halt"
    assert artifact["commit_status"] == "halted"


def test_apply_uses_pre_run_dirty_seed_tree_as_baseline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "app.txt").write_text("base\nseed\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=run_dir,
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "non_interactive_default": "include",
            "include_untracked": "none",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )
    worktree = _new_worktree(repo, run_dir)
    seeded = apply_pre_run_dirty_seed(
        intake,
        project_dir=repo,
        worktree_path=worktree,
    )
    assert seeded.seed_tree_sha
    (worktree / "app.txt").write_text("base\nseed\nrun\n", encoding="utf-8")

    # Phase A invariant — the seed_tree_sha baseline produces a patch
    # that contains only the run's delta on top of the seeded state, not
    # the seed itself. This is the load-bearing piece that prevents
    # apply→include from re-delivering the seed in subsequent runs.
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "apply"},
        no_interactive=True,
        baseline_ref=seeded.seed_tree_sha,
    )
    assert decision.status == "pending"
    # The diff is "+run\n" only — "+seed\n" already lived in the seed
    # baseline, so it is absent from the run-owned patch.
    assert "+run" in decision.patch_text
    assert "+seed" not in decision.patch_text

    # B1.2 invariant — apply itself now halts on the dirty target,
    # surfacing target_dirty rather than silently merging into the
    # uncommitted seed. The operator decides how to combine journeys.
    delivered = apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=True,
    )
    assert delivered.status == "target_dirty"
    assert any("app.txt" in line for line in delivered.target_dirty_paths)
    # Project still holds only the operator-owned seed dirty state;
    # the run's "+run" delta did NOT land.
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nseed\n"


def _seed_run_with_untracked(
    *,
    repo: Path,
    run_dir: Path,
    untracked_rel: str,
    untracked_body: str,
):
    """Seed an isolated run worktree with an ``include`` + all-untracked intake.

    Mirrors the operator choosing pre-run intake ``include`` and
    answering yes to "Include all untracked files in this run seed?":
    the untracked file is folded into ``seed_tree_sha`` as a tracked add
    while the worktree HEAD stays at the pre-seed base, leaving the file
    untracked on disk. Returns ``(worktree, seed_tree_sha)``.

    The untracked file's parent directory is given a committed sibling
    first so ``git status`` reports the new file individually (a wholly
    new directory would collapse to a ``dir/`` entry) — matching the
    Unity ``Scripts/Store/`` repro where the directory already existed.
    """
    src = repo / untracked_rel
    src.parent.mkdir(parents=True, exist_ok=True)
    (src.parent / ".keep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed sibling"], cwd=repo, check=True,
    )
    src.write_text(untracked_body, encoding="utf-8")
    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=run_dir,
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "non_interactive_default": "include",
            "include_untracked": "all",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )
    assert untracked_rel in intake.selected_untracked_paths
    worktree = _new_worktree(repo, run_dir)
    seeded = apply_pre_run_dirty_seed(
        intake,
        project_dir=repo,
        worktree_path=worktree,
    )
    assert seeded.seed_tree_sha
    # The seed left the file untracked on disk in the worktree; the run
    # itself never touched it.
    assert (worktree / untracked_rel).read_text(encoding="utf-8") == untracked_body
    return worktree, seeded.seed_tree_sha


def test_seeded_untracked_file_is_classified_once_not_double_listed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A run-seeded untracked file must render as ``??`` only, never ``M`` + ``??``.

    Regression: with intake=include + include-untracked, the seeded file
    lands in the seed baseline as a tracked add but stays untracked on
    disk, so ``git diff --name-only <seed_tree>`` AND ``ls-files
    --others`` both report it. Untracked wins — it appears in
    ``untracked_paths`` once and is absent from ``changed_paths``, so the
    delivery gate prints a single ``??`` line. A genuinely run-modified
    tracked file still shows as ``M`` (the dedup is surgical).
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree, seed_tree_sha = _seed_run_with_untracked(
        repo=repo,
        run_dir=run_dir,
        untracked_rel="Scripts/Store/StoreReturn.cs",
        untracked_body="store return code\n",
    )
    # A real run-owned modification to a tracked file — this MUST keep its
    # ``M`` classification; only the phantom untracked dup is dropped.
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    lines: list[str] = []
    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={
            "enabled": True,
            "interactive_default": "apply",
            "auto_in_ci": "approve",
            "add_untracked": True,
        },
        no_interactive=False,
        baseline_ref=seed_tree_sha,
        input_fn=lambda _prompt: "",
        output_fn=lines.append,
    )

    assert "Scripts/Store/StoreReturn.cs" in decision.untracked_paths
    assert "Scripts/Store/StoreReturn.cs" not in decision.changed_paths
    assert decision.changed_paths == ("app.txt",)
    # Rendered exactly once, as untracked — no phantom ``M`` line.
    assert "  ?? Scripts/Store/StoreReturn.cs" in lines
    assert "  M  Scripts/Store/StoreReturn.cs" not in lines
    assert "  M  app.txt" in lines


# ── B1.2: target-dirty guard ───────────────────────────────────────────────


def test_approve_with_dirty_project_non_interactive_records_target_dirty(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    # Operator-owned parallel dirty work (modified tracked + untracked).
    (repo / "app.txt").write_text("base\nlocal-edit\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("local notes\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo, worktree=worktree, run_dir=run_dir, action="approve",
    )

    assert delivered.status == "target_dirty"
    assert delivered.target_dirty_retries == 0
    paths = list(delivered.target_dirty_paths)
    assert any(p.startswith(" M ") and "app.txt" in p for p in paths)
    assert any(p.startswith("?? ") and "scratch.txt" in p for p in paths)
    assert _head(repo) == old_head
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nlocal-edit\n"
    artifact = json.loads(
        (run_dir / "commit_decisions" / "r1.json").read_text(encoding="utf-8")
    )
    assert artifact["commit_status"] == "target_dirty"
    assert "target_dirty_paths" in artifact
    assert artifact["commit_sha"] is None
    assert artifact["commit_error"] is None


def test_apply_with_dirty_project_non_interactive_records_target_dirty(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\nlocal\n", encoding="utf-8")

    delivered = _resolve_and_apply(
        repo=repo, worktree=worktree, run_dir=run_dir, action="apply",
    )

    assert delivered.status == "target_dirty"
    assert delivered.target_dirty_retries == 0
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nlocal\n"
    assert _head(repo) == old_head


def test_no_tty_treated_as_non_interactive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """no_interactive=False is overridden when stdio is not a TTY (MCP /
    background runs). Prevents input() from blocking."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\ndirty\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: False)

    def _no_input(prompt: str) -> str:
        raise AssertionError(f"input() called in non-TTY context: {prompt!r}")

    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "apply"},
        no_interactive=True,
        input_fn=_no_input,
    )
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,  # would normally prompt
        input_fn=_no_input,
    )
    assert delivered.status == "target_dirty"


def test_interactive_retry_then_clean_proceeds_with_original_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    # First check returns dirty, second clean (operator cleaned up).
    state = {"calls": 0}

    def fake_target_dirty(_project_dir):
        state["calls"] += 1
        return (" M app.txt",) if state["calls"] == 1 else ()

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    monkeypatch.setattr(commit_delivery, "_target_dirty_paths", fake_target_dirty)

    answers = iter(["retry"])
    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "apply"},
        no_interactive=True,
    )
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,
        input_fn=lambda _p: next(answers),
        output_fn=lambda _l: None,
    )

    assert delivered.status == "applied_uncommitted"
    assert delivered.target_dirty_retries == 1
    # Stale dirty paths are not carried into the success artifact.
    assert delivered.target_dirty_paths == ()
    artifact = json.loads(
        (run_dir / "commit_decisions" / "r1.json").read_text(encoding="utf-8")
    )
    assert artifact["commit_status"] == "applied_uncommitted"
    assert artifact["target_dirty_retries"] == 1
    assert "target_dirty_paths" not in artifact


def test_interactive_retry_then_still_dirty_then_skip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    monkeypatch.setattr(
        commit_delivery, "_target_dirty_paths",
        lambda _p: (" M app.txt",),
    )

    answers = iter(["retry", "skip"])
    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,
        input_fn=lambda _p: next(answers),
        output_fn=lambda _l: None,
    )

    assert delivered.action == "skip"
    assert delivered.status == "skipped"
    assert delivered.target_dirty_retries == 1
    assert " M app.txt" in delivered.target_dirty_paths


def test_interactive_immediate_skip_at_dirty_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    monkeypatch.setattr(
        commit_delivery, "_target_dirty_paths",
        lambda _p: (" M app.txt",),
    )

    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "apply"},
        no_interactive=True,
    )
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,
        input_fn=lambda _p: "skip",
        output_fn=lambda _l: None,
    )

    assert delivered.action == "skip"
    assert delivered.status == "skipped"
    assert delivered.target_dirty_retries == 0
    assert " M app.txt" in delivered.target_dirty_paths


def test_interactive_immediate_halt_at_dirty_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    monkeypatch.setattr(
        commit_delivery, "_target_dirty_paths",
        lambda _p: (" M app.txt", "?? scratch.txt"),
    )

    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,
        input_fn=lambda _p: "halt",
        output_fn=lambda _l: None,
    )

    assert delivered.action == "halt"
    assert delivered.status == "halted"
    assert delivered.target_dirty_retries == 0
    # Halt artifact still carries the porcelain lines that explain the block.
    assert " M app.txt" in delivered.target_dirty_paths
    assert "?? scratch.txt" in delivered.target_dirty_paths


def test_skip_action_bypasses_target_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """decision.action='skip' (voluntary skip on clean prompt) must not
    invoke the target guard — skip never writes to project_dir."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("local\n", encoding="utf-8")  # dirty

    sentinel = {"called": False}

    def fake_target_dirty(_p):
        sentinel["called"] = True
        return (" M nope.txt",)

    monkeypatch.setattr(commit_delivery, "_target_dirty_paths", fake_target_dirty)

    delivered = _resolve_and_apply(
        repo=repo, worktree=worktree, run_dir=run_dir, action="skip",
    )

    assert delivered.status == "skipped"
    assert sentinel["called"] is False


def test_halt_action_bypasses_target_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("local\n", encoding="utf-8")

    sentinel = {"called": False}

    def fake_target_dirty(_p):
        sentinel["called"] = True
        return (" M nope.txt",)

    monkeypatch.setattr(commit_delivery, "_target_dirty_paths", fake_target_dirty)

    delivered = _resolve_and_apply(
        repo=repo, worktree=worktree, run_dir=run_dir, action="halt",
    )

    assert delivered.status == "halted"
    assert sentinel["called"] is False


def test_path_scoped_staging_excludes_race_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The actual race the path-scoped ``git add`` defends against:
    project_dir is clean when the guard checks, then between transport
    and staging an unrelated file appears. With blanket ``git add -A``
    that file would slip into the commit; with path-scoped ``git add --``
    it stays out."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    orig_transport = commit_delivery._transport_patch

    def transport_then_race(decision):
        result = orig_transport(decision)
        # Operator creates an unrelated file mid-flight, after guard saw
        # clean checkout and after the run's diff was applied to repo.
        (decision.project_path / "race.py").write_text(
            "racing\n", encoding="utf-8",
        )
        return result

    monkeypatch.setattr(commit_delivery, "_transport_patch", transport_then_race)

    delivered = _resolve_and_apply(
        repo=repo, worktree=worktree, run_dir=run_dir, action="approve",
    )

    assert delivered.status == "committed"
    # The run-owned change landed; the race file stayed out.
    files_in_commit = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "app.txt" in files_in_commit
    assert "race.py" not in files_in_commit
    # The race file survives as untracked — user-owned, not lost.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "?? race.py" in status


def test_pure_untracked_with_add_untracked_false_resolves_to_no_diff(
    tmp_path: Path,
) -> None:
    """Pure-untracked run + commit.add_untracked=False must short-circuit
    in resolve as no_diff. Without the resolve-fix, path-scoped staging
    would otherwise hit commit_failed."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "new.txt").write_text("untracked-only\n", encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": False,
        },
        no_interactive=True,
    )

    assert decision.action == "none"
    assert decision.status == "no_diff"


def test_profile_without_final_acceptance_delivers_implicitly(
    tmp_path: Path,
) -> None:
    """Lite-shaped profile (no ``final_acceptance`` phase) must still deliver.

    Regression for the gap where ``resolve_commit_delivery`` short-
    circuited to ``not_applicable`` whenever the session carried no
    release verdict. The caller already restricts entry to runs with
    ``session["status"] == "done"``, so an absent ``final_acceptance``
    entry is implicit approval — the profile simply chose not to
    release-gate delivery — not a missing verdict.
    """
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session={"status": "done", "phases": {"plan": {}, "implement": {}}},
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
        },
        no_interactive=True,
        baseline_ref="HEAD",
    )
    delivered = apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
    )

    assert delivered.status == "committed", delivered.error
    assert _head(repo) != old_head
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"
    artifact = json.loads(
        (run_dir / "commit_decisions" / "r1.json").read_text(encoding="utf-8")
    )
    assert artifact["commit_status"] == "committed"
    assert artifact["action"] == "approve"
    # Fallback commit subject used because there is no release_summary.
    assert artifact["final_message"].startswith("chore: deliver orcho run ")


def test_profile_with_rejected_final_acceptance_still_blocks(
    tmp_path: Path,
) -> None:
    """A profile that DOES gate on ``final_acceptance`` and rejects must
    not deliver — only an *absent* entry is implicit approval.

    Pairs with ``test_profile_without_final_acceptance_delivers_implicitly``
    to lock the present-but-rejected vs. absent distinction.
    """
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session={
            "status": "done",
            "phases": {"final_acceptance": {"verdict": "REJECTED"}},
        },
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
        },
        no_interactive=True,
        baseline_ref="HEAD",
    )

    assert decision.action == "none"
    assert decision.status == "not_applicable"
    assert _head(repo) == old_head
    assert _status(repo) == ""


def test_target_dirty_artifact_preserves_porcelain_prefixes(
    tmp_path: Path,
) -> None:
    """The audit artifact must carry the porcelain prefix (' M ', '?? ')
    so operators can tell modified from added/untracked at a glance."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\nlocal\n", encoding="utf-8")  # modified
    (repo / "scratch.txt").write_text("local\n", encoding="utf-8")     # untracked

    _resolve_and_apply(
        repo=repo, worktree=worktree, run_dir=run_dir, action="approve",
    )

    artifact = json.loads(
        (run_dir / "commit_decisions" / "r1.json").read_text(encoding="utf-8")
    )
    porcelain = artifact["target_dirty_paths"]
    # At least one " M " (modified tracked) and one "?? " (untracked).
    assert any(line.startswith(" M ") for line in porcelain)
    assert any(line.startswith("?? ") for line in porcelain)


# ── retry-rebase: operator cleared dirty via real commit ───────────────────


def test_interactive_retry_after_operator_commit_rebases_patch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Operator commits their dirty work during the gate pause, then
    picks retry. The cached patch was anchored on the original baseline
    and would fail ``git apply --check`` against the advanced HEAD. The
    retry path must re-anchor against the current project HEAD so the
    run-owned delta actually delivers — not silently fail."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)

    # Operator has uncommitted edit when run started.
    (repo / "app.txt").write_text("base\noperator\n", encoding="utf-8")
    # Worktree carries that same content (simulating pre_run_dirty seed)
    # plus the run's own delta on top.
    (worktree / "app.txt").write_text(
        "base\noperator\nrun\n", encoding="utf-8",
    )

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)

    def commit_then_retry(_prompt):
        # Operator commits their dirty work in another terminal while
        # the gate is paused. project_dir HEAD advances.
        subprocess.run(
            ["git", "add", "app.txt"], cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "operator: stash"],
            cwd=repo, check=True,
        )
        return "retry"

    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    original_baseline = decision.baseline_ref
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,
        input_fn=commit_then_retry,
        output_fn=lambda _l: None,
    )

    assert delivered.status == "committed", delivered.error
    assert delivered.target_dirty_retries == 1
    # Project ends up with operator's commit + run's delta on top.
    assert (repo / "app.txt").read_text(encoding="utf-8") == (
        "base\noperator\nrun\n"
    )
    # The rebase moved the baseline off the original ref.
    assert delivered.baseline_ref != original_baseline
    # Two commits land on top of the initial: operator's, then run's.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert len(log) == 3


def test_interactive_retry_rebase_keeps_unrelated_operator_commit_out(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A dirty-gate retry must not expand delivery to target-only commits.

    The operator may clear unrelated dirty state by committing it while
    delivery is paused. Re-anchoring the run patch against that newer HEAD must
    stay scoped to the original run-owned paths.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)

    (repo / "operator.txt").write_text("operator\n", encoding="utf-8")
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)

    def commit_then_retry(_prompt):
        subprocess.run(["git", "add", "operator.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "operator: unrelated"],
            cwd=repo, check=True,
        )
        return "retry"

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    assert decision.changed_paths == ("app.txt",)

    delivered = apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,
        input_fn=commit_then_retry,
        output_fn=lambda _l: None,
    )

    assert delivered.status == "committed", delivered.error
    assert delivered.target_dirty_retries == 1
    assert delivered.changed_paths == ("app.txt",)
    files_in_delivery_commit = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert files_in_delivery_commit == ["app.txt"]
    files_in_operator_commit = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD~1"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert files_in_operator_commit == ["operator.txt"]


def test_interactive_retry_after_operator_commits_run_output_reports_no_op(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Edge case: operator manually commits exactly the run's output.
    After rebase the diff is empty, so there is no work left. Surface
    that as a failure with an explanatory error instead of silently
    declaring success — the user picked approve and deserves to see
    that Orcho did not own the commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)

    # Operator's dirty edit IS the run's intended output.
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)

    def commit_then_retry(_prompt):
        subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "operator: same content"],
            cwd=repo, check=True,
        )
        return "retry"

    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,
        input_fn=commit_then_retry,
        output_fn=lambda _l: None,
    )

    assert delivered.status == "commit_failed"
    assert delivered.target_dirty_retries == 1
    assert delivered.error and "operator commit" in delivered.error
    artifact = json.loads(
        (run_dir / "commit_decisions" / "r1.json").read_text(encoding="utf-8")
    )
    assert artifact["commit_status"] == "commit_failed"
    assert artifact["commit_error"]


def test_interactive_retry_after_operator_divergent_commit_still_applies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Operator commits content that differs from the worktree (e.g.
    they edited a different region of the same file before committing).
    The rebased patch must capture only the remaining delta and apply
    cleanly on top of the divergent project HEAD."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)

    # Start: app.txt = "base\n".
    # Worktree adds a tail line.
    (worktree / "app.txt").write_text("base\nrun-tail\n", encoding="utf-8")
    # Operator independently adds a HEAD line and commits it before retry.
    (repo / "app.txt").write_text("operator-head\nbase\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)

    def commit_then_retry(_prompt):
        subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "operator: prepend header"],
            cwd=repo, check=True,
        )
        return "retry"

    decision = resolve_commit_delivery(
        project_dir=repo, source_worktree=worktree,
        run_dir=run_dir, run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
        no_interactive=False,
        input_fn=commit_then_retry,
        output_fn=lambda _l: None,
    )

    # The rebased patch is "worktree state vs operator HEAD" — the run
    # owns the worktree, so its delivered content overwrites the
    # operator's head line. This is honest: the run had no awareness of
    # the operator's parallel edit; what matters is that delivery is
    # not a silent failure.
    assert delivered.status == "committed", delivered.error
    assert delivered.target_dirty_retries == 1
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun-tail\n"


def _rejected_session(summary: str = "not ship-ready") -> dict:
    return {
        "status": "done",
        "phases": {
            "final_acceptance": {"verdict": "REJECTED", "short_summary": summary},
        },
    }


def test_rejected_verdict_interactive_still_offers_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """REJECTED at a TTY must show the correction-first gate. The operator
    can still deliver the diff anyway (their checkout, their call). Default is fix;
    here the operator explicitly overrides with ``approve``."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    lines: list[str] = []

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_rejected_session(),
        commit_config={
            "enabled": True,
            "interactive_default": "apply",
            "auto_in_ci": "approve",
            "add_untracked": True,
        },
        no_interactive=False,
        input_fn=lambda _prompt: "approve",  # operator overrides the rejection
        output_fn=lines.append,
    )

    # The prompt appeared and warned about the rejection.
    out = "\n".join(lines)
    assert "did NOT approve" in out
    assert "Correction gate" in out
    assert "Default is fix" in out
    assert "fix" in out
    # skip vs halt is disambiguated by the run-status consequence (DONE vs
    # HALTED) so the operator is not left guessing which "don't deliver" to pick.
    assert "Retain artifacts" in out
    assert "HALTED" in out
    # Operator's explicit override is honoured.
    assert decision.status == "pending"
    assert decision.action == "approve"
    assert decision.release_verdict == "REJECTED"


def test_rejected_verdict_interactive_default_fix_does_not_deliver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A bare Enter on a rejected change must NOT deliver — the default is fix."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: True)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_rejected_session(),
        commit_config={"enabled": True, "interactive_default": "apply"},
        no_interactive=False,
        input_fn=lambda _prompt: "",  # bare Enter → default
        output_fn=lambda _line: None,
    )
    assert decision.action == "fix"


def test_fix_action_persists_correction_request(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    decision = CommitDeliveryDecision(
        action="fix",
        status="pending",
        run_id="r1",
        decision_id="r1",
        project_path=repo,
        source_path=worktree,
        baseline_ref="HEAD",
        patch_text="diff",
        changed_paths=("app.txt",),
    )

    delivered = apply_commit_delivery(
        decision,
        run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
    )

    assert delivered.action == "fix"
    assert delivered.status == "fix_requested"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"
    artifact = json.loads(
        (run_dir / "commit_decisions" / "r1.json").read_text(encoding="utf-8")
    )
    assert artifact["action"] == "fix"
    assert artifact["commit_status"] == "fix_requested"


def test_pipeline_run_fix_request_marks_followup_halt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    decision = CommitDeliveryDecision(
        action="fix",
        status="pending",
        run_id="r1",
        decision_id="r1",
        project_path=repo,
        source_path=worktree,
        baseline_ref="HEAD",
    )
    delivered = CommitDeliveryDecision(
        action="fix",
        status="fix_requested",
        run_id="r1",
        decision_id="r1",
        project_path=repo,
        source_path=worktree,
        baseline_ref="HEAD",
        decided_at="2026-06-03T00:00:00+00:00",
    )
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(commit={"enabled": True}),
    )
    monkeypatch.setattr(
        commit_delivery,
        "resolve_commit_delivery",
        lambda **_kwargs: decision,
    )
    monkeypatch.setattr(
        commit_delivery,
        "apply_commit_delivery",
        lambda _decision, **_kwargs: delivered,
    )
    run = SimpleNamespace(
        output_dir=run_dir,
        session={"status": "done"},
        parent_run_id=None,
        project_alias=None,
        project_path=repo,
        no_interactive=True,
        session_ts="r1",
        _commit_delivery_baseline=lambda: "HEAD",
    )

    _PipelineRun._run_commit_delivery(run, worktree)

    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "commit_decision_fix"
    assert run.session["commit_delivery"]["status"] == "fix_requested"


def test_pipeline_run_rejected_acceptance_offers_correction_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    decision = CommitDeliveryDecision(
        action="fix",
        status="pending",
        run_id="r1",
        decision_id="r1",
        project_path=repo,
        source_path=worktree,
        baseline_ref="HEAD",
    )
    delivered = CommitDeliveryDecision(
        action="fix",
        status="fix_requested",
        run_id="r1",
        decision_id="r1",
        project_path=repo,
        source_path=worktree,
        baseline_ref="HEAD",
        decided_at="2026-06-03T00:00:00+00:00",
    )
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(commit={"enabled": True}),
    )
    monkeypatch.setattr(
        commit_delivery,
        "resolve_commit_delivery",
        lambda **_kwargs: decision,
    )
    monkeypatch.setattr(
        commit_delivery,
        "apply_commit_delivery",
        lambda _decision, **_kwargs: delivered,
    )
    run = SimpleNamespace(
        output_dir=run_dir,
        session={
            "status": "failed",
            "phases": {
                "final_acceptance": {
                    "verdict": "REJECTED",
                    "short_summary": "not ship-ready",
                },
            },
        },
        parent_run_id=None,
        project_alias=None,
        project_path=repo,
        no_interactive=True,
        session_ts="r1",
        _commit_delivery_baseline=lambda: "HEAD",
    )

    _PipelineRun._run_commit_delivery(run, worktree)

    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "commit_decision_fix"
    assert run.session["commit_delivery"]["status"] == "fix_requested"


def test_pipeline_run_failed_without_rejected_acceptance_skips_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    called = False

    def resolve_unexpected(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("commit delivery should stay silent")

    monkeypatch.setattr(commit_delivery, "resolve_commit_delivery", resolve_unexpected)
    run = SimpleNamespace(
        output_dir=run_dir,
        session={"status": "failed", "phases": {"implement": {"status": "failed"}}},
        parent_run_id=None,
        project_alias=None,
    )

    _PipelineRun._run_commit_delivery(run, worktree)

    assert called is False
    assert "commit_delivery" not in run.session


def test_rejected_verdict_non_interactive_stays_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CI / piped: a rejected change is never auto-delivered."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    monkeypatch.setattr(commit_delivery, "stdio_interactive", lambda: False)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_rejected_session(),
        commit_config={"enabled": True, "auto_in_ci": "approve"},
        no_interactive=True,
        baseline_ref="HEAD",
    )
    assert decision.status == "not_applicable"
    assert decision.action == "none"


def test_delivery_anchors_on_nested_git_dir_not_registered_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Regression (B7): when the registered project dir is NOT the git root
    (nested ``git_dir`` — e.g. a Unity project under SVN with the C# repo at
    ``Assets/_Match-Three-Common``), delivery must apply/commit against the
    git root. Before the fix, ``git apply`` ran from the SVN root and failed
    with "No such file or directory" because the run-owned patch paths are
    relative to the git root.
    """
    project_root = tmp_path / "unity_project"          # registered project (no .git)
    git_dir_rel = "Assets/_Match-Three-Common"
    nested_repo = project_root / git_dir_rel           # actual git root
    old_head = _init_repo(nested_repo)

    # The workspace config records the nested git_dir for this project.
    monkeypatch.setattr(
        "pipeline.project.project_aliases.load_workspace_project_git_dir",
        lambda p, *a, **k: git_dir_rel if Path(p) == project_root else "",
    )

    run_dir = tmp_path / "run"
    worktree = _new_worktree(nested_repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=project_root,                      # caller passes SVN root
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config={"enabled": True, "auto_in_ci": "approve", "add_untracked": True},
        no_interactive=True,
        baseline_ref="HEAD",
    )
    # Re-anchored onto the git root, not the registered (SVN) project root.
    assert decision.project_path == nested_repo
    assert not (project_root / ".git").exists()

    delivered = apply_commit_delivery(
        decision, run_dir=run_dir,
        commit_config={"add_untracked": True, "branch_policy": "bypass"},
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha and delivered.commit_sha != old_head
    assert (nested_repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"
    assert _status(nested_repo) == ""


# ─────────────────────────────────────────────────────────────────────────
# ADR 0119 — branch_policy × isolation at the single commit site.
#
# These pin the default-branch invariant end-to-end through apply_commit_delivery
# on real git commits. A missing ``branch_policy`` key resolves to the ADR 0119
# default ``worktree_branch`` (never ``bypass``), so the mechanics tests above
# opt out explicitly with ``branch_policy="bypass"`` to keep committing onto the
# current HEAD; each mode below is exercised with an explicit policy.
# ─────────────────────────────────────────────────────────────────────────


def _git_str(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _current_branch(repo: Path) -> str:
    return _git_str(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _branch_exists(repo: Path, name: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        cwd=repo, capture_output=True,
    ).returncode == 0


def _apply_with_policy(
    *,
    repo: Path,
    worktree: Path,
    run_dir: Path,
    branch_policy: str,
    branch_name: str | None = None,
    action: str = "approve",
    baseline_ref: str = "HEAD",
):
    commit_config: dict = {
        "enabled": True,
        "auto_in_ci": action,
        "add_untracked": True,
        "branch_policy": branch_policy,
    }
    if branch_name is not None:
        commit_config["branch_name"] = branch_name
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config=commit_config,
        no_interactive=True,
        baseline_ref=baseline_ref,
    )
    return apply_commit_delivery(
        decision, run_dir=run_dir, commit_config=commit_config,
    )


def test_worktree_branch_publishes_and_leaves_canonical_untouched(
    tmp_path: Path,
) -> None:
    """worktree_branch (per_run): the canonical checkout's HEAD and working tree
    are never touched; the run's work is published as ``orcho/deliver/…`` and the
    decision surfaces ``commit_sha=None`` + ``delivery_branch``."""
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply_with_policy(
        repo=repo, worktree=worktree, run_dir=run_dir,
        branch_policy="worktree_branch",
    )

    assert delivered.status == "committed"
    # Invariant: canonical checkout untouched — no commit landed on it.
    assert _head(repo) == old_head
    assert _current_branch(repo) == "main"
    assert _status(repo) == ""
    # commit_sha absent for a pure publish; the branch is the deliverable.
    assert delivered.commit_sha is None
    assert delivered.delivery_branch is not None
    assert delivered.delivery_branch.startswith("orcho/deliver/r1-")
    assert delivered.pr_intent is not None
    assert delivered.pr_intent.base == "main"
    # The published branch carries the run's change.
    assert _branch_exists(repo, delivered.delivery_branch)
    assert _git_str(repo, "show", f"{delivered.delivery_branch}:app.txt") == "base\nrun"
    # Serialized projection carries the additive keys.
    payload = delivered.to_dict()
    assert payload["delivery_branch"] == delivered.delivery_branch
    assert "commit_sha" not in payload
    # No git-provider is registered in the test env, so no PR was opened:
    # the typed twin stays None and the notice invites a manual PR.
    assert delivered.pr_url is None
    assert payload["pr_url"] is None
    assert any(
        "open a pull request" in notice for notice in delivered.delivery_notices
    )


def test_worktree_branch_pr_url_rides_typed_field_and_dict(
    tmp_path: Path, monkeypatch,
) -> None:
    """When a git-provider opens a PR, its ``pr_url`` lands on the typed decision
    field and to_dict projection — derived from the same ``PublishResult.pr_url``
    that shapes the human-readable 'PR opened' notice, never re-parsed from it."""
    from pipeline.engine.delivery_publish import PublishResult

    pr_url = "https://example.invalid/pr/42"
    monkeypatch.setattr(
        commit_delivery,
        "publish_delivery",
        lambda *a, **k: PublishResult(pushed=True, pr_url=pr_url),
    )

    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply_with_policy(
        repo=repo, worktree=worktree, run_dir=run_dir,
        branch_policy="worktree_branch",
    )

    assert delivered.status == "committed"
    # Typed twin carries the URL, and so does the serialized projection.
    assert delivered.pr_url == pr_url
    assert delivered.to_dict()["pr_url"] == pr_url
    # The human-readable 'PR opened' notice is preserved (not removed).
    assert any(
        notice == f"PR opened: {pr_url}"
        for notice in delivered.delivery_notices
    )


def test_protect_default_in_place_head_default_creates_delivery_branch(
    tmp_path: Path,
) -> None:
    """protect_default, in-place, HEAD on the default branch: the delivery must
    NOT land on the default branch — a fresh ``orcho/deliver/…`` is created and
    committed instead."""
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    # In-place: the run mutated the canonical checkout directly.
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply_with_policy(
        repo=repo, worktree=repo, run_dir=run_dir,
        branch_policy="protect_default",
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha
    # Invariant: the default branch never moved.
    assert _git_str(repo, "rev-parse", "main") == old_head
    # The commit landed on a fresh delivery branch, now checked out.
    assert _current_branch(repo).startswith("orcho/deliver/r1-")
    assert delivered.commit_sha == _head(repo)
    assert delivered.delivery_branch == _current_branch(repo)


def test_worktree_branch_in_place_degrades_and_protects_default(
    tmp_path: Path,
) -> None:
    """worktree_branch with an in-place run has no run branch to publish, so it
    degrades to protect_default — still guarding the default branch."""
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply_with_policy(
        repo=repo, worktree=repo, run_dir=run_dir,
        branch_policy="worktree_branch",
    )

    assert delivered.status == "committed"
    assert _git_str(repo, "rev-parse", "main") == old_head
    assert _current_branch(repo).startswith("orcho/deliver/r1-")


def test_in_place_feature_branch_commits_in_place(tmp_path: Path) -> None:
    """In-place on a NON-default branch: the operator ran on their own feature
    branch deliberately — the delivery commits onto it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "feature/x"], cwd=repo, check=True)
    feature_head = _head(repo)
    run_dir = tmp_path / "run"
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply_with_policy(
        repo=repo, worktree=repo, run_dir=run_dir,
        branch_policy="worktree_branch",
    )

    assert delivered.status == "committed"
    assert _current_branch(repo) == "feature/x"
    assert delivered.commit_sha and delivered.commit_sha != feature_head
    assert delivered.commit_sha == _head(repo)
    assert delivered.delivery_branch == "feature/x"


def test_bypass_commits_onto_default_branch(tmp_path: Path) -> None:
    """bypass is the explicit opt-out: it reproduces the prior behavior and
    commits onto the current HEAD, including the default branch."""
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply_with_policy(
        repo=repo, worktree=worktree, run_dir=run_dir,
        branch_policy="bypass",
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha and delivered.commit_sha != old_head
    # bypass committed straight onto main.
    assert _current_branch(repo) == "main"
    assert _head(repo) == delivered.commit_sha
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"
    # No branch record / PR-intent — byte-identical to the prior path.
    assert delivered.delivery_branch is None
    assert "delivery_branch" not in delivered.to_dict()


def test_named_commits_onto_named_branch(tmp_path: Path) -> None:
    """named commits onto the operator-supplied branch (created off the current
    tip when absent)."""
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply_with_policy(
        repo=repo, worktree=worktree, run_dir=run_dir,
        branch_policy="named", branch_name="release/v9",
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha
    assert _git_str(repo, "rev-parse", "main") == old_head
    assert _current_branch(repo) == "release/v9"
    assert delivered.delivery_branch == "release/v9"


def test_named_delivery_branch_roots_at_base_ref(
    tmp_path: Path,
) -> None:
    """A ``named`` / ``protect_default`` delivery branch is created off the run's
    ``base_ref`` (ADR 0119) and carries the run's owned change. The parent of the
    delivery commit is exactly ``base_ref``, so the branch / PR range starts at
    the run baseline rather than the local default branch or the current HEAD.

    (The ref-level "base_ref, not default, even for a bare SHA" invariant is
    pinned directly on the resolver in
    ``tests/unit/pipeline/engine/test_delivery_branch.py``; this is the
    integration-level check that the commit site threads ``base_ref`` through.)"""
    repo = tmp_path / "repo"
    base = _init_repo(repo)  # main @ A — the run's baseline
    run_dir = tmp_path / "run"
    # In-place run mutates the canonical checkout (an owned change over baseline).
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply_with_policy(
        repo=repo, worktree=repo, run_dir=run_dir,
        branch_policy="named", branch_name="release/v9",
        baseline_ref=base,
    )

    assert delivered.status == "committed"
    assert delivered.delivery_branch == "release/v9"
    assert _current_branch(repo) == "release/v9"
    # The delivery commit's parent is exactly base_ref: the branch starts at the
    # run baseline, and the default branch never moved.
    assert _git_str(repo, "rev-parse", "release/v9~1") == base
    assert _git_str(repo, "rev-parse", "main") == base
    tree = _git_str(repo, "ls-tree", "-r", "--name-only", "release/v9").split()
    assert "app.txt" in tree
    assert _git_str(repo, "show", "release/v9:app.txt") == "base\nrun"


# ── T2: content_language body-language + strategy-guard removal ──────────────


def _commit_prompt_decision(base: Path) -> CommitDeliveryDecision:
    """A minimal decision usable to render the llm_generate commit prompt."""
    return CommitDeliveryDecision(
        action="approve",
        status="pending",
        run_id="r1",
        decision_id="r1",
        project_path=base,
        source_path=base,
        baseline_ref="HEAD",
        release_summary="сводка релиза",
        patch_text="diff --git a/a.py b/a.py\n",
        changed_paths=("a.py",),
        decided_at="2026-06-04T00:00:00+00:00",
    )


def test_commit_message_directive_english_when_content_language_unset(
    tmp_path: Path,
) -> None:
    """operator=Russian but content_language unset → default English, so the
    outward commit-message directive is English (fail-safe for public repos)."""
    from core.infra.config import AppConfig

    cfg = AppConfig(
        phases={}, timeouts={}, session={}, codemap={}, hypothesis={},
        language={"task_language": "Russian"}, artifacts={}, pipeline={},
    )
    assert cfg.content_language == "English"
    prompt = commit_delivery.render_commit_message_prompt(
        _commit_prompt_decision(tmp_path),
        body_language=cfg.content_language,
    )
    assert "in English" in prompt
    assert "in Russian" not in prompt


def test_commit_message_directive_russian_when_content_language_ru(
    tmp_path: Path,
) -> None:
    """content_language=ru flips the outward directive to Russian."""
    from core.infra.config import AppConfig

    cfg = AppConfig(
        phases={}, timeouts={}, session={}, codemap={}, hypothesis={},
        language={"task_language": "Russian", "content_language": "ru"},
        artifacts={}, pipeline={},
    )
    assert cfg.content_language == "ru"
    prompt = commit_delivery.render_commit_message_prompt(
        _commit_prompt_decision(tmp_path),
        body_language=cfg.content_language,
    )
    assert "in ru" in prompt


def test_commit_message_generator_not_disabled_by_release_summary_strategy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The run.py generator closure no longer self-disables when
    ``default_strategy`` != 'llm_generate'. With an available agent it renders
    the prompt, invokes the agent, and returns the parsed message even under
    ``default_strategy='release_summary'`` — strategy is decided upstream by
    ``resolve_commit_delivery``, not the generator. It also uses
    ``content_language`` (default English) for the body-language directive
    while the operator language is Russian."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)

    class _CommitMessageAgent:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def invoke(self, prompt: str, cwd: str, **kwargs: object) -> str:
            self.calls.append({"prompt": prompt, "cwd": cwd, **kwargs})
            return (
                '{"subject": "fix(delivery): generated message", '
                '"body": "", "type": "fix", "scope": "delivery", '
                '"breaking": false}'
            )

    agent = _CommitMessageAgent()
    generated: list[str | None] = []

    def resolve_with_generator(**kwargs: object) -> CommitDeliveryDecision:
        decision = _commit_prompt_decision(worktree)
        decision = dataclasses.replace(decision, project_path=project_dir)
        generator = kwargs["commit_message_generator"]
        assert callable(generator)
        generated.append(generator(decision))
        return decision

    monkeypatch.setattr(
        commit_delivery, "resolve_commit_delivery", resolve_with_generator,
    )
    monkeypatch.setattr(
        commit_delivery,
        "apply_commit_delivery",
        lambda decision, **_kwargs: dataclasses.replace(
            decision,
            status="committed",
            final_message=generated[-1],
            commit_message_strategy="llm_generate",
        ),
    )
    # default_strategy='release_summary' is the pre-change self-disable trigger.
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "default_strategy": "release_summary"},
            task_language="Russian",
            content_language="English",
        ),
    )

    stub = SimpleNamespace(
        output_dir=run_dir,
        session={"status": "done"},
        project_path=project_dir,
        parent_run_id=None,
        project_alias=None,
        no_interactive=True,
        worktree_context=None,
        session_ts="r1",
        phase_config=SimpleNamespace(final_acceptance_agent=agent),
        _commit_delivery_baseline=lambda: "HEAD",
    )

    _PipelineRun._run_commit_delivery(stub, diff_cwd=worktree)

    # Generator returned a real message (NOT None) despite release_summary.
    assert generated == ["fix(delivery): generated message\n"]
    assert agent.calls, "agent must be invoked when a final_acceptance agent exists"
    # content_language (English) drove the directive, not the Russian operator lang.
    assert "in English" in str(agent.calls[0]["prompt"])


# ── T4: publish-outward forces content_language authorship ───────────────────


def test_publish_outward_forces_llm_authorship_over_release_summary(
    tmp_path: Path,
) -> None:
    """On a publish-outward path (publish!=off AND branch policy!=bypass) the
    outward commit message is authored via the generator even when
    ``default_strategy='release_summary'`` — the operator (Russian) summary must
    not become the public commit message."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    calls: list[object] = []

    def generator(decision: CommitDeliveryDecision) -> str:
        calls.append(decision)
        return "fix(delivery): english outward message\n"

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session("Русское операторское резюме"),
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
            "default_strategy": "release_summary",  # NOT llm_generate
            "publish": "auto",
            "branch_policy": "worktree_branch",  # publishable (!= bypass)
        },
        no_interactive=True,
        commit_message_generator=generator,
    )

    assert calls, "generator must be forced on the publish-outward path"
    assert decision.final_message == "fix(delivery): english outward message"
    assert decision.commit_message_strategy == "llm_generate"
    assert "Русское" not in (decision.final_message or "")


def test_non_publish_path_keeps_release_summary_strategy(tmp_path: Path) -> None:
    """The non-publishing paths (branch policy ``bypass`` OR ``publish=off``)
    keep the configured ``release_summary`` strategy verbatim — the generator is
    never force-invoked, so the local commit behaviour is unchanged."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    worktree = _new_worktree(repo, run_dir)
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    calls: list[object] = []

    def generator(decision: CommitDeliveryDecision) -> str:
        calls.append(decision)
        return "fix(delivery): english outward message\n"

    for non_publish in ({"branch_policy": "bypass"}, {"publish": "off"}):
        calls.clear()
        decision = resolve_commit_delivery(
            project_dir=repo,
            source_worktree=worktree,
            run_dir=run_dir,
            run_id="r1",
            session=_session("docs: keep summary default"),
            commit_config={
                "enabled": True,
                "auto_in_ci": "approve",
                "add_untracked": True,
                "default_strategy": "release_summary",
                **non_publish,
            },
            no_interactive=True,
            commit_message_generator=generator,
        )
        assert not calls, f"generator must NOT be forced for {non_publish}"
        assert decision.commit_message_strategy == "release_summary"
        assert decision.final_message == "docs: keep summary default"
