"""Domain model of a structured search question.

The model follows Rafael del Pino's method: a question is decomposed into components
(SPC or PICO), every component holds one or more *conditions* that are joined with AND,
and every condition holds the *terms* (controlled headings and free-text synonyms) that
are joined with OR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class QuestionFormat(StrEnum):
    """Frameworks used to decompose a search question."""

    SPC = "spc"
    PICO = "pico"


class ComponentRole(StrEnum):
    """Role of a component inside its question format."""

    SITUATION = "situation"
    POPULATION = "population"
    QUESTION = "question"
    INTERVENTION = "intervention"
    COMPARISON = "comparison"
    OUTCOME = "outcome"


FORMAT_ROLES: dict[QuestionFormat, tuple[ComponentRole, ...]] = {
    QuestionFormat.SPC: (
        ComponentRole.SITUATION,
        ComponentRole.POPULATION,
        ComponentRole.QUESTION,
    ),
    QuestionFormat.PICO: (
        ComponentRole.POPULATION,
        ComponentRole.INTERVENTION,
        ComponentRole.COMPARISON,
        ComponentRole.OUTCOME,
    ),
}


class TermKind(StrEnum):
    """Whether a term comes from a thesaurus or is free text."""

    CONTROLLED = "controlled"
    FREE = "free"


class ControlledScope(StrEnum):
    """How a controlled heading is searched, from most to least sensitive.

    Maps to PubMed's [mh], [mj], [mh:noexp] and [mj:noexp].
    """

    HEADING = "heading"
    MAJOR = "major"
    HEADING_NO_EXPLODE = "heading_no_explode"
    MAJOR_NO_EXPLODE = "major_no_explode"


class FreeTextField(StrEnum):
    """Fields where a free-text term is searched."""

    TITLE_ABSTRACT = "title_abstract"
    TITLE = "title"


class QuestionError(ValueError):
    """Raised when a structured question or one of its parts is not valid."""


TRUNCATION = "*"
_BOOLEAN_WORD = re.compile(r"\b(?:AND|OR|NOT)\b", re.IGNORECASE)
# Las comillas y los corchetes rompen la sintaxis de PubMed: no hay forma de escaparlos.
_FORBIDDEN_CHARS = frozenset('"[]')


@dataclass(frozen=True, slots=True)
class Term:
    """A single search term.

    Controlled terms are thesaurus headings (MeSH) and can be narrowed with ``scope``.
    Free terms are author language and can be truncated with a trailing ``*``.
    """

    text: str
    kind: TermKind
    scope: ControlledScope = ControlledScope.HEADING
    free_field: FreeTextField = FreeTextField.TITLE_ABSTRACT

    def __post_init__(self) -> None:
        stripped = " ".join(self.text.split())
        if not stripped:
            raise QuestionError("a term cannot be empty")
        object.__setattr__(self, "text", stripped)
        bad = _FORBIDDEN_CHARS.intersection(stripped)
        if bad:
            raise QuestionError(f"term {stripped!r} contains forbidden characters: {sorted(bad)}")
        if TRUNCATION in stripped:
            if self.kind is TermKind.CONTROLLED:
                # Truncar un descriptor lo convierte en lenguaje libre (Del Pino, 2017).
                raise QuestionError(f"controlled term {stripped!r} cannot be truncated")
            if stripped.count(TRUNCATION) > 1 or not stripped.endswith(TRUNCATION):
                raise QuestionError(f"truncation is only allowed at the end of {stripped!r}")
            if not stripped[:-1].strip():
                raise QuestionError("a truncated term needs a root before the asterisk")

    @classmethod
    def controlled(cls, text: str, scope: ControlledScope = ControlledScope.HEADING) -> Term:
        return cls(text=text, kind=TermKind.CONTROLLED, scope=scope)

    @classmethod
    def free(cls, text: str, free_field: FreeTextField = FreeTextField.TITLE_ABSTRACT) -> Term:
        return cls(text=text, kind=TermKind.FREE, free_field=free_field)

    @property
    def is_truncated(self) -> bool:
        return self.text.endswith(TRUNCATION)

    @property
    def needs_quotes(self) -> bool:
        """Whether the term must be quoted so the database reads it literally.

        Quotes restrict the search, so they are only used when the term contains commas,
        parentheses or words that would be parsed as boolean operators.
        """
        return any(ch in self.text for ch in ",()") or bool(_BOOLEAN_WORD.search(self.text))


@dataclass(frozen=True, slots=True)
class Condition:
    """A requirement every retrieved record must meet: its terms are alternatives (OR)."""

    label: str
    terms: tuple[Term, ...]

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise QuestionError("a condition needs a label")
        if not self.terms:
            raise QuestionError(f"condition {self.label!r} has no terms")
        seen: set[tuple[str, TermKind]] = set()
        for term in self.terms:
            key = (term.text.casefold(), term.kind)
            if key in seen:
                raise QuestionError(f"condition {self.label!r} repeats the term {term.text!r}")
            seen.add(key)

    @property
    def controlled_terms(self) -> tuple[Term, ...]:
        return tuple(t for t in self.terms if t.kind is TermKind.CONTROLLED)

    @property
    def free_terms(self) -> tuple[Term, ...]:
        return tuple(t for t in self.terms if t.kind is TermKind.FREE)


@dataclass(frozen=True, slots=True)
class Component:
    """One element of the question (e.g. the population) and the conditions it yields.

    A component may have no conditions: Cochrane advises against searching every PICO
    element, so e.g. the comparison is often described but left out of the search.
    """

    role: ComponentRole
    description: str
    conditions: tuple[Condition, ...] = ()

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise QuestionError(f"component {self.role.value!r} needs a description")


@dataclass(frozen=True, slots=True)
class StructuredQuestion:
    """A search question decomposed into the components of its format."""

    text: str
    format: QuestionFormat
    components: tuple[Component, ...]
    exclusions: tuple[Condition, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise QuestionError("the question text cannot be empty")
        allowed = FORMAT_ROLES[self.format]
        roles = [c.role for c in self.components]
        for role in roles:
            if role not in allowed:
                raise QuestionError(
                    f"role {role.value!r} does not belong to the {self.format.value} format"
                )
        if len(set(roles)) != len(roles):
            raise QuestionError("each component role can appear only once")
        if not self.conditions:
            raise QuestionError("the question needs at least one condition to search for")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        """Conditions in the canonical order of the format, regardless of input order."""
        order = FORMAT_ROLES[self.format]
        ordered = sorted(self.components, key=lambda c: order.index(c.role))
        return tuple(cond for comp in ordered for cond in comp.conditions)


@dataclass(frozen=True, slots=True)
class StrategyOptions:
    """Knobs to balance sensitivity and specificity of the generated equation.

    ``repeat_controlled_as_free`` also searches every heading as free text, which recovers
    records not yet indexed (more sensitive). ``free_text_unindexed_only`` limits free text
    to records outside MEDLINE with ``NOT MEDLINE[sb]`` (more specific); Del Pino advises
    against it in systematic reviews.
    """

    repeat_controlled_as_free: bool = False
    free_text_unindexed_only: bool = False
