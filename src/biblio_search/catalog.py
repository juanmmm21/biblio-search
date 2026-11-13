"""Databases and vocabularies biblio-search knows, shared by the CLI and the web interface."""

from __future__ import annotations

from biblio_search.arxiv_query import ArxivDialect
from biblio_search.models import Database, StructuredQuestion, Term, Vocabulary
from biblio_search.openalex_query import OpenAlexDialect
from biblio_search.protocols import QueryDialect
from biblio_search.pubmed_query import PubMedDialect, foreign_terms

DIALECTS: dict[Database, QueryDialect] = {
    Database.PUBMED: PubMedDialect(),
    Database.ARXIV: ArxivDialect(),
    Database.OPENALEX: OpenAlexDialect(),
}
# Bases con cliente: ERIC tiene su paso propio en el desarrollo.
SEARCHABLE = frozenset({Database.PUBMED, Database.ARXIV, Database.OPENALEX})
VOCABULARY_LABELS = {Vocabulary.MESH.value: "MeSH", Vocabulary.ERIC.value: "ERIC"}
DATABASE_LABELS = {
    Database.PUBMED: "PubMed",
    Database.ERIC: "ERIC",
    Database.ARXIV: "arXiv",
    Database.OPENALEX: "OpenAlex",
}
# Cómo se nombra el identificador de un registro en cada base («PMID 123», «OpenAlex W1»).
RECORD_ID_LABELS = {
    Database.PUBMED: "PMID",
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
    label = DATABASE_LABELS.get(Database(dialect.name), dialect.name)
    headings = ", ".join(heading_label(term) for term in foreign)
    return f"{label} does not index these headings, so they are searched as free text: {headings}"


def dialect_notes(question: StructuredQuestion, dialect: QueryDialect) -> tuple[str, ...]:
    """Everything the user should know about how ``dialect`` searches ``question``."""
    note = foreign_note(question, dialect)
    return ((note,) if note else ()) + dialect.notes(question)
