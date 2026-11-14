"""PubMed search equation generator.

Every condition becomes a parenthesised group of OR-ed terms and the groups are joined with
AND. PubMed evaluates operators left to right, so the parentheses are what make AND and OR
coexist safely in a single equation (Del Pino, 2017).
"""

from __future__ import annotations

from collections.abc import Collection

from biblio_search.models import (
    TRUNCATION,
    Condition,
    ControlledScope,
    EvidenceLevel,
    FreeTextField,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermKind,
    Vocabulary,
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

_GUIDELINE_TYPES = "Guideline[pt] OR Practice Guideline[pt]"
# systematic[sb] es el filtro de revisiones sistemáticas de PubMed: reúne los tipos de
# publicación (Systematic Review, Meta-Analysis...) y una estrategia de texto libre que
# también encuentra las que aún no están indexadas.
_SYSTEMATIC_FILTER = "systematic[sb]"
# PubMed no tiene un filtro de estudios originales: se quitan las síntesis (también las
# revisiones narrativas, que según Del Pino no aportan evidencia) y lo que no es investigación.
_NOT_ORIGINAL = (
    f"{_SYSTEMATIC_FILTER} OR Review[pt] OR {_GUIDELINE_TYPES} OR Editorial[pt] OR Letter[pt] "
    "OR Comment[pt] OR News[pt]"
)
EVIDENCE_FILTERS: dict[EvidenceLevel, str] = {
    EvidenceLevel.GUIDELINES: f"AND ({_GUIDELINE_TYPES})",
    EvidenceLevel.SYSTEMATIC_REVIEWS: f"AND {_SYSTEMATIC_FILTER}",
    EvidenceLevel.ORIGINAL_STUDIES: f"NOT ({_NOT_ORIGINAL})",
}


def apply_evidence_level(equation: str, level: EvidenceLevel | None) -> str:
    """Limit a PubMed equation to one level of evidence.

    PubMed reads operators left to right, so the equation is wrapped in parentheses before
    the filter is joined: otherwise a trailing NOT of the question would swallow it.
    """
    if level is None:
        return equation
    return f"({equation}) {EVIDENCE_FILTERS[level]}"


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


def is_covered(text: str, free_terms: tuple[Term, ...]) -> bool:
    """Whether some free term already retrieves ``text`` (same word or a truncated root)."""
    folded = text.casefold()
    for term in free_terms:
        candidate = term.text.casefold()
        if term.is_truncated and folded.startswith(candidate[: -len(TRUNCATION)]):
            return True
        if candidate == folded:
            return True
    return False


def foreign_terms(
    question: StructuredQuestion, vocabularies: Collection[Vocabulary]
) -> tuple[Term, ...]:
    """Controlled terms of ``question`` from vocabularies a database does not index."""
    return tuple(t for t in question.controlled_terms if t.vocabulary not in vocabularies)


def localize_condition(condition: Condition, vocabularies: Collection[Vocabulary]) -> Condition:
    """Search headings from foreign vocabularies as free text.

    A heading of another thesaurus (an ERIC descriptor in PubMed) would match nothing as a
    heading, but its words still name the concept, so they are kept as free text unless a
    free term of the condition already retrieves them.
    """
    terms: list[Term] = []
    for term in condition.terms:
        if term.kind is TermKind.FREE or term.vocabulary in vocabularies:
            terms.append(term)
            continue
        free = tuple(t for t in terms if t.kind is TermKind.FREE) + condition.free_terms
        if not is_covered(term.text, free):
            terms.append(Term.free(term.text.lower()))
    return Condition(label=condition.label, terms=tuple(terms))


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
        if is_covered(heading.text, free_terms + tuple(extra)):
            continue
        extra.append(Term.free(heading.text.lower()))
    if not extra:
        return condition
    return Condition(label=condition.label, terms=condition.terms + tuple(extra))


def _or_group(terms: tuple[Term, ...]) -> str:
    return "(" + " OR ".join(render_term(t) for t in terms) + ")"


def render_condition(condition: Condition, options: StrategyOptions) -> str:
    """Render a condition as a parenthesised OR group."""
    condition = expand_condition(localize_condition(condition, PubMedDialect.vocabularies), options)
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
    vocabularies = frozenset({Vocabulary.MESH})

    def render(self, question: StructuredQuestion, options: StrategyOptions) -> str:
        equation = " AND ".join(render_condition(c, options) for c in question.conditions)
        for exclusion in question.exclusions:
            local = localize_condition(exclusion, self.vocabularies)
            equation += f" NOT {_or_group(local.terms)}"
        return apply_evidence_level(equation, options.evidence_level)

    def notes(self, question: StructuredQuestion) -> tuple[str, ...]:
        # PubMed entiende todo lo que el modelo permite escribir: no hay nada que explicar.
        return ()
