"""Assisted tuning of sensitivity and specificity (Del Pino, 2017, «Ajustar sensibilidad y
especificidad»).

There is no rule for how many records a search should return, but Del Pino gives two
warnings: distrust fewer than 100 records on a topic with enough scientific output (too
specific), and if none of the first 50 records is relevant the search is probably too
sensitive. To act on them, the tuning counts what every condition contributes and what each
of the method's levers (field tags, repeating headings as free text, NOT MEDLINE[sb]) would
change, one lever at a time, so the user compares real numbers instead of guessing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    ControlledScope,
    FreeTextField,
    QuestionFormat,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermKind,
)
from biblio_search.pipeline import SearchBackend
from biblio_search.protocols import QueryDialect

# Del Pino (2017): menos de 100 documentos en un tema con producción suficiente es sospechoso,
# y 50 primeros resultados sin ninguno relevante apuntan a una búsqueda demasiado sensible.
LOW_COUNT = 100
SCREENING_WINDOW = 50


class Direction(StrEnum):
    """What a lever does to the search."""

    SENSITIVE = "sensitive"
    SPECIFIC = "specific"


class AdviceKind(StrEnum):
    """Which of Del Pino's warnings applies."""

    NO_RESULTS = "no_results"
    TOO_SPECIFIC = "too_specific"
    TOO_SENSITIVE = "too_sensitive"


@dataclass(frozen=True, slots=True)
class Variant:
    """The question with one lever moved, as it would be searched."""

    key: str
    description: str
    direction: Direction
    question: StructuredQuestion
    options: StrategyOptions


@dataclass(frozen=True, slots=True)
class VariantCount:
    """How many records a variant finds in one database."""

    key: str
    description: str
    direction: Direction
    equation: str
    count: int


@dataclass(frozen=True, slots=True)
class ConditionCount:
    """What one condition contributes: its records alone and the total without it.

    ``without`` is ``None`` when the condition is the only one (removing it leaves nothing).
    """

    label: str
    equation: str
    alone: int
    without: int | None


@dataclass(frozen=True, slots=True)
class Advice:
    """One of Del Pino's warnings with concrete changes, most effective first."""

    kind: AdviceKind
    message: str
    suggestions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TuningReport:
    """Counts behind the equation of one database and what to do about them.

    ``without_exclusions`` is the total if the NOT conditions were dropped (``None`` when the
    question has none).
    """

    database: str
    equation: str
    total: int
    conditions: tuple[ConditionCount, ...]
    without_exclusions: int | None
    variants: tuple[VariantCount, ...]
    advice: tuple[Advice, ...]


def _map_terms(question: StructuredQuestion, change: Callable[[Term], Term]) -> StructuredQuestion:
    def condition(c: Condition) -> Condition:
        return Condition(label=c.label, terms=tuple(change(t) for t in c.terms))

    components = tuple(
        replace(c, conditions=tuple(condition(x) for x in c.conditions))
        for c in question.components
    )
    return replace(
        question,
        components=components,
        exclusions=tuple(condition(x) for x in question.exclusions),
    )


def _rescope(mapping: dict[ControlledScope, ControlledScope]) -> Callable[[Term], Term]:
    def change(term: Term) -> Term:
        if term.kind is not TermKind.CONTROLLED:
            return term
        return replace(term, scope=mapping.get(term.scope, term.scope))

    return change


def _refield(field: FreeTextField) -> Callable[[Term], Term]:
    def change(term: Term) -> Term:
        return replace(term, free_field=field) if term.kind is TermKind.FREE else term

    return change


_TO_MAJOR = {
    ControlledScope.HEADING: ControlledScope.MAJOR,
    ControlledScope.HEADING_NO_EXPLODE: ControlledScope.MAJOR_NO_EXPLODE,
}
_TO_ANY = {v: k for k, v in _TO_MAJOR.items()}
_TO_EXPLODED = {
    ControlledScope.HEADING_NO_EXPLODE: ControlledScope.HEADING,
    ControlledScope.MAJOR_NO_EXPLODE: ControlledScope.MAJOR,
}


