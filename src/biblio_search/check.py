"""Checking the controlled terms of a question against a thesaurus.

Headings are never trusted as typed (or as proposed by a model): each one is confirmed in
the thesaurus. Entry terms are replaced by the heading they point to, and texts that are
not in the thesaurus are searched as free text instead, so the equation never relies on a
heading that does not exist. Each thesaurus only checks the headings of its own vocabulary;
headings of other vocabularies are left as they are and listed as unchecked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from biblio_search.models import (
    Condition,
    ControlledScope,
    StructuredQuestion,
    Term,
    TermCheck,
    TermKind,
    TermStatus,
    ThesaurusEntry,
    Vocabulary,
)
from biblio_search.protocols import Thesaurus
from biblio_search.pubmed_query import is_covered

_NO_EXPLODE = frozenset({ControlledScope.HEADING_NO_EXPLODE, ControlledScope.MAJOR_NO_EXPLODE})


@dataclass(frozen=True, slots=True)
class ConditionCheck:
    """Checks of one condition plus the heading synonyms it does not search yet."""

    condition: Condition
    checks: tuple[TermCheck, ...]
    suggested_synonyms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuestionCheck:
    """Outcome of checking a whole question against one thesaurus."""

    vocabulary: str
    conditions: tuple[ConditionCheck, ...]
    unchecked: tuple[Term, ...] = ()

    @property
    def checks(self) -> tuple[TermCheck, ...]:
        return tuple(check for item in self.conditions for check in item.checks)

    @property
    def all_valid(self) -> bool:
        return all(check.status is TermStatus.VALID for check in self.checks)


def _status(term: Term, entry: ThesaurusEntry | None) -> TermStatus:
    if entry is None:
        return TermStatus.NOT_FOUND
    if entry.translated_from:
        return TermStatus.TRANSLATED
    if entry.name.casefold() == term.text.casefold():
        return TermStatus.VALID
    return TermStatus.ENTRY_TERM


def _singular(text: str) -> str:
    # Plural inglés aproximado de la última palabra: basta para no sugerir «Social Anxiety»
    # y «Social Anxieties» a la vez.
    folded = text.casefold()
    if folded.endswith("ies"):
        return folded[:-3] + "y"
    if folded.endswith("s") and not folded.endswith("ss"):
        return folded[:-1]
    return folded


def _suggestions(condition: Condition, checks: tuple[TermCheck, ...]) -> tuple[str, ...]:
    present = condition.free_terms
    headings = {c.entry.name.casefold() for c in checks if c.entry is not None}
    suggestions: list[str] = []
    forms: set[str] = set()
    for check in checks:
        if check.entry is None:
            continue
        for synonym in check.entry.synonyms:
            # Comillas, corchetes o asteriscos romperían el sinónimo como texto libre.
            if any(ch in synonym for ch in '"[]*'):
                continue
            taken = present + tuple(Term.free(s) for s in suggestions)
            if synonym.casefold() in headings or is_covered(synonym, taken):
                continue
            if _singular(synonym) in forms:
                continue
            forms.add(_singular(synonym))
            suggestions.append(synonym)
    return tuple(suggestions)


async def check_question(question: StructuredQuestion, thesaurus: Thesaurus) -> QuestionCheck:
    """Look up the controlled terms of ``question`` that belong to ``thesaurus``."""
    conditions = question.conditions + question.exclusions
    own = [t for c in conditions for t in c.controlled_in(thesaurus.name)]
    texts = list(dict.fromkeys(t.text for t in own))
    entries = await thesaurus.lookup(texts) if texts else {}
    results: list[ConditionCheck] = []
    for condition in conditions:
        checks = tuple(
            TermCheck(
                condition=condition.label,
                term=term,
                status=_status(term, entries.get(term.text)),
                entry=entries.get(term.text),
            )
            for term in condition.controlled_in(thesaurus.name)
        )
        results.append(ConditionCheck(condition, checks, _suggestions(condition, checks)))
    unchecked = tuple(t for t in question.controlled_terms if t.vocabulary != thesaurus.name)
    return QuestionCheck(thesaurus.name, tuple(results), tuple(dict.fromkeys(unchecked)))


def scope_note(check: TermCheck) -> str | None:
    """Explain what the chosen scope does for this heading, when it is worth saying."""
    if check.entry is None:
        return None
    if check.entry.use_with:
        others = " and ".join(check.entry.use_with)
        return f"the thesaurus pairs this term with {others} too; add it if it fits the question"
    # Solo MeSH explota los descriptores; en ERIC el alcance no cambia nada (lo dice el dialecto).
    if check.entry.vocabulary != Vocabulary.MESH:
        return None
    if check.term.scope in _NO_EXPLODE and not check.entry.has_narrower:
        return "has no narrower headings, so no_explode changes nothing"
    if check.term.scope not in _NO_EXPLODE and check.entry.has_narrower:
        return "also retrieves its narrower headings; use a no_explode scope to leave them out"
    return None


_CheckKey = tuple[str, Vocabulary | None]


def _fixed_term(term: Term, check: TermCheck | None) -> Term:
    if term.kind is TermKind.FREE or check is None or term.vocabulary is None:
        return term
    if check.entry is None:
        return Term.free(term.text)
    return Term.controlled(check.entry.name, term.scope, term.vocabulary)


def _fixed_condition(condition: Condition, checks: dict[_CheckKey, TermCheck]) -> Condition:
    terms: list[Term] = []
    seen: set[tuple[str, TermKind, Vocabulary | None]] = set()
    for term in condition.terms:
        fixed = _fixed_term(term, checks.get((term.text, term.vocabulary)))
        key = (fixed.text.casefold(), fixed.kind, fixed.vocabulary)
        if key not in seen:
            seen.add(key)
            terms.append(fixed)
    return Condition(label=condition.label, terms=tuple(terms))


def apply_check(question: StructuredQuestion, result: QuestionCheck) -> StructuredQuestion:
    """Rename entry terms to their heading and search unknown headings as free text."""
    checks = {(check.term.text, check.term.vocabulary): check for check in result.checks}
    components = tuple(
        replace(c, conditions=tuple(_fixed_condition(cond, checks) for cond in c.conditions))
        for c in question.components
    )
    exclusions = tuple(_fixed_condition(cond, checks) for cond in question.exclusions)
    return replace(question, components=components, exclusions=exclusions)


def vocabularies_to_check(question: StructuredQuestion) -> tuple[Vocabulary, ...]:
    """The thesauri the controlled terms of ``question`` come from, MeSH first.

    A question without headings is still checked against MeSH, so the report can say so.
    """
    used = {term.vocabulary for term in question.controlled_terms}
    found = tuple(vocabulary for vocabulary in Vocabulary if vocabulary in used)
    return found or (Vocabulary.MESH,)


async def check_all(
    question: StructuredQuestion, thesauri: Sequence[Thesaurus]
) -> tuple[QuestionCheck, ...]:
    """Check ``question`` against every thesaurus; only headings none of them knows stay
    listed as unchecked."""
    names = {thesaurus.name for thesaurus in thesauri}
    results: list[QuestionCheck] = []
    for thesaurus in thesauri:
        result = await check_question(question, thesaurus)
        unchecked = tuple(t for t in result.unchecked if t.vocabulary not in names)
        results.append(replace(result, unchecked=unchecked))
    return tuple(results)


def apply_checks(
    question: StructuredQuestion, results: Sequence[QuestionCheck]
) -> StructuredQuestion:
    """Apply the outcome of several checks, one thesaurus after another."""
    for result in results:
        question = apply_check(question, result)
    return question
