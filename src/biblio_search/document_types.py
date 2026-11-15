"""Document types in every discipline: which databases can limit a search to each kind.

Health has Haynes' pyramid (:mod:`biblio_search.evidence`); other disciplines start from the
kind of document instead: reviews to get an overview of a field, journal articles for
published research, preprints for the newest work, theses and books for depth. Each database
names those kinds its own way, and some cannot tell them apart: they get every record and
the user is told so.
"""

from __future__ import annotations

from biblio_search.eric_query import DOCUMENT_TYPE_FILTERS as ERIC_FILTERS
from biblio_search.models import Database, DocumentType
from biblio_search.openalex_query import DOCUMENT_TYPE_FILTERS as OPENALEX_FILTERS
from biblio_search.pubmed_query import DOCUMENT_TYPE_FILTERS as PUBMED_FILTERS

DOCUMENT_TYPE_LABELS: dict[DocumentType, str] = {
    DocumentType.ARTICLE: "Journal articles",
    DocumentType.REVIEW: "Reviews",
    DocumentType.PREPRINT: "Preprints",
    DocumentType.THESIS: "Theses and dissertations",
    DocumentType.BOOK: "Books and chapters",
}

# arXiv no necesita filtro: todo lo que guarda es un preprint (aunque luego se publique).
SUPPORTED_TYPES: dict[str, frozenset[DocumentType]] = {
    Database.PUBMED.value: frozenset(PUBMED_FILTERS),
    Database.OPENALEX.value: frozenset(OPENALEX_FILTERS),
    Database.ERIC.value: frozenset(ERIC_FILTERS),
    Database.ARXIV.value: frozenset({DocumentType.PREPRINT}),
}


def filters_type(database: str, document_type: DocumentType) -> bool:
    """Whether ``database`` returns only documents of ``document_type`` when asked to."""
    return document_type in SUPPORTED_TYPES.get(database, frozenset())


def document_type_note(database: str, label: str, document_type: DocumentType | None) -> str | None:
    """Warn that ``database`` (shown as ``label``) cannot limit the search to the type chosen."""
    if document_type is None or filters_type(database, document_type):
        return None
    kind = DOCUMENT_TYPE_LABELS[document_type].lower()
    if database == Database.ARXIV.value:
        return f"arXiv only holds preprints: it cannot be limited to {kind}"
    return f"{label} cannot be limited to {kind}: it gets every type of document"
