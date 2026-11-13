"""ERIC search equation generator.

The ERIC API runs on Solr: ``title:`` and ``description:`` (the abstract) are searched word by
word, without stemming («tutor» does not find «tutoring»), and a wildcard only works on a
single word. ERIC descriptors are searched in ``subject_ss``, the exact form of the descriptor
field, so «Reading» does not also bring every descriptor with that word («Reading Habits»).
ERIC has no major descriptors nor explosion: every scope is searched the same way.
https://eric.ed.gov/?api · https://eric.ed.gov/?advanced
"""

from __future__ import annotations

from biblio_search.models import (
    TRUNCATION,
    Condition,
    ControlledScope,
    FreeTextField,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermKind,
    Vocabulary,
)
from biblio_search.pubmed_query import expand_condition, localize_condition

DESCRIPTOR_FIELD = "subject_ss"


_OPERATORS = frozenset({"and", "or", "not"})


def _is_plain_word(text: str) -> bool:
    return text.isalnum() and text.casefold() not in _OPERATORS


def quote(text: str) -> str:
    """Quote ``text`` as a Solr phrase."""
    # Dentro de comillas Solr solo interpreta la barra invertida; las comillas no llegan aquí.
    return '"' + text.replace("\\", "\\\\") + '"'


def render_text(term: Term) -> str:
    """The text of a free term as Solr reads it: a word, a truncated word or a phrase.

    A truncated phrase cannot be a Solr phrase, so its words are required together
    (``(peer AND tutor*)``): the last one keeps its wildcard, the order is lost.
    """
    if not term.is_truncated:
        if _is_plain_word(term.text) and not term.needs_quotes:
            return term.text
        return quote(term.text)
    *first, last = term.text.removesuffix(TRUNCATION).split()
    # Un comodín solo vale tras una palabra simple: «self-regulat*» va sin él (y se avisa).
    last_rendered = last + TRUNCATION if _is_plain_word(last) else quote(last)
    if not first:
        return last_rendered
    rendered = [w if _is_plain_word(w) else quote(w) for w in first]
    return "(" + " AND ".join([*rendered, last_rendered]) + ")"


def render_term(term: Term) -> str:
    """Render one term in the ERIC fields it is searched in."""
    if term.kind is TermKind.CONTROLLED:
        return f"{DESCRIPTOR_FIELD}:{quote(term.text)}"
    text = render_text(term)
    if term.free_field is FreeTextField.TITLE:
        return f"title:{text}"
    return f"title:{text} OR description:{text}"


def _local_condition(condition: Condition, options: StrategyOptions) -> Condition:
    return expand_condition(localize_condition(condition, EricDialect.vocabularies), options)


def _or_group(condition: Condition) -> str:
    return "(" + " OR ".join(render_term(t) for t in condition.terms) + ")"


class EricDialect:
    """Query dialect for the ERIC API, with ERIC Thesaurus descriptors."""

    name = "eric"
    vocabularies = frozenset({Vocabulary.ERIC})

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        # NOT MEDLINE[sb] no tiene equivalente: ERIC indexa todos sus registros con descriptores.
        equation = " AND ".join(
            _or_group(_local_condition(c, options)) for c in question.conditions
        )
        for exclusion in question.exclusions:
            equation += f" NOT {_or_group(_local_condition(exclusion, StrategyOptions()))}"
        return equation

    def notes(self, question: StructuredQuestion) -> tuple[str, ...]:
        conditions = question.conditions + question.exclusions
        terms = [t for c in conditions for t in localize_condition(c, self.vocabularies).terms]
        notes: list[str] = []
        phrases = [t.text for t in terms if t.is_truncated and " " in t.text]
        if phrases:
            notes.append(
                "ERIC takes no wildcard inside a phrase, so the words of these are required "
                "together, in any order: " + ", ".join(phrases)
            )
        unwildcarded = [
            t.text
            for t in terms
            if t.is_truncated and not _is_plain_word(t.text.removesuffix(TRUNCATION).split()[-1])
        ]
        if unwildcarded:
            notes.append(
                "ERIC only takes a wildcard after a plain word, so these are searched without "
                "it: " + ", ".join(unwildcarded)
            )
        scoped = [
            t.text
            for t in terms
            if t.kind is TermKind.CONTROLLED and t.scope is not ControlledScope.HEADING
        ]
        if scoped:
            notes.append(
                "ERIC has no major descriptors nor explosion, so these are searched as plain "
                "descriptors: " + ", ".join(scoped)
            )
        return tuple(notes)
