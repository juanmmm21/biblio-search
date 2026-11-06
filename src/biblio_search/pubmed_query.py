"""PubMed search equation generator.

Every condition becomes a parenthesised group of OR-ed terms and the groups are joined with
AND. PubMed evaluates operators left to right, so the parentheses are what make AND and OR
coexist safely in a single equation (Del Pino, 2017).
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
)

CONTROLLED_TAGS: dict[ControlledScope, str] = {
    ControlledScope.HEADING: "mh",
    ControlledScope.MAJOR: "mj",
    ControlledScope.HEADING_NO_EXPLODE: "mh:noexp",
    ControlledScope.MAJOR_NO_EXPLODE: "mj:noexp",
}

FREE_TAGS: dict[FreeTextField, str] = {
    FreeTextField.TITLE_ABSTRACT: "tiab",
    FreeTextField.TITLE: "ti",
}

UNINDEXED_FILTER = "MEDLINE[sb]"


def render_term(term: Term) -> str:
    """Render one term with its PubMed field tag, quoting it only when required."""
    if term.kind is TermKind.CONTROLLED:
        tag = CONTROLLED_TAGS[term.scope]
    else:
        tag = FREE_TAGS[term.free_field]
    # PubMed solo trunca frases de varias palabras si van entre comillas.
    quoted = term.needs_quotes or (term.is_truncated and " " in term.text)
    text = f'"{term.text}"' if quoted else term.text
    return f"{text}[{tag}]"


def _is_covered(text: str, free_terms: tuple[Term, ...]) -> bool:
    """Whether some free term already retrieves ``text`` (same word or a truncated root)."""
    folded = text.casefold()
    for term in free_terms:
        candidate = term.text.casefold()
        if term.is_truncated and folded.startswith(candidate[: -len(TRUNCATION)]):
            return True
        if candidate == folded:
            return True
    return False


def expand_condition(condition: Condition, options: StrategyOptions) -> Condition:
    """Apply the term-level options of the strategy to a condition."""
    if not options.repeat_controlled_as_free:
        return condition
    extra: list[Term] = []
    free_terms = condition.free_terms
    for heading in condition.controlled_terms:
        # Un descriptor con comas u operadores («Aged, 80 and over») no tiene una forma libre
        # natural: se omite y el usuario añade sus sinónimos a mano.
        if heading.needs_quotes:
            continue
        if _is_covered(heading.text, free_terms + tuple(extra)):
            continue
        extra.append(Term.free(heading.text.lower()))
    if not extra:
        return condition
    return Condition(label=condition.label, terms=condition.terms + tuple(extra))


def _or_group(terms: tuple[Term, ...]) -> str:
    return "(" + " OR ".join(render_term(t) for t in terms) + ")"


def render_condition(condition: Condition, options: StrategyOptions) -> str:
    """Render a condition as a parenthesised OR group."""
    condition = expand_condition(condition, options)
    controlled = condition.controlled_terms
    free = condition.free_terms
    # Sin descriptores, limitar el lenguaje libre a registros no indexados dejaría la
    # condición sin poder recuperar nada de MEDLINE: el filtro solo se aplica si hay ambos.
    if options.free_text_unindexed_only and controlled and free:
        headings = " OR ".join(render_term(t) for t in controlled)
        return f"({headings} OR ({_or_group(free)} NOT {UNINDEXED_FILTER}))"
    return _or_group(condition.terms)


class PubMedDialect:
    """Query dialect for PubMed / MEDLINE."""

    name = "pubmed"

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        equation = " AND ".join(render_condition(c, options) for c in question.conditions)
        for exclusion in question.exclusions:
            equation += f" NOT {_or_group(exclusion.terms)}"
        return equation
