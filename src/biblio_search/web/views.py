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
from biblio_search.decs import LICENCE_URL
from biblio_search.models import (
    ArticleSummary,
    Condition,
    Database,
    SearchResult,
    StrategyOptions,
    StructuredQuestion,
    Term,
    TermKind,
    TermStatus,
    Vocabulary,
)
from biblio_search.question_file import question_to_data
from biblio_search.screening import AddedSearch, FlowSummary, ScreeningEntry
from biblio_search.search_log import SearchLog
from biblio_search.selection import DatabaseSelection, select_databases
from biblio_search.snowball import SnowballRun
from biblio_search.tuning import TuningReport

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


def check_view(result: QuestionCheck, *, decs: bool = False, source: str | None = None) -> JsonDict:
    """One thesaurus check; ``source`` says where the headings were confirmed."""
    # Sin clave de DeCS, un término que no está en MeSH puede ser uno en castellano: se dice
    # cómo traducirlo en vez de dejarlo solo como «no encontrado».
    missing = any(check.status is TermStatus.NOT_FOUND for check in result.checks)
    decs_hint = (
        {
            "text": "Spanish headings are looked up in DeCS when DECS_API_KEY is set.",
            "url": LICENCE_URL,
        }
        if missing and not decs and result.vocabulary == Vocabulary.MESH
        else None
    )
    return {
        "decs": decs,
        "decs_hint": decs_hint,
        "source": source,
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


def tuning_view(database: Database, report: TuningReport) -> JsonDict:
    """Counts, levers and advice of one database; each lever carries the question it makes,
    so the page can load it into the form."""
    return {
        "database": database.value,
        "label": DATABASE_LABELS[database],
        "equation": report.equation,
        "total": report.total,
        "conditions": [
            {"label": c.label, "equation": c.equation, "alone": c.alone, "without": c.without}
            for c in report.conditions
        ],
        "without_exclusions": report.without_exclusions,
        "variants": [
            {
                "key": v.key,
                "description": v.description,
                "direction": v.direction.value,
                "equation": v.equation,
                "count": v.count,
                "question": question_to_data(v.variant.question, v.variant.options),
            }
            for v in report.variants
        ],
        "advice": [
            {"kind": a.kind.value, "message": a.message, "suggestions": list(a.suggestions)}
            for a in report.advice
        ],
    }


def merged_view(search_id: str, log: SearchLog) -> JsonDict:
    """How many records the search kept after merging, and where to download them."""
    merged = log.merged
    return {
        "search_id": search_id,
        "searched_at": log.searched_at.isoformat(timespec="seconds"),
        "identified": merged.identified,
        "duplicates": merged.duplicates,
        "unique": len(merged.records),
        "downloads": {kind: f"/api/searches/{search_id}/{kind}" for kind in ("log", "ris", "csv")},
    }


def flow_view(summary: FlowSummary) -> JsonDict:
    """The counts of the screening flow diagram."""
    return {
        "searches": summary.searches,
        "identified": summary.identified,
        "duplicates": summary.duplicates,
        "screened": summary.screened,
        "pending": summary.pending,
        "included": summary.included,
        "excluded": summary.excluded,
        "maybe": summary.maybe,
        "snowball_rounds": summary.snowball_rounds,
        "citation_identified": summary.citation_identified,
        "exclusion_reasons": [
            {"reason": reason, "count": count} for reason, count in summary.exclusion_reasons
        ],
    }


def entry_view(entry: ScreeningEntry) -> JsonDict:
    """A record to screen, with every database it was found in and its decision."""
    record = entry.record
    return {
        "id": entry.entry_id,
        "title": record.title,
        "authors": list(record.authors),
        "journal": record.journal,
        "year": record.year,
        "doi": record.doi,
        "publication_types": list(record.publication_types),
        "sources": [
            {
                "database": source.database.value,
                "label": DATABASE_LABELS[source.database],
                "id_label": RECORD_ID_LABELS.get(source.database, DATABASE_LABELS[source.database]),
                "id": source.record_id,
                "url": source.url,
            }
            for source in entry.sources
        ],
        "decision": entry.decision.value if entry.decision is not None else None,
        "reason": entry.reason,
    }


def added_view(added: AddedSearch) -> JsonDict:
    return {
        "search_number": added.search_number,
        "identified": added.identified,
        "new_records": added.new_records,
        "already_known": added.already_known,
    }


def snowball_view(run: SnowballRun) -> JsonDict:
    """What a snowballing round followed, what it left out and how much it reached."""
    return {
        "directions": [direction.value for direction in run.directions],
        "max_citing": run.max_citing,
        "seeds": [
            {
                "id": seed.entry_id,
                "title": seed.title,
                "work_id": seed.work_id,
                "references": seed.references,
                "citing_total": seed.citing_total,
                "citing_kept": seed.citing_kept,
            }
            for seed in run.seeds
        ],
        "unmatched": [
            {"id": seed.entry_id, "title": seed.title, "reason": seed.reason}
            for seed in run.unmatched
        ],
        "candidates": len(run.candidates),
        "missing_references": run.missing_references,
    }
