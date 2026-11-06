from __future__ import annotations

from pathlib import Path

import pytest

from biblio_search.models import (
    ComponentRole,
    ControlledScope,
    FreeTextField,
    QuestionError,
    QuestionFormat,
    Term,
)
from biblio_search.question_file import load_question, parse_question

MINIMAL: dict[str, object] = {
    "question": "Does exercise reduce falls in older adults?",
    "format": "pico",
    "components": [
        {
            "role": "population",
            "description": "older adults",
            "conditions": [
                {
                    "label": "older adults",
                    "controlled": [{"term": "Aged", "scope": "heading_no_explode"}],
                    "free": ["elder*", {"term": "older adult*", "field": "title"}],
                }
            ],
        },
        {"role": "comparison", "description": "usual care"},
    ],
    "exclusions": [{"label": "animals", "controlled": ["Animals"]}],
    "options": {"repeat_controlled_as_free": True},
}


def test_parse_full_question() -> None:
    question, options = parse_question(MINIMAL)
    assert question.format is QuestionFormat.PICO
    assert [c.role for c in question.components] == [
        ComponentRole.POPULATION,
        ComponentRole.COMPARISON,
    ]
    assert question.conditions[0].terms == (
        Term.controlled("Aged", ControlledScope.HEADING_NO_EXPLODE),
        Term.free("elder*"),
        Term.free("older adult*", FreeTextField.TITLE),
    )
    assert question.exclusions[0].terms == (Term.controlled("Animals"),)
    assert options.repeat_controlled_as_free
    assert not options.free_text_unindexed_only


def _with(**changes: object) -> dict[str, object]:
    return {**MINIMAL, **changes}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_with(format="peco"), "format must be one of: spc, pico"),
        (_with(question=3), "question must be a string"),
        (_with(components="population"), "components must be a list"),
        (_with(options={"verbose": True}), "unknown options: verbose"),
        (_with(options={"free_text_unindexed_only": "yes"}), "must be true or false"),
        (
            _with(components=[{"role": "population", "description": "x", "conditions": [1]}]),
            r"components\[0\].conditions\[0\] must be a table",
        ),
        (
            _with(
                components=[
                    {
                        "role": "population",
                        "description": "x",
                        "conditions": [{"label": "x", "controlled": [{"term": "A", "scope": "x"}]}],
                    }
                ]
            ),
            r"components\[0\].conditions\[0\].controlled\[0\].scope must be one of",
        ),
    ],
)
def test_parse_errors_point_to_the_offending_field(data: dict[str, object], message: str) -> None:
    with pytest.raises(QuestionError, match=message):
        parse_question(data)


def test_load_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(QuestionError, match="cannot read"):
        load_question(tmp_path / "missing.toml")


def test_load_reports_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("question = ", encoding="utf-8")
    with pytest.raises(QuestionError, match="not valid TOML"):
        load_question(path)
