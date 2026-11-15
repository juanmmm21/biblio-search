from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_openalex import Recorder, _page
from test_tuning import CountingBackend
from test_web import example_data, make_client

from biblio_search.__main__ import main
from biblio_search.catalog import (
    DIALECTS,
    EXPORT_DIALECTS,
    SEARCHABLE,
    dialect_notes,
    export_notes,
)
from biblio_search.document_types import (
    DOCUMENT_TYPE_LABELS,
    SUPPORTED_TYPES,
    document_type_note,
    filters_type,
)
from biblio_search.eric_query import EricDialect
from biblio_search.evidence import evidence_ladder
from biblio_search.models import (
    Database,
    DocumentType,
    EvidenceLevel,
    ExportTarget,
    QuestionError,
    SearchResult,
    StrategyOptions,
)
from biblio_search.openalex import SEARCH_PARAM
from biblio_search.openalex_query import OpenAlexDialect, split_filter
from biblio_search.pipeline import SearchReport
from biblio_search.pubmed_query import PubMedDialect, apply_document_type
from biblio_search.question_file import (
    dump_question,
    load_question,
    parse_question_text,
)
from biblio_search.search_log import build_log, render_log
from biblio_search.selection import select_databases

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
HEALTH = EXAMPLES / "caregiver-burden.toml"
EDUCATION = EXAMPLES / "peer-tutoring-reading.toml"


def _options(document_type: DocumentType | None) -> StrategyOptions:
    return StrategyOptions(document_type=document_type)


def test_every_type_has_a_label_and_openalex_tells_them_all_apart() -> None:
    assert set(DOCUMENT_TYPE_LABELS) == set(DocumentType)
    assert SUPPORTED_TYPES[Database.OPENALEX.value] == frozenset(DocumentType)
    assert set(SUPPORTED_TYPES) == {database.value for database in Database}


@pytest.mark.parametrize(
    ("document_type", "suffix"),
    [
        (DocumentType.ARTICLE, " AND Journal Article[pt]"),
        (DocumentType.REVIEW, " AND Review[pt]"),
        (DocumentType.PREPRINT, " AND Preprint[pt]"),
    ],
)
def test_pubmed_wraps_the_equation_and_adds_the_publication_type(
    document_type: DocumentType, suffix: str
) -> None:
    question, _ = load_question(HEALTH)
    plain = PubMedDialect().render(question, StrategyOptions())
    assert PubMedDialect().render(question, _options(document_type)) == f"({plain}){suffix}"


@pytest.mark.parametrize("document_type", [None, DocumentType.THESIS, DocumentType.BOOK])
def test_pubmed_leaves_alone_the_types_it_cannot_tell_apart(
    document_type: DocumentType | None,
) -> None:
    assert apply_document_type("a[tiab] NOT b[tiab]", document_type) == "a[tiab] NOT b[tiab]"


def test_pubmed_combines_the_type_with_the_level_of_evidence() -> None:
    question, _ = load_question(HEALTH)
    plain = PubMedDialect().render(question, StrategyOptions())
    options = StrategyOptions(
        evidence_level=EvidenceLevel.SYSTEMATIC_REVIEWS, document_type=DocumentType.REVIEW
    )
    assert PubMedDialect().render(question, options) == (
        f"(({plain}) AND systematic[sb]) AND Review[pt]"
    )


@pytest.mark.parametrize(
    ("document_type", "suffix"),
    [
        (DocumentType.ARTICLE, ' AND publicationtype:"Journal Articles"'),
        (DocumentType.REVIEW, ' AND publicationtype:"Information Analyses"'),
        (DocumentType.THESIS, " AND publicationtype:Dissertations*"),
        (DocumentType.BOOK, " AND publicationtype:Books"),
    ],
)
def test_eric_filters_by_publication_type(document_type: DocumentType, suffix: str) -> None:
    question, _ = load_question(EDUCATION)
    plain = EricDialect().render(question, StrategyOptions())
    assert EricDialect().render(question, _options(document_type)) == f"({plain}){suffix}"


def test_eric_has_no_preprints_to_filter() -> None:
    question, _ = load_question(EDUCATION)
    plain = EricDialect().render(question, StrategyOptions())
    assert EricDialect().render(question, _options(DocumentType.PREPRINT)) == plain


@pytest.mark.parametrize(
    ("document_type", "type_filter"),
    [
        (DocumentType.ARTICLE, "type:article"),
        (DocumentType.REVIEW, "type:review"),
        (DocumentType.PREPRINT, "type:preprint"),
        (DocumentType.THESIS, "type:dissertation"),
        (DocumentType.BOOK, "type:book|book-chapter"),
    ],
)
def test_openalex_writes_the_type_filter_after_the_equation(
    document_type: DocumentType, type_filter: str
) -> None:
    question, _ = load_question(EDUCATION)
    plain = OpenAlexDialect().render(question, StrategyOptions())
    equation = OpenAlexDialect().render(question, _options(document_type))
    assert equation == f"{plain} FILTER {type_filter}"
    assert split_filter(equation) == (plain, type_filter)


@pytest.mark.parametrize(
    "equation",
    [
        "(caregiv*) AND (burden*)",
        "(caregiv* OR FILTER OR x) AND (burden*)",
        "(a) FILTER type:Review",
        "(a) FILTER doi:10.1/x",
    ],
)
def test_split_filter_only_takes_a_trailing_type_filter(equation: str) -> None:
    assert split_filter(equation) == (equation, None)


