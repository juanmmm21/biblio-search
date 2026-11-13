"""Loading structured questions from TOML files and writing them back."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from biblio_search.models import (
    Component,
    ComponentRole,
    Condition,
    ControlledScope,
    Database,
    Discipline,
    FreeTextField,
    QuestionError,
    QuestionFormat,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermKind,
    Vocabulary,
)


def _table(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QuestionError(f"{where} must be a table")
    return value


def _list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise QuestionError(f"{where} must be a list")
    return value


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise QuestionError(f"{where} must be a string")
    return value


def _bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise QuestionError(f"{where} must be true or false")
    return value


E = TypeVar("E", bound=StrEnum)


def _choice(enum: type[E], value: object, where: str) -> E:
    raw = _string(value, where)
    try:
        return enum(raw)
    except ValueError:
        valid = ", ".join(member.value for member in enum)
        raise QuestionError(f"{where} must be one of: {valid} (got {raw!r})") from None


def _controlled_term(value: object, where: str) -> Term:
    if isinstance(value, str):
        return Term.controlled(value)
    table = _table(value, where)
    scope = table.get("scope", ControlledScope.HEADING.value)
    vocabulary = table.get("vocabulary", Vocabulary.MESH.value)
    return Term.controlled(
        _string(table.get("term"), f"{where}.term"),
        _choice(ControlledScope, scope, f"{where}.scope"),
        _choice(Vocabulary, vocabulary, f"{where}.vocabulary"),
    )


def _free_term(value: object, where: str) -> Term:
    if isinstance(value, str):
        return Term.free(value)
    table = _table(value, where)
    free_field = table.get("field", FreeTextField.TITLE_ABSTRACT.value)
    return Term.free(
        _string(table.get("term"), f"{where}.term"),
        _choice(FreeTextField, free_field, f"{where}.field"),
    )


def _condition(value: object, where: str) -> Condition:
    table = _table(value, where)
    controlled = [
        _controlled_term(item, f"{where}.controlled[{i}]")
        for i, item in enumerate(_list(table.get("controlled", []), f"{where}.controlled"))
    ]
    free = [
        _free_term(item, f"{where}.free[{i}]")
        for i, item in enumerate(_list(table.get("free", []), f"{where}.free"))
    ]
    return Condition(
        label=_string(table.get("label"), f"{where}.label"),
        terms=tuple(controlled + free),
    )


def _component(value: object, where: str, question_format: QuestionFormat) -> Component:
    table = _table(value, where)
    conditions = _list(table.get("conditions", []), f"{where}.conditions")
    # Con conceptos libres el rol es siempre el mismo: escribirlo en cada uno sobra.
    default_role = (
        ComponentRole.CONCEPT.value if question_format is QuestionFormat.CONCEPTS else None
    )
    return Component(
        role=_choice(ComponentRole, table.get("role", default_role), f"{where}.role"),
        description=_string(table.get("description"), f"{where}.description"),
        conditions=tuple(
            _condition(item, f"{where}.conditions[{i}]") for i, item in enumerate(conditions)
        ),
    )


def _options(value: object) -> StrategyOptions:
    table = _table(value, "options")
    known = {"repeat_controlled_as_free", "free_text_unindexed_only"}
    unknown = sorted(set(table) - known)
    if unknown:
        raise QuestionError(f"unknown options: {', '.join(unknown)}")
    return StrategyOptions(
        repeat_controlled_as_free=_bool(
            table.get("repeat_controlled_as_free", False), "options.repeat_controlled_as_free"
        ),
        free_text_unindexed_only=_bool(
            table.get("free_text_unindexed_only", False), "options.free_text_unindexed_only"
        ),
    )


def parse_question(data: Mapping[str, object]) -> tuple[StructuredQuestion, StrategyOptions]:
    """Build a question and its strategy options from already parsed TOML data."""
    question_format = _choice(QuestionFormat, data.get("format"), "format")
    components = _list(data.get("components"), "components")
    exclusions = _list(data.get("exclusions", []), "exclusions")
    discipline = data.get("discipline")
    databases = _list(data.get("databases", []), "databases")
    categories = _list(data.get("arxiv_categories", []), "arxiv_categories")
    question = StructuredQuestion(
        text=_string(data.get("question"), "question"),
        format=question_format,
        components=tuple(
            _component(item, f"components[{i}]", question_format)
            for i, item in enumerate(components)
        ),
        exclusions=tuple(_condition(item, f"exclusions[{i}]") for i, item in enumerate(exclusions)),
        discipline=None if discipline is None else _choice(Discipline, discipline, "discipline"),
        databases=tuple(
            _choice(Database, item, f"databases[{i}]") for i, item in enumerate(databases)
        ),
        arxiv_categories=tuple(
            _string(item, f"arxiv_categories[{i}]") for i, item in enumerate(categories)
        ),
    )
    return question, _options(data.get("options", {}))


def load_question(path: Path) -> tuple[StructuredQuestion, StrategyOptions]:
    """Read a question file; every problem is reported as a :class:`QuestionError`."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise QuestionError(f"cannot read {path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise QuestionError(f"{path} is not valid TOML: {exc}") from exc
    return parse_question(data)


def parse_question_text(text: str) -> tuple[StructuredQuestion, StrategyOptions]:
    """Read a question from the text of a TOML file (e.g. one uploaded to the web interface)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise QuestionError(f"the question file is not valid TOML: {exc}") from exc
    return parse_question(data)


def _term_data(term: Term) -> str | dict[str, str]:
    # La forma corta (solo el texto) es la que se escribe a mano: se usa siempre que basta.
    if term.kind is TermKind.FREE:
        if term.free_field is FreeTextField.TITLE_ABSTRACT:
            return term.text
        return {"term": term.text, "field": term.free_field.value}
    table = {"term": term.text}
    if term.scope is not ControlledScope.HEADING:
        table["scope"] = term.scope.value
    if term.vocabulary is not None and term.vocabulary is not Vocabulary.MESH:
        table["vocabulary"] = term.vocabulary.value
    return term.text if len(table) == 1 else table


def _condition_data(condition: Condition) -> dict[str, object]:
    data: dict[str, object] = {"label": condition.label}
    if condition.controlled_terms:
        data["controlled"] = [_term_data(t) for t in condition.controlled_terms]
    if condition.free_terms:
        data["free"] = [_term_data(t) for t in condition.free_terms]
    return data


def question_to_data(question: StructuredQuestion, options: StrategyOptions) -> dict[str, object]:
    """The question in the same shape :func:`parse_question` reads, without default values."""
    data: dict[str, object] = {"question": question.text, "format": question.format.value}
    if question.discipline is not None:
        data["discipline"] = question.discipline.value
    if question.databases:
        data["databases"] = [database.value for database in question.databases]
    if question.arxiv_categories:
        data["arxiv_categories"] = list(question.arxiv_categories)
    components: list[dict[str, object]] = []
    for component in question.components:
        item: dict[str, object] = {}
        if question.format is not QuestionFormat.CONCEPTS:
            item["role"] = component.role.value
        item["description"] = component.description
        if component.conditions:
            item["conditions"] = [_condition_data(c) for c in component.conditions]
        components.append(item)
    data["components"] = components
    if question.exclusions:
        data["exclusions"] = [_condition_data(c) for c in question.exclusions]
    chosen = {
        "repeat_controlled_as_free": options.repeat_controlled_as_free,
        "free_text_unindexed_only": options.free_text_unindexed_only,
    }
    if any(chosen.values()):
        data["options"] = {key: value for key, value in chosen.items() if value}
    return data


_TOML_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_string(text: str) -> str:
    out: list[str] = []
    for char in text:
        if char in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        pairs = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + pairs + " }"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


# Solo estas listas se escriben como [[tabla]]; los términos con opciones van en línea.
_TABLE_LISTS = frozenset({"components", "conditions", "exclusions"})


def _toml_table(header: str, table: Mapping[str, object], lines: list[str]) -> None:
    nested = {k: v for k, v in table.items() if k in _TABLE_LISTS and isinstance(v, list)}
    lines.extend(f"{k} = {_toml_value(v)}" for k, v in table.items() if k not in nested)
    for key, items in nested.items():
        name = f"{header}.{key}" if header else key
        for item in items:
            lines += ["", f"[[{name}]]"]
            _toml_table(name, _table(item, name), lines)


def dump_question(question: StructuredQuestion, options: StrategyOptions) -> str:
    """Write the question as a TOML question file that :func:`load_question` reads back."""
    data = question_to_data(question, options)
    chosen = data.pop("options", None)
    lines: list[str] = []
    _toml_table("", data, lines)
    if isinstance(chosen, Mapping):
        lines += ["", "[options]"]
        lines += [f"{key} = {_toml_value(value)}" for key, value in chosen.items()]
    return "\n".join(lines) + "\n"
