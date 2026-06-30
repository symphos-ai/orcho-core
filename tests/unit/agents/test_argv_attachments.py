"""argv builder integration.

Verifies that ``build_orch_argv`` emits one ``--attach*`` flag pair per
listed path and that omitting them leaves the argv untouched.
"""
from pipeline.argv import build_orch_argv


def test_no_attachments_omits_flag() -> None:
    argv = build_orch_argv(project="/p", task="t")
    assert "--attach" not in argv


def test_single_attach_emitted() -> None:
    argv = build_orch_argv(project="/p", task="t", attach=["spec.md"])
    assert argv.count("--attach") == 1
    assert "spec.md" in argv


def test_multiple_attach_each_one_pair() -> None:
    argv = build_orch_argv(
        project="/p", task="t",
        attach=["a.md", "b.md", "c.md"],
    )
    assert argv.count("--attach") == 3
    for name in ("a.md", "b.md", "c.md"):
        assert name in argv


def test_typed_flags_preserved() -> None:
    argv = build_orch_argv(
        project="/p", task="t",
        attach=["doc.md"],
        attach_text=["forced.bin"],
        attach_image=["m.png"],
        attach_binary=["data.dat"],
    )
    assert "--attach" in argv
    assert "--attach-text" in argv
    assert "--attach-image" in argv
    assert "--attach-binary" in argv
    assert "doc.md" in argv
    assert "forced.bin" in argv
    assert "m.png" in argv
    assert "data.dat" in argv
