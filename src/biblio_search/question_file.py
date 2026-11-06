"""Loading structured questions from TOML files."""

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
