"""arXiv search equation generator.

arXiv searches each field separately (``ti:`` title, ``abs:`` abstract, ``cat:`` subject
category), so a title-and-abstract term is asked in both. Its search engine stems every
word («caregiver» also finds «caregiving») and only takes a wildcard on a single word, so a
truncated phrase is searched as the phrase itself. arXiv indexes no thesaurus: every
heading is searched as free text. https://info.arxiv.org/help/api/user-manual.html
"""

from __future__ import annotations

from biblio_search.models import (
    TRUNCATION,
    Condition,
    FreeTextField,
    StrategyOptions,
    StructuredQuestion,
    Term,
    Vocabulary,
)
from biblio_search.pubmed_query import localize_condition


def _is_plain_word(text: str) -> bool:
    return text.isalnum()


def render_text(term: Term) -> str:
    """The text of a term as arXiv reads it: a bare word, a truncated word or a phrase."""
    text = term.text.removesuffix(TRUNCATION) if term.is_truncated else term.text
    if _is_plain_word(text) and not term.needs_quotes:
        return text + TRUNCATION if term.is_truncated else text
    # Sin comillas, arXiv parte «self-care» en palabras sueltas y encuentra 20 veces más.
    return f'"{text}"'


def render_term(term: Term) -> str:
    """Render one free-text term in the fields it is searched in."""
    text = render_text(term)
    if term.free_field is FreeTextField.TITLE:
        return f"ti:{text}"
    return f"ti:{text} OR abs:{text}"


def _local_terms(condition: Condition) -> tuple[Term, ...]:
    return localize_condition(condition, ArxivDialect.vocabularies).terms


def _or_group(condition: Condition) -> str:
    return "(" + " OR ".join(render_term(t) for t in _local_terms(condition)) + ")"


class ArxivDialect:
    """Query dialect for the arXiv API, searched in title and abstract."""

    name = "arxiv"
    vocabularies: frozenset[Vocabulary] = frozenset()

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        # Como en OpenAlex, las opciones actúan sobre descriptores y MEDLINE, que arXiv no tiene.
        parts = [_or_group(c) for c in question.conditions]
        if question.arxiv_categories:
            parts.append("(" + " OR ".join(f"cat:{c}" for c in question.arxiv_categories) + ")")
        equation = " AND ".join(parts)
        for exclusion in question.exclusions:
            equation += f" ANDNOT {_or_group(exclusion)}"
        return equation

    def notes(self, question: StructuredQuestion) -> tuple[str, ...]:
        terms = [t for c in question.conditions + question.exclusions for t in _local_terms(c)]
        phrases = [t.text for t in terms if t.is_truncated and not _is_plain_word(t.text[:-1])]
        if not phrases:
            return ()
        return (
            "arXiv takes no wildcard inside a phrase, so these are searched as the phrase "
            "(arXiv already finds other endings of each word): " + ", ".join(phrases),
        )
