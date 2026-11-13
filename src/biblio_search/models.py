"""Domain model of a structured search question.

The model follows Rafael del Pino's method: a question is decomposed into components
(SPC or PICO for health questions, free concepts for any other discipline), every component
holds one or more *conditions* that are joined with AND,
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
    CONCEPTS = "concepts"


class ComponentRole(StrEnum):
    """Role of a component inside its question format."""

    SITUATION = "situation"
    POPULATION = "population"
    QUESTION = "question"
    INTERVENTION = "intervention"
    COMPARISON = "comparison"
    OUTCOME = "outcome"
    CONCEPT = "concept"


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
    QuestionFormat.CONCEPTS: (ComponentRole.CONCEPT,),
}

# SPC y PICO fijan un orden y un único componente por rol; los conceptos libres no.
REPEATABLE_ROLES = frozenset({ComponentRole.CONCEPT})


class Discipline(StrEnum):
    """Field of knowledge of a question, used to choose where to search."""

    HEALTH = "health"
    EDUCATION = "education"
    PHYSICS = "physics"
    MATHEMATICS = "mathematics"
    COMPUTER_SCIENCE = "computer_science"
    ECONOMICS = "economics"


class Database(StrEnum):
    """Free bibliographic databases a question can be searched in."""

    PUBMED = "pubmed"
    ERIC = "eric"
    ARXIV = "arxiv"
    OPENALEX = "openalex"


class Vocabulary(StrEnum):
    """Thesauri that controlled terms can come from."""

    MESH = "mesh"
    ERIC = "eric"


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


class DatabaseError(RuntimeError):
    """Raised when a bibliographic database cannot be reached or rejects a request."""


TRUNCATION = "*"
_BOOLEAN_WORD = re.compile(r"\b(?:AND|OR|NOT)\b", re.IGNORECASE)
# Las comillas y los corchetes rompen la sintaxis de PubMed: no hay forma de escaparlos.
_FORBIDDEN_CHARS = frozenset('"[]')
# Categorías de arXiv: archivo con guiones y subcategoría opcional, o todo el archivo con «.*»
# (cs.LG, quant-ph, cond-mat.str-el, q-fin.ST, math.*).
_ARXIV_CATEGORY = re.compile(r"^[a-z]+(?:-[a-z]+)*(?:\.(?:[A-Za-z]+(?:-[a-z]+)*|\*))?$")


@dataclass(frozen=True, slots=True)
class Term:
    """A single search term.

    Controlled terms are thesaurus headings (MeSH unless ``vocabulary`` says otherwise) and
    can be narrowed with ``scope``. Free terms are author language, belong to no vocabulary
    and can be truncated with a trailing ``*``.
    """

    text: str
    kind: TermKind
    scope: ControlledScope = ControlledScope.HEADING
    free_field: FreeTextField = FreeTextField.TITLE_ABSTRACT
    vocabulary: Vocabulary | None = None

    def __post_init__(self) -> None:
        stripped = " ".join(self.text.split())
        if not stripped:
            raise QuestionError("a term cannot be empty")
        object.__setattr__(self, "text", stripped)
        if self.kind is TermKind.FREE and self.vocabulary is not None:
            raise QuestionError(f"free term {stripped!r} cannot belong to a vocabulary")
        if self.kind is TermKind.CONTROLLED and self.vocabulary is None:
            # MeSH por defecto: así los ficheros anteriores a los vocabularios siguen valiendo.
            object.__setattr__(self, "vocabulary", Vocabulary.MESH)
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
    def controlled(
        cls,
        text: str,
        scope: ControlledScope = ControlledScope.HEADING,
        vocabulary: Vocabulary = Vocabulary.MESH,
    ) -> Term:
        return cls(text=text, kind=TermKind.CONTROLLED, scope=scope, vocabulary=vocabulary)

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
        seen: set[tuple[str, TermKind, Vocabulary | None]] = set()
        for term in self.terms:
            key = (term.text.casefold(), term.kind, term.vocabulary)
            if key in seen:
                raise QuestionError(f"condition {self.label!r} repeats the term {term.text!r}")
            seen.add(key)

    @property
    def controlled_terms(self) -> tuple[Term, ...]:
        return tuple(t for t in self.terms if t.kind is TermKind.CONTROLLED)

    @property
    def free_terms(self) -> tuple[Term, ...]:
        return tuple(t for t in self.terms if t.kind is TermKind.FREE)

    def controlled_in(self, vocabulary: str) -> tuple[Term, ...]:
        """Controlled terms that come from ``vocabulary``."""
        return tuple(t for t in self.controlled_terms if t.vocabulary == vocabulary)


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
    """A search question decomposed into the components of its format.

    ``discipline`` helps choose the databases automatically; ``databases`` chooses them by
    hand and, when given, overrides that choice. ``arxiv_categories`` (e.g. ``cs.LG`` or
    ``math.*``) restrict the search in arXiv only.
    """

    text: str
    format: QuestionFormat
    components: tuple[Component, ...]
    exclusions: tuple[Condition, ...] = field(default=())
    discipline: Discipline | None = None
    databases: tuple[Database, ...] = ()
    arxiv_categories: tuple[str, ...] = ()

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
        fixed = [role for role in roles if role not in REPEATABLE_ROLES]
        if len(set(fixed)) != len(fixed):
            raise QuestionError("each component role can appear only once")
        if len(set(self.databases)) != len(self.databases):
            raise QuestionError("each database can be chosen only once")
        for category in self.arxiv_categories:
            if not _ARXIV_CATEGORY.match(category):
                raise QuestionError(
                    f"{category!r} is not an arXiv category (e.g. cs.LG, quant-ph or math.*)"
                )
        if len(set(self.arxiv_categories)) != len(self.arxiv_categories):
            raise QuestionError("each arXiv category can be given only once")
        if not self.conditions:
            raise QuestionError("the question needs at least one condition to search for")

    @property
    def conditions(self) -> tuple[Condition, ...]:
        """Conditions in the canonical order of the format, regardless of input order.

        Free concepts have no canonical order, so they keep the order they were given in.
        """
        order = FORMAT_ROLES[self.format]
        ordered = sorted(self.components, key=lambda c: order.index(c.role))
        return tuple(cond for comp in ordered for cond in comp.conditions)

    @property
    def controlled_terms(self) -> tuple[Term, ...]:
        """Every controlled term of the conditions and exclusions."""
        return tuple(t for c in self.conditions + self.exclusions for t in c.controlled_terms)


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


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Outcome of running an equation against a database.

    ``query_translation`` is how the database actually understood the equation: PubMed maps
    and silently drops parts of a query, so it must be shown next to the count.
    ``not_found`` lists terms or fields the database did not recognise and ``warnings``
    any other message it returned (e.g. ignored phrases or stray syntax).
    """

    count: int
    ids: tuple[str, ...]
    query_translation: str
    not_found: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArticleSummary:
    """Bibliographic summary of a record, enough to screen it by title.

    ``record_id`` is the identifier in the database that returned it (a PMID in PubMed, a
    work id such as ``W2167987787`` in OpenAlex) and ``url`` its page there. ``doi`` is bare
    (``10.1000/x``), without the ``https://doi.org/`` prefix, and ``pmid`` is the PubMed id
    of the same work when the database knows it: both are what tells duplicates apart when
    the records of several databases are merged.
    """

    record_id: str
    title: str
    authors: tuple[str, ...]
    journal: str
    year: int | None
    doi: str | None
    publication_types: tuple[str, ...]
    url: str
    pmid: str | None = None


