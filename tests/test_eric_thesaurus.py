from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest
from conftest import FIXTURES, eric_fixture_for

from biblio_search.check import apply_check, check_question, scope_note
from biblio_search.eric import RECORD_FIELDS, EricPage, parse_page
from biblio_search.eric_thesaurus import (
    MIN_RECORDS,
    EricApiThesaurus,
    EricFileThesaurus,
    EricThesaurusFile,
    ThesaurusFileError,
    describe_source,
)
from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    QuestionFormat,
    StructuredQuestion,
    Term,
    TermStatus,
    ThesaurusEntry,
    Vocabulary,
)
from biblio_search.protocols import Thesaurus

EXCERPT = FIXTURES / "eric_thesaurus_excerpt.xml"


class RecordedEric:
    """Answers ERIC queries with the recorded fixtures and keeps what was asked."""

    def __init__(self) -> None:
        self.searches: list[tuple[str, int, str]] = []

    async def query(
        self, search: str, *, rows: int, start: int = 0, fields: str = RECORD_FIELDS
    ) -> EricPage:
        self.searches.append((search, rows, fields))
        return parse_page((FIXTURES / eric_fixture_for(search)).read_bytes())


def _lookup(thesaurus: Thesaurus, *texts: str) -> dict[str, ThesaurusEntry | None]:
    return dict(asyncio.run(thesaurus.lookup(list(texts))))


def test_api_thesaurus_finds_the_exact_form_and_checks_its_use() -> None:
    backend = RecordedEric()
    thesaurus: Thesaurus = EricApiThesaurus(backend)
    found = _lookup(thesaurus, "peer teaching", "Peer  Teaching", "Qwzx Learning")
    entry = ThesaurusEntry(vocabulary="eric", identifier="", name="Peer Teaching")
    assert found == {"peer teaching": entry, "Peer  Teaching": entry, "Qwzx Learning": None}
    # Una búsqueda por texto distinto (sin contar mayúsculas ni espacios) más su recuento.
    assert backend.searches == [
        ('subject:"peer teaching"', 50, "subject"),
        ('subject_ss:"Peer Teaching"', 0, RECORD_FIELDS),
        ('subject:"Qwzx Learning"', 50, "subject"),
    ]


def test_api_thesaurus_ignores_headings_on_too_few_records() -> None:
    # «Peer Tutoring» es un sinónimo que se coló en los descriptores de un solo registro.
    assert MIN_RECORDS > 1
    assert _lookup(EricApiThesaurus(RecordedEric()), "Peer Tutoring") == {"Peer Tutoring": None}


@pytest.fixture(scope="module")
def excerpt() -> EricThesaurusFile:
    return EricThesaurusFile.load(EXCERPT)


def test_file_finds_descriptors_with_their_synonyms(excerpt: EricThesaurusFile) -> None:
    entry = excerpt.entry("tutoring")
    assert entry is not None
    assert (entry.vocabulary, entry.identifier, entry.name) == ("eric", "", "Tutoring")
    # Los años de los descriptores antiguos se quitan: nadie los escribe en un título.
    assert entry.synonyms == ("After School Tutoring", "Peer Tutoring", "Tutorial Instruction")
    assert entry.scope_note.startswith("Instruction provided to a learner")
    assert entry.has_narrower is False and entry.use_with == ()


def test_file_follows_synonyms_to_every_descriptor(excerpt: EricThesaurusFile) -> None:
    entry = excerpt.entry("Peer Tutoring")
    assert entry is not None
    assert entry.name == "Peer Teaching" and entry.use_with == ("Tutoring",)


def test_file_leaves_out_dead_and_unknown_terms(excerpt: EricThesaurusFile) -> None:
    assert excerpt.entry("American Culture (1966 1980)") is None
    assert excerpt.entry("Qwzx Learning") is None
    assert len(excerpt) == 7


def test_file_reads_the_zip_as_published(tmp_path: Path) -> None:
    archive = tmp_path / "ERICThesaurus2025.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.write(EXCERPT, "ERICThesaurus2025.xml")
    assert EricThesaurusFile.load(archive).entry("Peer Teaching") is not None


def test_file_errors_name_the_problem(tmp_path: Path) -> None:
    with pytest.raises(ThesaurusFileError, match="cannot read"):
        EricThesaurusFile.load(tmp_path / "missing.xml")
    other = tmp_path / "other.xml"
    other.write_text("<root/>", encoding="utf-8")
    with pytest.raises(ThesaurusFileError, match="has no <Terms>"):
        EricThesaurusFile.load(other)
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as handle:
        handle.writestr("readme.txt", "x")
    with pytest.raises(ThesaurusFileError, match="holds no XML file"):
        EricThesaurusFile.load(empty_zip)


def test_file_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert EricThesaurusFile.from_env() is None
    monkeypatch.setenv("ERIC_THESAURUS", str(EXCERPT))
    thesaurus = EricThesaurusFile.from_env()
    assert thesaurus is not None and len(thesaurus) == 7
    assert describe_source(thesaurus).endswith("ERIC Thesaurus file (7 terms)")
    assert "set ERIC_THESAURUS" in describe_source(None)


def test_check_with_the_file_renames_synonyms_and_suggests_free_text(
    excerpt: EricThesaurusFile,
) -> None:
    question = StructuredQuestion(
        text="q",
        format=QuestionFormat.CONCEPTS,
        components=(
            Component(
                ComponentRole.CONCEPT,
                "peer tutoring",
                (
                    Condition(
                        "peer tutoring",
                        (
                            Term.controlled("Peer Tutoring", vocabulary=Vocabulary.ERIC),
                            Term.controlled("Anxiety"),
                            Term.free("peer tutor*"),
                        ),
                    ),
                ),
            ),
        ),
    )
    result = asyncio.run(check_question(question, EricFileThesaurus(excerpt)))
    (check,) = result.checks
    assert check.status is TermStatus.ENTRY_TERM
    assert scope_note(check) == (
        "the thesaurus pairs this term with Tutoring too; add it if it fits the question"
    )
    # «Peer Tutoring» ya lo cubre «peer tutor*»: solo se sugiere lo que falta.
    assert result.conditions[0].suggested_synonyms == ()
    assert [t.text for t in result.unchecked] == ["Anxiety"]
    fixed = apply_check(question, result)
    assert fixed.conditions[0].terms[0] == Term.controlled(
        "Peer Teaching", vocabulary=Vocabulary.ERIC
    )


def test_recorded_fixtures_are_valid_json() -> None:
    for path in FIXTURES.glob("eric_*.json"):
        json.loads(path.read_text(encoding="utf-8"))
