from __future__ import annotations

from pathlib import Path

from biblio_search.catalog import EXPORT_DIALECTS, export_notes, export_reasons
from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    ControlledScope,
    Discipline,
    ExportTarget,
    FreeTextField,
    QuestionFormat,
    StrategyOptions,
    StructuredQuestion,
    Term,
    Vocabulary,
)
from biblio_search.protocols import QueryDialect
from biblio_search.question_file import load_question
from biblio_search.subscription_query import (
    CinahlDialect,
    ScopusDialect,
    WebOfScienceDialect,
    phrase,
    render_cinahl_term,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _question(
    *conditions: Condition,
    exclusions: tuple[Condition, ...] = (),
    discipline: Discipline | None = None,
) -> StructuredQuestion:
    return StructuredQuestion(
        text="q",
        format=QuestionFormat.CONCEPTS,
        components=(Component(ComponentRole.CONCEPT, "concept", conditions),),
        exclusions=exclusions,
        discipline=discipline,
    )


def test_dialects_satisfy_the_protocol() -> None:
    dialects: list[QueryDialect] = [CinahlDialect(), ScopusDialect(), WebOfScienceDialect()]
    assert [d.name for d in dialects] == [t.value for t in ExportTarget]


def test_phrase_quotes_only_phrases_and_reserved_text() -> None:
    assert phrase(Term.free("caregiv*")) == "caregiv*"
    assert phrase(Term.free("family caregiv*")) == '"family caregiv*"'
    assert phrase(Term.free("aged, 80")) == '"aged, 80"'
    assert phrase(Term.free("or")) == '"or"'


def test_cinahl_heading_codes_go_in_front_and_plus_explodes() -> None:
    assert render_cinahl_term(Term.controlled("Caregivers")) == 'MH "Caregivers+"'
    assert render_cinahl_term(Term.controlled("Anxiety", ControlledScope.MAJOR)) == 'MM "Anxiety+"'
    no_explode = Term.controlled("Anxiety", ControlledScope.HEADING_NO_EXPLODE)
    assert render_cinahl_term(no_explode) == 'MH "Anxiety"'
    major = Term.controlled("Anxiety", ControlledScope.MAJOR_NO_EXPLODE)
    assert render_cinahl_term(major) == 'MM "Anxiety"'
    # Un descriptor con comas va igual entre comillas: así lo escribe CINAHL.
    assert render_cinahl_term(Term.controlled("Aged, 80 and over")) == 'MH "Aged, 80 and over+"'


def test_cinahl_free_text_searches_title_and_abstract() -> None:
    assert render_cinahl_term(Term.free("carer*")) == "TI carer* OR AB carer*"
    assert render_cinahl_term(Term.free("older people")) == 'TI "older people" OR AB "older people"'
    assert render_cinahl_term(Term.free("burden", FreeTextField.TITLE)) == "TI burden"


def test_cinahl_equation_of_the_del_pino_example() -> None:
    question, _ = load_question(EXAMPLES / "caregiver-burden.toml")
    equation = CinahlDialect().render(question, StrategyOptions())
    assert equation.startswith('(MM "Anxiety" OR MM "Depression+" OR TI anxiety OR AB anxiety')
    assert ' AND (MM "Caregivers+" OR TI caregiv* OR AB caregiv*' in equation
    assert '(MH "Aged+" OR' in equation
    assert equation.endswith("TI strain* OR AB strain*)")
    [note] = CinahlDialect().notes(question)
    assert "Anxiety, Depression, Caregivers, Aged" in note


def test_cinahl_searches_eric_descriptors_as_free_text() -> None:
    condition = Condition("c", (Term.controlled("Peer Teaching", vocabulary=Vocabulary.ERIC),))
    question = _question(condition)
    assert CinahlDialect().render(question, StrategyOptions()) == (
        '(TI "peer teaching" OR AB "peer teaching")'
    )
    assert CinahlDialect().notes(question) == ()


def test_repeat_as_free_also_reaches_cinahl() -> None:
    question = _question(Condition("c", (Term.controlled("Caregivers"),)))
    options = StrategyOptions(repeat_controlled_as_free=True)
    assert CinahlDialect().render(question, options) == (
        '(MH "Caregivers+" OR TI caregivers OR AB caregivers)'
    )


def test_scopus_groups_every_condition_in_one_field_call() -> None:
    question = _question(
        Condition("a", (Term.controlled("Caregivers"), Term.free("carer*"))),
        Condition("b", (Term.free("burden*"), Term.free("strain", FreeTextField.TITLE))),
        exclusions=(Condition("x", (Term.free("child*"),)),),
    )
    assert ScopusDialect().render(question, StrategyOptions()) == (
        "TITLE-ABS-KEY(caregivers OR carer*) AND "
        "(TITLE-ABS-KEY(burden*) OR TITLE(strain)) AND NOT TITLE-ABS-KEY(child*)"
    )
    [note] = ScopusDialect().notes(question)
    assert "TITLE-ABS-KEY also searches the keywords" in note


def test_web_of_science_uses_topic_and_title_codes() -> None:
    question = _question(
        Condition("a", (Term.free("peer tutor*"), Term.free("mentor*"))),
        Condition("b", (Term.free("reading", FreeTextField.TITLE),)),
        exclusions=(Condition("x", (Term.free("adult*"),)),),
    )
    assert WebOfScienceDialect().render(question, StrategyOptions()) == (
        'TS=("peer tutor*" OR mentor*) AND TI=(reading) NOT TS=(adult*)'
    )


def test_title_only_questions_need_no_keywords_note() -> None:
    question = _question(Condition("a", (Term.free("tutor*", FreeTextField.TITLE),)))
    assert WebOfScienceDialect().notes(question) == ()
    assert ScopusDialect().notes(question) == ()


def test_cinahl_is_only_offered_for_health_questions() -> None:
    health, _ = load_question(EXAMPLES / "caregiver-burden.toml")
    reasons = export_reasons(health)
    assert list(reasons) == [ExportTarget.CINAHL, ExportTarget.SCOPUS, ExportTarget.WEB_OF_SCIENCE]
    assert "the question uses MeSH headings" in reasons[ExportTarget.CINAHL]
    physics = _question(Condition("a", (Term.free("qubit*"),)), discipline=Discipline.PHYSICS)
    assert list(export_reasons(physics)) == [ExportTarget.SCOPUS, ExportTarget.WEB_OF_SCIENCE]


def test_export_notes_warn_that_not_medline_cannot_be_exported() -> None:
    question, _ = load_question(EXAMPLES / "caregiver-burden.toml")
    options = StrategyOptions(free_text_unindexed_only=True)
    for target in ExportTarget:
        assert "NOT MEDLINE[sb]" not in EXPORT_DIALECTS[target].render(question, options)
        notes = export_notes(question, target, options)
        assert any("no equivalent of NOT MEDLINE[sb]" in note for note in notes)
    scopus = export_notes(question, ExportTarget.SCOPUS, StrategyOptions())
    assert "Scopus does not index these headings" in scopus[0]
