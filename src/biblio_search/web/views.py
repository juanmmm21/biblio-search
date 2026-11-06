"""JSON views of the domain objects the web interface draws.

They are plain dictionaries built from the same functions the CLI uses, so the page shows
exactly the equations and reasons the command line would print.
"""

from __future__ import annotations

from collections.abc import Collection

from biblio_search.catalog import (
    DATABASE_LABELS,
    DIALECTS,
    RECORD_ID_LABELS,
    VOCABULARY_LABELS,
    dialect_notes,
)
from biblio_search.check import QuestionCheck, scope_note
from biblio_search.models import (
    ArticleSummary,
    Condition,
    Database,
    SearchResult,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermKind,
)
from biblio_search.selection import DatabaseSelection, select_databases

JsonDict = dict[str, object]


def term_view(term: Term) -> JsonDict:
    if term.kind is TermKind.CONTROLLED:
        vocabulary = term.vocabulary.value if term.vocabulary is not None else ""
        return {
            "text": term.text,
            "kind": term.kind.value,
            "vocabulary": vocabulary,
            "vocabulary_label": VOCABULARY_LABELS.get(vocabulary, vocabulary),
            "scope": term.scope.value,
        }
    return {"text": term.text, "kind": term.kind.value, "field": term.free_field.value}


def condition_view(condition: Condition) -> JsonDict:
    return {"label": condition.label, "terms": [term_view(t) for t in condition.terms]}


def tree_view(question: StructuredQuestion) -> JsonDict:
    """The question broken down as the user wrote it: components, conditions and terms."""
    return {
        "text": question.text,
        "format": question.format.value,
        "components": [
            {
                "role": component.role.value,
                "description": component.description,
                "conditions": [condition_view(c) for c in component.conditions],
            }
            for component in question.components
        ],
        "exclusions": [condition_view(c) for c in question.exclusions],
    }


def grid_view(question: StructuredQuestion) -> JsonDict:
    """Del Pino's logic grid: one row per condition (AND between rows, OR inside a row)."""
    return {
        "rows": [condition_view(c) for c in question.conditions],
        "exclusions": [condition_view(c) for c in question.exclusions],
    }


def selection_view(selection: DatabaseSelection) -> JsonDict:
    return {
        "mode": selection.mode.value,
        "choices": [
            {
                "database": choice.database.value,
                "label": DATABASE_LABELS[choice.database],
                "reasons": list(choice.reasons),
                "searchable": choice.searchable,
            }
            for choice in selection.choices
        ],
    }


def equation_view(
    database: Database, question: StructuredQuestion, options: StrategyOptions
) -> JsonDict:
    dialect = DIALECTS[database]
    return {
        "database": database.value,
        "label": DATABASE_LABELS[database],
        "equation": dialect.render(question, options),
        "notes": list(dialect_notes(question, dialect)),
    }


def preview_view(
    question: StructuredQuestion,
    options: StrategyOptions,
    searchable: Collection[Database],
) -> JsonDict:
    """Everything the page draws while the question is being written."""
    selection = select_databases(question, searchable)
    return {
        "tree": tree_view(question),
        "grid": grid_view(question),
        "selection": selection_view(selection),
        "equations": [
            equation_view(database, question, options)
            for database in selection.databases
            if database in DIALECTS
        ],
    }


def count_view(database: Database, result: SearchResult) -> JsonDict:
    return {
        "database": database.value,
        "label": DATABASE_LABELS[database],
        "count": result.count,
        "translation": result.query_translation,
        "not_found": list(result.not_found),
        "warnings": list(result.warnings),
    }


def record_view(database: Database, record: ArticleSummary) -> JsonDict:
    return {
        "id": record.record_id,
        "id_label": RECORD_ID_LABELS[database],
        "title": record.title,
        "authors": list(record.authors),
        "journal": record.journal,
        "year": record.year,
        "doi": record.doi,
        "publication_types": list(record.publication_types),
        "url": record.url,
    }


def check_view(result: QuestionCheck) -> JsonDict:
    return {
        "vocabulary": result.vocabulary,
        "vocabulary_label": VOCABULARY_LABELS.get(result.vocabulary, result.vocabulary),
        "checks": [
            {
                "condition": check.condition,
                "term": check.term.text,
                "status": check.status.value,
                "heading": check.entry.name if check.entry is not None else None,
                "identifier": check.entry.identifier if check.entry is not None else None,
                "note": scope_note(check),
            }
            for check in result.checks
        ],
        "unchecked": [term_view(term) for term in result.unchecked],
        "suggestions": [
            {"condition": item.condition.label, "synonyms": list(item.suggested_synonyms)}
            for item in result.conditions
            if item.suggested_synonyms
        ],
    }