def test_openalex_client_sends_the_type_as_a_filter() -> None:
    recorder = Recorder(_ok_page())

    async def scenario() -> None:
        async with recorder.client() as client:
            result = await client.search("(caregiv*) FILTER type:book|book-chapter", retmax=0)
        assert result.count == 7

    asyncio.run(scenario())
    params = recorder.requests[0].url.params
    assert params[SEARCH_PARAM] == "(caregiv*)"
    assert params["filter"] == "type:book|book-chapter"


def test_openalex_client_sends_no_filter_without_a_type() -> None:
    recorder = Recorder(_ok_page())

    async def scenario() -> None:
        async with recorder.client() as client:
            await client.search("(caregiv*) AND (burden*)", retmax=0)

    asyncio.run(scenario())
    assert "filter" not in recorder.requests[0].url.params


def _ok_page() -> httpx.Response:
    return httpx.Response(200, json=_page(1, 0, count=7))


def test_arxiv_only_holds_preprints() -> None:
    question, _ = load_question(EDUCATION)
    dialect = DIALECTS[Database.ARXIV]
    plain = dialect.render(question, StrategyOptions())
    assert dialect.render(question, _options(DocumentType.REVIEW)) == plain
    assert filters_type(Database.ARXIV.value, DocumentType.PREPRINT)
    assert not any("preprint" in note for note in _notes(Database.ARXIV, DocumentType.PREPRINT))
    assert "arXiv only holds preprints: it cannot be limited to reviews" in _notes(
        Database.ARXIV, DocumentType.REVIEW
    )


def _notes(database: Database, document_type: DocumentType) -> tuple[str, ...]:
    question, _ = load_question(EDUCATION)
    return dialect_notes(question, DIALECTS[database], _options(document_type))


def test_databases_that_cannot_tell_a_type_apart_say_so() -> None:
    thesis = "PubMed cannot be limited to theses and dissertations: it gets every type of document"
    assert thesis in _notes(Database.PUBMED, DocumentType.THESIS)
    assert "ERIC cannot be limited to preprints: it gets every type of document" in _notes(
        Database.ERIC, DocumentType.PREPRINT
    )
    assert not any("cannot be limited" in n for n in _notes(Database.OPENALEX, DocumentType.BOOK))
    assert document_type_note("pubmed", "PubMed", None) is None


def test_exported_equations_leave_the_type_to_the_database() -> None:
    question, _ = load_question(HEALTH)
    options = _options(DocumentType.REVIEW)
    equation = EXPORT_DIALECTS[ExportTarget.SCOPUS].render(question, options)
    assert "DOCTYPE" not in equation
    assert "The Scopus equation has no document type: limit it to reviews in Scopus" in (
        export_notes(question, ExportTarget.SCOPUS, options)
    )
    assert not any(
        "document type" in note
        for note in export_notes(question, ExportTarget.SCOPUS, StrategyOptions())
    )


def test_the_evidence_ladder_ignores_the_document_type() -> None:
    question, _ = load_question(HEALTH)
    plain = PubMedDialect().render(question, StrategyOptions())
    backend = CountingBackend({plain: 2816})
    report = asyncio.run(evidence_ladder(question, _options(DocumentType.REVIEW), backend))
    assert report.equation == plain
    assert not any(equation.endswith(") AND Review[pt]") for equation, _ in backend.asked)


def test_the_type_is_read_and_written_in_the_question_file() -> None:
    question, options = load_question(EDUCATION)
    chosen = StrategyOptions(document_type=DocumentType.THESIS)
    text = dump_question(question, chosen)
    assert 'document_type = "thesis"' in text
    assert parse_question_text(text) == (question, chosen)
    assert options.document_type is None
    with pytest.raises(QuestionError, match=r"options\.document_type must be one of"):
        parse_question_text(text.replace('"thesis"', '"poster"'))


def test_the_log_names_the_document_type() -> None:
    question, _ = load_question(EDUCATION)
    selection = select_databases(question, SEARCHABLE, [Database.ERIC])
    report = SearchReport(
        equation="x", result=SearchResult(count=0, ids=(), query_translation=""), records=()
    )
    when = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

    def render(options: StrategyOptions) -> str:
        log = build_log(
            question,
            options,
            selection,
            [(Database.ERIC, report)],
            max_records=20,
            searched_at=when,
        )
        return render_log(log)

    assert "- Document type: theses and dissertations" in render(_options(DocumentType.THESIS))
    assert "- Document type: every type of document" in render(StrategyOptions())


def test_build_document_type_flag_overrides_the_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(EDUCATION), "--dialect", "eric", "--document-type", "book"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip().endswith(") AND publicationtype:Books")
    assert main(["build", str(EDUCATION), "--dialect", "arxiv", "--document-type", "book"]) == 0
    assert "arXiv only holds preprints" in capsys.readouterr().err


def test_preview_filters_and_warns_by_document_type() -> None:
    data = {
        **example_data("peer-tutoring-reading.toml"),
        "databases": ["eric", "openalex", "arxiv"],
        "options": {"document_type": "review"},
    }
    with make_client() as client:
        preview = client.post("/api/preview", json=data).json()
    eric, openalex, arxiv = preview["equations"]
    assert eric["equation"].endswith('AND publicationtype:"Information Analyses"')
    assert openalex["equation"].endswith(" FILTER type:review")
    assert any("only holds preprints" in note for note in arxiv["notes"])
