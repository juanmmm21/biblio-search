"""Merged records as files for reference managers (RIS) and spreadsheets (CSV)."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence

from biblio_search.catalog import DATABASE_LABELS, RECORD_ID_LABELS
from biblio_search.merge import MergedRecord

# Tipos RIS según el tipo de publicación de la base; lo que no se reconoce es un artículo.
_RIS_TYPES = (
    ("preprint", "UNPB"),
    ("book-chapter", "CHAP"),
    ("book", "BOOK"),
    ("dissertation", "THES"),
    ("dataset", "DATA"),
)
CSV_COLUMNS = (
    "title",
    "authors",
    "journal",
    "year",
    "doi",
    "pmid",
    "databases",
    "ids",
    "url",
    "publication_types",
)
# Una celda que empieza por estos caracteres es una fórmula para Excel y LibreOffice.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def ris_type(record: MergedRecord) -> str:
    types = " ".join(record.record.publication_types).casefold()
    return next((code for word, code in _RIS_TYPES if word in types), "JOUR")


def _source_ids(record: MergedRecord) -> str:
    return "; ".join(f"{RECORD_ID_LABELS[s.database]} {s.record_id}" for s in record.sources)


def to_ris(records: Sequence[MergedRecord]) -> str:
    """RIS, the format Zotero, Mendeley and EndNote import; one entry per merged record."""
    lines: list[str] = []
    for merged in records:
        record = merged.record
        entry = [("TY", ris_type(merged)), ("TI", record.title)]
        entry += [("AU", author) for author in record.authors]
        if record.journal:
            entry.append(("T2", record.journal))
        if record.year is not None:
            entry.append(("PY", str(record.year)))
        if record.doi:
            entry.append(("DO", record.doi))
        entry.append(("UR", record.url))
        entry += [("KW", kind) for kind in record.publication_types]
        entry.append(("DB", ", ".join(DATABASE_LABELS[d] for d in merged.databases)))
        entry.append(("N1", f"Found in: {_source_ids(merged)}"))
        lines += [f"{tag}  - {_one_line(value)}" for tag, value in entry if _one_line(value)]
        lines += ["ER  - ", ""]
    # La especificación de RIS pide CRLF al final de cada línea.
    return "\r\n".join(lines)


def _cell(text: str) -> str:
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def to_csv(records: Sequence[MergedRecord]) -> str:
    """CSV with one row per merged record, starting with a BOM so Excel reads it as UTF-8."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for merged in records:
        record = merged.record
        row = (
            record.title,
            "; ".join(record.authors),
            record.journal,
            "" if record.year is None else str(record.year),
            record.doi or "",
            record.pmid or "",
            "; ".join(DATABASE_LABELS[d] for d in merged.databases),
            _source_ids(merged),
            record.url,
            "; ".join(record.publication_types),
        )
        writer.writerow([_cell(_one_line(value)) for value in row])
    return "﻿" + buffer.getvalue()
