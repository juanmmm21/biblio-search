"""Databases and vocabularies biblio-search knows, shared by the CLI and the web interface."""

from __future__ import annotations

from biblio_search.models import Database, StructuredQuestion, Term, Vocabulary
from biblio_search.protocols import QueryDialect
from biblio_search.pubmed_query import PubMedDialect, foreign_terms

DIALECTS: dict[Database, QueryDialect] = {Database.PUBMED: PubMedDialect()}
# Bases con cliente: de momento solo se puede consultar PubMed.
SEARCHABLE = frozenset({Database.PUBMED})
VOCABULARY_LABELS = {Vocabulary.MESH.value: "MeSH", Vocabulary.ERIC.value: "ERIC"}
DATABASE_LABELS = {
    Database.PUBMED: "PubMed",
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
    label = DATABASE_LABELS.get(Database(dialect.name), dialect.name)
    headings = ", ".join(heading_label(term) for term in foreign)
    return f"{label} does not index these headings, so they are searched as free text: {headings}"