def variants(question: StructuredQuestion, options: StrategyOptions) -> tuple[Variant, ...]:
    """Every lever of the method, each moved on its own in the direction it can still go.

    A lever that would not change the question (e.g. headings already major topics) is left
    out; one that changes the question but not a given database's equation is dropped later.
    """
    found: list[Variant] = []

    def add(
        key: str, description: str, direction: Direction, q: StructuredQuestion, o: StrategyOptions
    ) -> None:
        if q != question or o != options:
            found.append(Variant(key, description, direction, q, o))

    if options.repeat_controlled_as_free:
        add(
            "no_repeat",
            "stop repeating headings as free text",
            Direction.SPECIFIC,
            question,
            replace(options, repeat_controlled_as_free=False),
        )
    else:
        add(
            "repeat",
            "also search every heading as free text",
            Direction.SENSITIVE,
            question,
            replace(options, repeat_controlled_as_free=True),
        )
    if options.free_text_unindexed_only:
        add(
            "indexed_too",
            "search free text in MEDLINE too",
            Direction.SENSITIVE,
            question,
            replace(options, free_text_unindexed_only=False),
        )
    else:
        add(
            "unindexed_only",
            "free text only outside MEDLINE (NOT MEDLINE[sb]; not for systematic reviews)",
            Direction.SPECIFIC,
            question,
            replace(options, free_text_unindexed_only=True),
        )
    add(
        "major",
        "headings as major topics ([mj])",
        Direction.SPECIFIC,
        _map_terms(question, _rescope(_TO_MAJOR)),
        options,
    )
    add(
        "any_topic",
        "headings as any topic ([mh])",
        Direction.SENSITIVE,
        _map_terms(question, _rescope(_TO_ANY)),
        options,
    )
    add(
        "explode",
        "headings with their narrower ones (explode)",
        Direction.SENSITIVE,
        _map_terms(question, _rescope(_TO_EXPLODED)),
        options,
    )
    add(
        "title_only",
        "free text in the title only ([ti])",
        Direction.SPECIFIC,
        _map_terms(question, _refield(FreeTextField.TITLE)),
        options,
    )
    add(
        "title_abstract",
        "free text in title and abstract ([tiab])",
        Direction.SENSITIVE,
        _map_terms(question, _refield(FreeTextField.TITLE_ABSTRACT)),
        options,
    )
    return tuple(found)


def condition_alone(question: StructuredQuestion, condition: Condition) -> StructuredQuestion:
    """The question reduced to one condition, without exclusions."""
    component = Component(ComponentRole.CONCEPT, condition.label, (condition,))
    return replace(question, format=QuestionFormat.CONCEPTS, components=(component,), exclusions=())


def without_condition(question: StructuredQuestion, index: int) -> StructuredQuestion:
    """The question without its ``index``-th condition (in :attr:`conditions` order)."""
    target = question.conditions[index]
    components: list[Component] = []
    removed = False
    for component in question.components:
        kept: list[Condition] = []
        for condition in component.conditions:
            # Se quita por identidad y una sola vez: dos condiciones pueden ser iguales.
            if not removed and condition is target:
                removed = True
                continue
            kept.append(condition)
        components.append(replace(component, conditions=tuple(kept)))
    return replace(question, components=tuple(components))


def _gain(count: int, total: int) -> str:
    return f"{count:,} records ({count - total:+,d})"


