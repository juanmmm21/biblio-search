"""Databases and vocabularies biblio-search knows, shared by the CLI and the web interface."""

from __future__ import annotations

from biblio_search.arxiv_query import ArxivDialect
from biblio_search.eric_query import EricDialect
from biblio_search.models import (
    Database,
    Discipline,
    ExportTarget,
    QuestionFormat,
    StrategyOptions,
    StructuredQuestion,
    Term,
    Vocabulary,
)
from biblio_search.openalex_query import OpenAlexDialect
from biblio_search.protocols import QueryDialect
from biblio_search.pubmed_query import PubMedDialect, foreign_terms
from biblio_search.subscription_query import (
    CinahlDialect,
    ScopusDialect,
    WebOfScienceDialect,
    unindexed_note,
)

DIALECTS: dict[Database, QueryDialect] = {
    Database.PUBMED: PubMedDialect(),
    Database.ERIC: EricDialect(),
    Database.ARXIV: ArxivDialect(),
    Database.OPENALEX: OpenAlexDialect(),
}
SEARCHABLE = frozenset(DIALECTS)
VOCABULARY_LABELS = {Vocabulary.MESH.value: "MeSH", Vocabulary.ERIC.value: "ERIC"}
DATABASE_LABELS = {
    Database.PUBMED: "PubMed",
    Database.ERIC: "ERIC",
    Database.ARXIV: "arXiv",
    Database.OPENALEX: "OpenAlex",
}
# Bases de suscripción: solo se escribe su ecuación, nunca se consultan.
EXPORT_DIALECTS: dict[ExportTarget, QueryDialect] = {
    ExportTarget.CINAHL: CinahlDialect(),
    ExportTarget.SCOPUS: ScopusDialect(),
    ExportTarget.WEB_OF_SCIENCE: WebOfScienceDialect(),
}
EXPORT_LABELS = {
    ExportTarget.CINAHL: "CINAHL",
    ExportTarget.SCOPUS: "Scopus",
    ExportTarget.WEB_OF_SCIENCE: "Web of Science",
}
_LABELS_BY_NAME: dict[str, str] = {
    **{database.value: label for database, label in DATABASE_LABELS.items()},
    **{target.value: label for target, label in EXPORT_LABELS.items()},
}
# Cómo se nombra el identificador de un registro en cada base («PMID 123», «OpenAlex W1»).
RECORD_ID_LABELS = {
    Database.PUBMED: "PMID",
    Database.ERIC: "ERIC",
    Database.ARXIV: "arXiv",
    Database.OPENALEX: "OpenAlex",
}


def heading_label(term: Term) -> str:
    """A heading with the thesaurus it comes from, e.g. ``Tutoring (ERIC)``."""
    vocabulary = term.vocabulary.value if term.vocabulary is not None else ""
    return f"{term.text} ({VOCABULARY_LABELS.get(vocabulary, vocabulary)})"


def foreign_note(question: StructuredQuestion, dialect: QueryDialect) -> str | None:
    """Warn that some headings are searched as free text because the database lacks them."""
    foreign = foreign_terms(question, dialect.vocabularies)
    if not foreign:
        return None
    label = _LABELS_BY_NAME.get(dialect.name, dialect.name)
    headings = ", ".join(heading_label(term) for term in foreign)
    return f"{label} does not index these headings, so they are searched as free text: {headings}"


def dialect_notes(question: StructuredQuestion, dialect: QueryDialect) -> tuple[str, ...]:
    """Everything the user should know about how ``dialect`` searches ``question``."""
    note = foreign_note(question, dialect)
    return ((note,) if note else ()) + dialect.notes(question)


def export_reasons(question: StructuredQuestion) -> dict[ExportTarget, tuple[str, ...]]:
    """Subscription databases worth pasting the equation into, with the reasons.

    CINAHL is nursing and allied health, so it is only offered for health questions; Scopus
    and Web of Science cover every discipline.
    """
    health: list[str] = []
    if any(term.vocabulary is Vocabulary.MESH for term in question.controlled_terms):
        health.append("the question uses MeSH headings")
    if question.format in (QuestionFormat.SPC, QuestionFormat.PICO):
        health.append(f"{question.format.value.upper()} is a health-science question format")
    if question.discipline is Discipline.HEALTH:
        health.append("the discipline is health")
    reasons: dict[ExportTarget, tuple[str, ...]] = {}
    if health:
        reasons[ExportTarget.CINAHL] = ("nursing and allied health database", *health)
    general = ("multidisciplinary citation database",)
    reasons[ExportTarget.SCOPUS] = general
    reasons[ExportTarget.WEB_OF_SCIENCE] = general
    return reasons


def export_notes(
    question: StructuredQuestion, target: ExportTarget, options: StrategyOptions
) -> tuple[str, ...]:
    """Everything the user should know before pasting the equation into ``target``."""
    dialect = EXPORT_DIALECTS[target]
    return dialect_notes(question, dialect) + unindexed_note(EXPORT_LABELS[target], options)
