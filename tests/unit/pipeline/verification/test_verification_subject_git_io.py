"""Locale-independent git IO in the verification subject probes.

git emits raw UTF-8 pathnames (NUL-delimited output is never quoted), so the
decode must be pinned to UTF-8 rather than the process locale: on a non-UTF-8
Windows codepage the locale decode dies on any non-ASCII path, killing the
capture thread and leaving ``stdout`` as ``None``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pipeline.verification_subject as vs


class TestRunGitDecoding:
    def test_pins_utf8_decode_with_replacement(self, monkeypatch, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(vs.subprocess, "run", fake_run)
        result = vs._run_git(tmp_path, ("status",))
        assert result is not None and result.stdout == "ok\n"
        assert seen["encoding"] == "utf-8"
        assert seen["errors"] == "replace"
        assert "text" not in seen

    def test_none_stdout_is_a_failed_probe(self, monkeypatch, tmp_path: Path) -> None:
        # A dead capture thread leaves stdout as None on the completed
        # process; that must read as "probe failed", never reach callers
        # that split/strip the payload.
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=None, stderr=None)

        monkeypatch.setattr(vs.subprocess, "run", fake_run)
        assert vs._run_git(tmp_path, ("ls-files", "-s", "-z")) is None

    def test_dirty_submodule_probe_fails_closed_on_dead_capture(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=None, stderr=None)

        monkeypatch.setattr(vs.subprocess, "run", fake_run)
        assert vs._has_dirty_submodule(tmp_path) is True

    def test_non_ascii_paths_survive_the_probe(self, tmp_path: Path) -> None:
        # End-to-end on a real repo with a Cyrillic pathname: the capture
        # must produce an identity, not raise.
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True,
        )
        (tmp_path / "документ.txt").write_text("привет", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True,
        )
        capture = vs.capture_verification_subject(tmp_path)
        assert isinstance(capture, vs.VerificationSubjectAvailable)
        assert capture.identity.tree_oid
