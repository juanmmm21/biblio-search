"""OpenAlex search equation generator.

The equation is sent as ``search.title_and_abstract.exact``: OpenAlex only accepts
wildcards in its unstemmed search, and the stemmed one reads «carer» as «care», so the
exact search is also the one that does what the user wrote, like PubMed's [tiab].
OpenAlex indexes no thesaurus, so every heading is searched as free text.
"""

from __future__ import annotations

from biblio_search.models import (
    Condition,
    FreeTextField,
    StrategyOptions,
    StructuredQuestion,
    Term,
    Vocabulary,
)
from biblio_search.pubmed_query import localize_condition


def render_term(term: Term) -> str:
    """Render one free-text term, quoting phrases so their words are searched together."""
    if term.is_truncated and " " in term.text:
        # OpenAlex rechaza comodines en una frase entre comillas, pero no en una de
        # proximidad: con distancia 0 las palabras siguen yendo juntas.
        return f'"{term.text}"~0'
    if " " in term.text or term.needs_quotes:
        return f'"{term.text}"'
    return term.text


def _local_terms(condition: Condition) -> tuple[Term, ...]:
    return localize_condition(condition, OpenAlexDialect.vocabularies).terms


def _or_group(condition: Condition) -> str:
    return "(" + " OR ".join(render_term(t) for t in _local_terms(condition)) + ")"


class OpenAlexDialect:
    """Query dialect for OpenAlex works, searched in title and abstract."""

    name = "openalex"
    vocabularies: frozenset[Vocabulary] = frozenset()

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        # Las opciones actúan sobre descriptores y MEDLINE, que OpenAlex no tiene: todo
        # descriptor ya se busca como texto libre aquí.
        equation = " AND ".join(_or_group(c) for c in question.conditions)
        for exclusion in question.exclusions:
            equation += f" NOT {_or_group(exclusion)}"
        return equation

    def notes(self, question: StructuredQuestion) -> tuple[str, ...]:
        terms = [t for c in question.conditions + question.exclusions for t in _local_terms(c)]
        title_only = [t.text for t in terms if t.free_field is FreeTextField.TITLE]
        if not title_only:
            return ()
        return (
            "OpenAlex cannot mix fields in one search, so these title-only terms are "
            f"searched in title and abstract: {', '.join(title_only)}",
        )