@dataclass(frozen=True, slots=True)
class ThesaurusEntry:
    """A heading as the thesaurus defines it (e.g. a MeSH descriptor).

    ``synonyms`` are the entry terms rewritten as free text: without the inverted forms
    («Anxiety, Social») that authors never write, as Del Pino advises. ``has_narrower``
    tells whether searching the heading also retrieves narrower ones (it *explodes*), so
    the no-explode scopes only make a difference when it is true. ``translated_from`` names
    the thesaurus that led to the heading from a text in another language (``"decs"`` for a
    Spanish term), and is empty when the text was found in the thesaurus itself.
    ``identifier`` is empty in thesauri without one (ERIC). ``use_with`` lists the other
    headings the thesaurus says to combine with this one for the text that led to it (ERIC
    sends «Peer Tutoring» to «Peer Teaching» and «Tutoring» together).
    """

    vocabulary: str
    identifier: str
    name: str
    synonyms: tuple[str, ...] = ()
    has_narrower: bool = False
    scope_note: str = ""
    translated_from: str = ""
    use_with: tuple[str, ...] = ()


class TermStatus(StrEnum):
    """Outcome of checking a controlled term against its thesaurus."""

    VALID = "valid"
    ENTRY_TERM = "entry_term"
    TRANSLATED = "translated"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class TermCheck:
    """Result of checking one controlled term of one condition.

    ``ENTRY_TERM`` means the text is a synonym of a heading (``entry.name``) rather than the
    heading itself; ``TRANSLATED`` means it is the same heading in another language (a DeCS
    term in Spanish for a MeSH descriptor); ``NOT_FOUND`` means it is not in the thesaurus and
    must be searched as free text.
    """

    condition: str
    term: Term
    status: TermStatus
    entry: ThesaurusEntry | None = None

    def __post_init__(self) -> None:
        if (self.entry is None) != (self.status is TermStatus.NOT_FOUND):
            raise ValueError("only terms that were not found come without a thesaurus entry")
