from __future__ import annotations

from pathlib import Path

import pytest

from biblio_search.__main__ import main

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "caregiver-burden.toml"


def test_build_prints_equation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("(Anxiety[mj:noexp] OR")
    assert "NOT MEDLINE[sb]" not in out


def test_build_flags_enable_options(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(EXAMPLE), "--unindexed-only", "--repeat-controlled-as-free"]) == 0
    out = capsys.readouterr().out
    assert "NOT MEDLINE[sb]" in out
    assert "aged[tiab]" in out


def test_build_reports_errors_on_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(tmp_path / "missing.toml")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot read" in captured.err
