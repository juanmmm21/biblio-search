from __future__ import annotations

from pathlib import Path

import pytest

from biblio_search.models import (
    ComponentRole,
    ControlledScope,
    Database,
    Discipline,
    FreeTextField,
    QuestionError,
    QuestionFormat,
    Term,
    Vocabulary,
)
from biblio_search.question_file import (
    dump_question,
    load_question,
    parse_question,
    parse_question_text,
    question_to_data,
)

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
        (_with(format="peco"), "format must be one of: spc, pico, concepts"),
        (_with(discipline="biology"), "discipline must be one of: health, education"),
        (_with(databases="pubmed"), "databases must be a list"),
        (_with(databases=["scopus"]), r"databases\[0\] must be one of: pubmed, eric"),
        (_with(databases=["eric", "eric"]), "database can be chosen only once"),
        (
            _with(
                components=[
                    {
                        "role": "population",
                        "description": "x",
                        "conditions": [
                            {"label": "x", "controlled": [{"term": "A", "vocabulary": "decs"}]}
                        ],
                    }
                ]
            ),
            r"controlled\[0\].vocabulary must be one of: mesh, eric",
        ),
        (
            _with(components=[{"description": "x", "conditions": []}]),
            r"components\[0\].role must be a string",
        ),
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


def test_parse_concepts_with_vocabularies_discipline_and_databases() -> None:
    data: dict[str, object] = {
        "question": "Does peer tutoring improve reading?",
        "format": "concepts",
        "discipline": "education",
        "databases": ["eric", "openalex"],
        "components": [
            {
                "description": "peer tutoring",
                "conditions": [
                    {
                        "label": "tutoring",
                        "controlled": [{"term": "Peer Teaching", "vocabulary": "eric"}, "Tutoring"],
                        "free": ["peer tutor*"],
                    }
                ],
            },
            {"role": "concept", "description": "reading", "conditions": []},
        ],
    }
    question, _ = parse_question(data)
    assert question.format is QuestionFormat.CONCEPTS
    assert [c.role for c in question.components] == [ComponentRole.CONCEPT] * 2
    assert question.discipline is Discipline.EDUCATION
    assert question.databases == (Database.ERIC, Database.OPENALEX)
    assert question.conditions[0].controlled_terms == (
        Term.controlled("Peer Teaching", vocabulary=Vocabulary.ERIC),
        Term.controlled("Tutoring"),
    )


def test_files_without_new_fields_keep_their_meaning() -> None:
    question, _ = parse_question(MINIMAL)
    assert question.discipline is None
    assert question.databases == ()
    assert question.conditions[0].terms[0].vocabulary is Vocabulary.MESH


def test_load_education_example() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "peer-tutoring-reading.toml"
    question, _ = load_question(example)
    assert [c.label for c in question.conditions] == [
        "peer tutoring",
        "reading comprehension",
        "primary school",
    ]
    assert {t.vocabulary for t in question.controlled_terms} == {Vocabulary.ERIC}


def test_load_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(QuestionError, match="cannot read"):
        load_question(tmp_path / "missing.toml")


def test_load_reports_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("question = ", encoding="utf-8")
    with pytest.raises(QuestionError, match="not valid TOML"):
        load_question(path)


EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.mark.parametrize("name", ["caregiver-burden.toml", "peer-tutoring-reading.toml"])
def test_dump_question_round_trips_the_examples(name: str) -> None:
    question, options = load_question(EXAMPLES / name)
    assert parse_question_text(dump_question(question, options)) == (question, options)


def test_dump_question_round_trips_every_option() -> None:
    question, options = parse_question(MINIMAL)
    text = dump_question(question, options)
    assert parse_question_text(text) == (question, options)
    assert "[options]\nrepeat_controlled_as_free = true\n" in text
    assert '{ term = "older adult*", field = "title" }' in text


def test_question_to_data_omits_defaults() -> None:
    question, options = parse_question(MINIMAL)
    data = question_to_data(question, options)
    population = data["components"]
    assert isinstance(population, list)
    condition = population[0]["conditions"][0]
    assert condition["controlled"] == [{"term": "Aged", "scope": "heading_no_explode"}]
    assert condition["free"][0] == "elder*"
    assert population[1] == {"role": "comparison", "description": "usual care"}
    assert data["exclusions"] == [{"label": "animals", "controlled": ["Animals"]}]
    assert "discipline" not in data
    assert "databases" not in data


def test_question_to_data_keeps_discipline_databases_and_vocabularies() -> None:
    question, options = load_question(EXAMPLES / "peer-tutoring-reading.toml")
    data = question_to_data(question, options)
    assert data["discipline"] == "education"
    assert "options" not in data
    components = data["components"]
    assert isinstance(components, list)
    assert "role" not in components[0]
    assert components[0]["conditions"][0]["controlled"] == [
        {"term": "Peer Teaching", "vocabulary": "eric"}
    ]


def test_dump_question_escapes_strings() -> None:
    data = dict(MINIMAL, question='Is "exercise"\tbetter\\worse?\nReally\x01')
    question, options = parse_question(data)
    text = dump_question(question, options)
    assert parse_question_text(text)[0].text == question.text
    assert '\\"exercise\\"\\tbetter\\\\worse?\\nReally\\u0001' in text


def test_parse_question_text_reports_bad_toml() -> None:
    with pytest.raises(QuestionError, match="not valid TOML"):
        parse_question_text("question = ")