def advise(
    total: int,
    conditions: tuple[ConditionCount, ...],
    counted: tuple[VariantCount, ...],
    *,
    none_relevant: bool = False,
) -> tuple[Advice, ...]:
    """Apply Del Pino's warnings to the counts of one database.

    ``none_relevant`` is the user's verdict after screening the first 50 records: only a
    person can tell whether a record is relevant.
    """
    advice: list[Advice] = []
    wider = sorted(
        (v for v in counted if v.direction is Direction.SENSITIVE and v.count > total),
        key=lambda v: -v.count,
    )
    removable = [c for c in conditions if c.without is not None and c.without > total]
    tightest = max(removable, key=lambda c: c.without or 0, default=None)
    if total == 0:
        empty = [c.label for c in conditions if c.alone == 0]
        suggestions = [f"«{label}» finds nothing on its own: check its terms" for label in empty]
        if not empty and tightest is not None:
            suggestions.append(
                f"the conditions find records apart but not together; without «{tightest.label}» "
                f"there are {tightest.without:,}"
            )
        suggestions += [f"{v.description}: {_gain(v.count, total)}" for v in wider]
        advice.append(
            Advice(AdviceKind.NO_RESULTS, "The search finds no records.", tuple(suggestions))
        )
    elif total < LOW_COUNT:
        suggestions = [f"{v.description}: {_gain(v.count, total)}" for v in wider]
        if tightest is not None:
            suggestions.append(
                f"«{tightest.label}» cuts the most ({tightest.without:,} records without it): "
                "add synonyms or truncation to it, or check that it is essential"
            )
        advice.append(
            Advice(
                AdviceKind.TOO_SPECIFIC,
                f"Fewer than {LOW_COUNT} records: if the topic has enough scientific output, "
                "the search is probably too specific.",
                tuple(suggestions),
            )
        )
    if none_relevant and total > 0:
        narrower = sorted(
            (v for v in counted if v.direction is Direction.SPECIFIC and v.count < total),
            key=lambda v: v.count,
        )
        suggestions = [f"{v.description}: {_gain(v.count, total)}" for v in narrower]
        loosest = max(conditions, key=lambda c: c.alone, default=None)
        if loosest is not None and len(conditions) > 1:
            suggestions.append(
                f"«{loosest.label}» is the least selective condition ({loosest.alone:,} records "
                "on its own): narrow its terms"
            )
        suggestions.append("add a condition the relevant records must meet")
        advice.append(
            Advice(
                AdviceKind.TOO_SENSITIVE,
                f"None of the first {SCREENING_WINDOW} records is relevant: the search is "
                "probably too sensitive.",
                tuple(suggestions),
            )
        )
    return tuple(advice)


async def tune(
    question: StructuredQuestion,
    options: StrategyOptions,
    dialect: QueryDialect,
    backend: SearchBackend,
    *,
    none_relevant: bool = False,
) -> TuningReport:
    """Count the equation, each condition and each variant in one database, and advise.

    Only counts are asked for (``retmax=0``), and an equation already counted is not sent
    again: with OpenAlex every request draws from the daily budget.
    """
    counts: dict[str, int] = {}

    async def count(equation: str) -> int:
        if equation not in counts:
            counts[equation] = (await backend.search(equation, retmax=0)).count
        return counts[equation]

    equation = dialect.render(question, options)
    total = await count(equation)
    several = len(question.conditions) > 1
    conditions: list[ConditionCount] = []
    for index, condition in enumerate(question.conditions):
        alone = dialect.render(condition_alone(question, condition), options)
        without = (
            await count(dialect.render(without_condition(question, index), options))
            if several
            else None
        )
        conditions.append(ConditionCount(condition.label, alone, await count(alone), without))
    without_exclusions = None
    if question.exclusions:
        plain = replace(question, exclusions=())
        without_exclusions = await count(dialect.render(plain, options))
    counted: list[VariantCount] = []
    seen = {equation}
    for variant in variants(question, options):
        rendered = dialect.render(variant.question, variant.options)
        # En una base que no tiene esa palanca (OpenAlex no tiene MeSH ni MEDLINE) la ecuación
        # no cambia: no hay nada que comparar.
        if rendered in seen:
            continue
        seen.add(rendered)
        counted.append(
            VariantCount(
                variant.key,
                variant.description,
                variant.direction,
                rendered,
                await count(rendered),
            )
        )
    found = tuple(conditions)
    variant_counts = tuple(counted)
    return TuningReport(
        database=dialect.name,
        equation=equation,
        total=total,
        conditions=found,
        without_exclusions=without_exclusions,
        variants=variant_counts,
        advice=advise(total, found, variant_counts, none_relevant=none_relevant),
    )
