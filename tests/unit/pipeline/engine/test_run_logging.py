from __future__ import annotations

from pathlib import Path

from pipeline.engine.run_logging import setup_run_logging


def test_setup_materializes_advertised_output_log(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()

    result = setup_run_logging(output, "run", terminal=False)

    assert result == output / "output.log"
    assert result.is_file()


def test_resume_preserves_existing_output_log(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    log = output / "output.log"
    log.write_text("existing transcript\n", encoding="utf-8")

    setup_run_logging(output, "run", is_resume=True, terminal=False)

    assert log.read_text(encoding="utf-8") == "existing transcript\n"
