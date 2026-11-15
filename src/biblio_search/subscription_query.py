"""Search equations for subscription databases: CINAHL, Scopus and Web of Science.

biblio-search never queries these databases (they need an institutional licence): it only
writes the equation so the user can paste it there, adapted from the same structured
question as the free ones (Del Pino's single equation adapted to every database).

- CINAHL (EBSCOhost): the field code goes before the term, separated by a space (Del Pino,
  2017). Headings are searched with ``MH`` or ``MM`` (major); a trailing ``+`` explodes them.
  CINAHL Subject Headings derive from MeSH, so MeSH headings are kept as headings.
  https://connect.ebsco.com/s/article/Searching-with-Field-Codes
- Scopus: ``TITLE-ABS-KEY`` (title, abstract and keywords, which include the index terms of
  MEDLINE records) and ``TITLE``; ``AND NOT`` excludes.
  https://schema.elsevier.com/dtds/document/bkapi/search/SCOPUSSearchTips.htm
- Web of Science Core Collection: ``TS=`` (topic: title, abstract and keywords) and ``TI=``.
  https://webofscience.help.clarivate.com/en-us/Content/search-operators.html

The three take ``*`` at the end of a word and inside quoted phrases, so every term keeps the
truncation it was written with.

Levels of evidence and document types become each database's own limit when it has one:
CINAHL's publication type field (``PT``), Scopus' ``DOCTYPE()`` and Web of Science's ``DT=``.
Only types those databases actually index are used; the rest are left to a note.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from biblio_search.models import (
    Condition,
    ControlledScope,
    DocumentType,
    EvidenceLevel,
    FreeTextField,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermKind,
    Vocabulary,
)
from biblio_search.pubmed_query import UNINDEXED_FILTER, expand_condition, localize_condition

CINAHL_TAGS: dict[ControlledScope, tuple[str, bool]] = {
    ControlledScope.HEADING: ("MH", True),
    ControlledScope.MAJOR: ("MM", True),
    ControlledScope.HEADING_NO_EXPLODE: ("MH", False),
    ControlledScope.MAJOR_NO_EXPLODE: ("MM", False),
}
EXPLODE = "+"

# Tipos de publicación de CINAHL (campo PT). «Research» marca los artículos de investigación
# original, lo más cercano a quitar guías y revisiones que hace el filtro de PubMed.
CINAHL_EVIDENCE_FILTERS: dict[EvidenceLevel, str] = {
    EvidenceLevel.GUIDELINES: 'PT "Practice Guidelines"',
    EvidenceLevel.SYSTEMATIC_REVIEWS: 'PT "Systematic Review"',
    EvidenceLevel.ORIGINAL_STUDIES: 'PT "Research"',
}
CINAHL_TYPE_FILTERS: dict[DocumentType, str] = {
    DocumentType.ARTICLE: 'PT "Journal Article"',
    DocumentType.REVIEW: 'PT "Review"',
    DocumentType.THESIS: '(PT "Doctoral Dissertation" OR PT "Masters Thesis")',
    DocumentType.BOOK: '(PT "Book" OR PT "Book Chapter")',
}
# Scopus y Web of Science no indexan tesis ni preprints en sus colecciones principales.
SCOPUS_TYPE_FILTERS: dict[DocumentType, str] = {
    DocumentType.ARTICLE: "DOCTYPE(ar)",
    DocumentType.REVIEW: "DOCTYPE(re)",
    DocumentType.BOOK: "(DOCTYPE(bk) OR DOCTYPE(ch))",
}
WOS_TYPE_FILTERS: dict[DocumentType, str] = {
    DocumentType.ARTICLE: "DT=(Article)",
    DocumentType.REVIEW: "DT=(Review)",
    DocumentType.BOOK: "(DT=(Book) OR DT=(Book Chapter))",
}


def apply_limits(
    equation: str,
    options: StrategyOptions,
    evidence_filters: dict[EvidenceLevel, str],
    type_filters: dict[DocumentType, str],
) -> str:
    """AND the database's own limits for the level of evidence and document type chosen.

    The equation is wrapped first so that a trailing NOT of the question cannot swallow the
    limit. A level or type the database cannot tell apart is left out (a note says so).
    """
    limits: list[str] = []
    if options.evidence_level is not None and options.evidence_level in evidence_filters:
        limits.append(evidence_filters[options.evidence_level])
    if options.document_type is not None and options.document_type in type_filters:
        limits.append(type_filters[options.document_type])
    if not limits:
        return equation
    return f"({equation}) AND " + " AND ".join(limits)


def phrase(term: Term) -> str:
    """The text of a free term, quoted when it has several words or reserved characters.

    Unquoted words next to each other are read as a phrase or as ANDed words depending on the
    database and its search mode, so a phrase is always quoted to mean the same everywhere.
    """
    if term.needs_quotes or " " in term.text:
        return f'"{term.text}"'
    return term.text


def render_cinahl_term(term: Term) -> str:
    """Render one term with its CINAHL field code in front."""
    if term.kind is TermKind.CONTROLLED:
        tag, explode = CINAHL_TAGS[term.scope]
        return f'{tag} "{term.text}{EXPLODE if explode else ""}"'
    text = phrase(term)
    if term.free_field is FreeTextField.TITLE:
        return f"TI {text}"
    # Del Pino usa solo AB; TI OR AB equivale a [tiab] y no pierde los títulos sin resumen.
    return f"TI {text} OR AB {text}"


def _fielded_group(condition: Condition, fields: dict[FreeTextField, str]) -> str:
    """OR the free terms of a condition inside one field call per field, e.g. ``TS=(a OR b)``."""
    calls: list[str] = []
    for free_field, code in fields.items():
        texts = [phrase(t) for t in condition.terms if t.free_field is free_field]
        if texts:
            calls.append(f"{code}(" + " OR ".join(texts) + ")")
    return calls[0] if len(calls) == 1 else "(" + " OR ".join(calls) + ")"


def render_equation(
    question: StructuredQuestion,
    options: StrategyOptions,
    vocabularies: frozenset[Vocabulary],
    group: Callable[[Condition], str],
    not_operator: str,
) -> str:
    """Join the conditions with AND and append each exclusion with ``not_operator``."""

    def local(condition: Condition, condition_options: StrategyOptions) -> Condition:
        return expand_condition(localize_condition(condition, vocabularies), condition_options)

    equation = " AND ".join(group(local(c, options)) for c in question.conditions)
    for exclusion in question.exclusions:
        equation += f" {not_operator} {group(local(exclusion, StrategyOptions()))}"
    return equation


def unindexed_note(label: str, options: StrategyOptions) -> tuple[str, ...]:
    """Warn that the NOT MEDLINE[sb] option cannot be carried over to ``label``."""
    if not options.free_text_unindexed_only:
        return ()
    return (
        f"{label} has no equivalent of NOT {UNINDEXED_FILTER}, so free text is searched in "
        "every record",
    )


def _keywords_note(question: StructuredQuestion, label: str, code: str) -> tuple[str, ...]:
    # Ninguna de las dos tiene un campo de solo título y resumen: el más cercano a [tiab]
    # también mira las palabras clave, y conviene saberlo al comparar recuentos.
    has_tiab = any(
        t.kind is TermKind.CONTROLLED or t.free_field is FreeTextField.TITLE_ABSTRACT
        for c in question.conditions + question.exclusions
        for t in c.terms
    )
    if not has_tiab:
        return ()
    return (f"{label} has no title-and-abstract field: {code} also searches the keywords",)


class CinahlDialect:
    """Equation for CINAHL on EBSCOhost, with MeSH headings as CINAHL Subject Headings."""

    name = "cinahl"
    vocabularies = frozenset({Vocabulary.MESH})

    @staticmethod
    def _group(condition: Condition) -> str:
        return "(" + " OR ".join(render_cinahl_term(t) for t in condition.terms) + ")"

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        equation = render_equation(question, options, self.vocabularies, self._group, "NOT")
        return apply_limits(equation, options, CINAHL_EVIDENCE_FILTERS, CINAHL_TYPE_FILTERS)

    def notes(self, question: StructuredQuestion) -> tuple[str, ...]:
        headings = [t.text for t in question.controlled_terms if t.vocabulary is Vocabulary.MESH]
        if not headings:
            return ()
        return (
            "CINAHL Subject Headings derive from MeSH but not all of them match: look these "
            "up in CINAHL Headings before running the search: " + ", ".join(headings),
        )


class ScopusDialect:
    """Equation for Scopus advanced search."""

    name = "scopus"
    vocabularies: frozenset[Vocabulary] = frozenset()
    fields: ClassVar[dict[FreeTextField, str]] = {
        FreeTextField.TITLE_ABSTRACT: "TITLE-ABS-KEY",
        FreeTextField.TITLE: "TITLE",
    }

    def _group(self, condition: Condition) -> str:
        return _fielded_group(condition, self.fields)

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        # En Scopus, NOT a secas no existe como operador binario: se escribe AND NOT.
        equation = render_equation(question, options, self.vocabularies, self._group, "AND NOT")
        return apply_limits(equation, options, {}, SCOPUS_TYPE_FILTERS)

    def notes(self, question: StructuredQuestion) -> tuple[str, ...]:
        return _keywords_note(question, "Scopus", "TITLE-ABS-KEY")


class WebOfScienceDialect:
    """Equation for the advanced search of the Web of Science Core Collection."""

    name = "wos"
    vocabularies: frozenset[Vocabulary] = frozenset()
    fields: ClassVar[dict[FreeTextField, str]] = {
        FreeTextField.TITLE_ABSTRACT: "TS=",
        FreeTextField.TITLE: "TI=",
    }

    def _group(self, condition: Condition) -> str:
        return _fielded_group(condition, self.fields)

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        equation = render_equation(question, options, self.vocabularies, self._group, "NOT")
        return apply_limits(equation, options, {}, WOS_TYPE_FILTERS)

    def notes(self, question: StructuredQuestion) -> tuple[str, ...]:
        return _keywords_note(question, "Web of Science", "TS=")
