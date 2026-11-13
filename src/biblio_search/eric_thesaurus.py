"""ERIC Thesaurus descriptors, confirmed through the ERIC API or the official thesaurus file.

The ERIC API has no thesaurus endpoint, so by default a descriptor is confirmed by asking the
API for records indexed with it: a descriptor is real when ERIC uses it on at least
``MIN_RECORDS`` records, written exactly as the records carry it (the synonym «Peer Tutoring»
slipped into the descriptors of one record). That cannot follow synonyms to their
descriptor («Peer Tutoring» USE «Peer Teaching»). The thesaurus ERIC publishes for
download (``ERICThesaurus2025.zip`` at eric.ed.gov/?download) can be used instead by pointing
``ERIC_THESAURUS`` at it: then synonyms lead to their descriptor and the descriptor's own
synonyms are suggested as free text. Dead descriptors are
left out in both cases, so they are searched as free text.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from biblio_search.eric import EricPage
from biblio_search.eric_query import DESCRIPTOR_FIELD, quote
from biblio_search.models import ThesaurusEntry, Vocabulary

VOCABULARY = Vocabulary.ERIC.value
THESAURUS_ENV = "ERIC_THESAURUS"
DOWNLOAD_URL = "https://eric.ed.gov/?download"
# Registros que se miran para encontrar cómo escribe ERIC el descriptor (mayúsculas incluidas).
SAMPLE_ROWS = 50
# Por debajo de esto es más probable un desliz de indexación que un descriptor en uso.
MIN_RECORDS = 5
# Los sinónimos antiguos llevan los años en que fueron descriptores: «Tutorial Instruction
# (1966 1980)». Nadie escribe eso en un título.
_YEARS_SUFFIX = re.compile(r"\s*\(\d{4}(?:\s+\d{4})?\)$")


class ThesaurusFileError(ValueError):
    """Raised when the ERIC thesaurus file cannot be read."""


class EricQuery(Protocol):
    """What the API-backed thesaurus needs from ERIC (EricClient satisfies it)."""

    async def query(self, search: str, *, rows: int, start: int = 0, fields: str = ...) -> EricPage:
        """Run ``search`` and return ``rows`` documents with ``fields``."""


def _folded(text: str) -> str:
    return " ".join(text.split()).casefold()


class EricApiThesaurus:
    """Confirms ERIC descriptors by finding records the API indexes with them."""

    name = VOCABULARY

    def __init__(self, backend: EricQuery) -> None:
        self._backend = backend

    async def lookup(self, texts: Sequence[str]) -> dict[str, ThesaurusEntry | None]:
        found: dict[str, ThesaurusEntry | None] = {}
        for text in texts:
            if _folded(text) not in found:
                found[_folded(text)] = await self._find(text)
        return {text: found[_folded(text)] for text in texts}

    async def _find(self, text: str) -> ThesaurusEntry | None:
        # El campo por palabras no distingue mayúsculas; la forma exacta se lee en los registros.
        page = await self._backend.query(
            f"subject:{quote(' '.join(text.split()))}", rows=SAMPLE_ROWS, fields="subject"
        )
        name = _exact_form(page, _folded(text))
        if name is None:
            return None
        usage = await self._backend.query(f"{DESCRIPTOR_FIELD}:{quote(name)}", rows=0)
        if usage.count < MIN_RECORDS:
            return None
        return ThesaurusEntry(VOCABULARY, "", name)


def _exact_form(page: EricPage, wanted: str) -> str | None:
    for doc in page.docs:
        subjects = doc.get("subject")
        if not isinstance(subjects, list):
            continue
        for subject in subjects:
            if isinstance(subject, str) and _folded(subject) == wanted:
                return " ".join(subject.split())
    return None


@dataclass(frozen=True, slots=True)
class _Record:
    name: str
    kind: str
    scope_note: str
    used_for: tuple[str, ...]
    use: tuple[str, ...]


def _natural(synonym: str) -> str:
    return _YEARS_SUFFIX.sub("", synonym)


class EricThesaurusFile:
    """The ERIC Thesaurus as published for download (XML, or the zip that holds it)."""

    def __init__(self, records: Mapping[str, _Record]) -> None:
        self._records = dict(records)

    @classmethod
    def load(cls, path: Path) -> EricThesaurusFile:
        """Read the thesaurus from ``path`` (``ERICThesaurus2025.zip`` or the XML inside)."""
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
                    if not names:
                        raise ThesaurusFileError(f"{path} holds no XML file")
                    with archive.open(names[0]) as handle:
                        root = ET.parse(handle).getroot()
            else:
                root = ET.parse(path).getroot()
        except (OSError, ET.ParseError, zipfile.BadZipFile) as exc:
            raise ThesaurusFileError(f"cannot read the ERIC thesaurus at {path}: {exc}") from exc
        return cls(_parse(root, path))

    @classmethod
    def from_env(cls) -> EricThesaurusFile | None:
        """The file named by ``ERIC_THESAURUS``, or ``None`` when it is not set."""
        value = os.environ.get(THESAURUS_ENV, "").strip()
        return cls.load(Path(value).expanduser()) if value else None

    def __len__(self) -> int:
        return len(self._records)

    def entry(self, text: str) -> ThesaurusEntry | None:
        """The descriptor ``text`` names, itself or as a synonym, or ``None``."""
        record = self._records.get(_folded(text))
        if record is None or record.kind not in ("Main", "Synonym"):
            return None
        if record.kind == "Synonym":
            targets = [self._records.get(_folded(name)) for name in record.use]
            descriptors = [t for t in targets if t is not None and t.kind == "Main"]
            if not descriptors:
                return None
            first, *others = descriptors
            return self._descriptor(first, tuple(other.name for other in others))
        return self._descriptor(record, ())

    @staticmethod
    def _descriptor(record: _Record, use_with: tuple[str, ...]) -> ThesaurusEntry:
        seen = {record.name.casefold()}
        synonyms: list[str] = []
        for raw in record.used_for:
            synonym = _natural(raw)
            if synonym.casefold() not in seen:
                seen.add(synonym.casefold())
                synonyms.append(synonym)
        return ThesaurusEntry(
            vocabulary=VOCABULARY,
            identifier="",
            name=record.name,
            synonyms=tuple(synonyms),
            scope_note=record.scope_note,
            use_with=use_with,
        )


def _parse(root: ET.Element, path: Path) -> dict[str, _Record]:
    terms = root.find("Terms")
    if terms is None:
        raise ThesaurusFileError(f"{path} is not the ERIC thesaurus: it has no <Terms>")
    records: dict[str, _Record] = {}
    for term in terms.findall("Term"):
        name = " ".join((term.findtext("Name") or "").split())
        if not name:
            continue
        attributes = {a.get("name"): (a.text or "").strip() for a in term.iter("Attribute")}
        relations: dict[str, list[str]] = {}
        for relation in term.iter("Relationship"):
            names = [" ".join((i.text or "").split()) for i in relation.findall("Is")]
            relations.setdefault(relation.get("type", ""), []).extend(n for n in names if n)
        records[name.casefold()] = _Record(
            name=name,
            kind=attributes.get("RecType", ""),
            scope_note=attributes.get("ScopeNote", ""),
            used_for=tuple(relations.get("UF", ())),
            use=tuple(relations.get("U", ())),
        )
    if not records:
        raise ThesaurusFileError(f"{path} is not the ERIC thesaurus: it has no terms")
    return records


class EricFileThesaurus:
    """:class:`Thesaurus` over the downloaded ERIC Thesaurus: no network involved."""

    name = VOCABULARY

    def __init__(self, thesaurus: EricThesaurusFile) -> None:
        self._thesaurus = thesaurus

    async def lookup(self, texts: Sequence[str]) -> dict[str, ThesaurusEntry | None]:
        return {text: self._thesaurus.entry(text) for text in texts}


def describe_source(thesaurus: EricThesaurusFile | None) -> str:
    """Say where ERIC descriptors are confirmed, for the check report."""
    if thesaurus is None:
        return (
            "ERIC descriptors are confirmed through the ERIC API; set ERIC_THESAURUS to the "
            f"thesaurus file ({DOWNLOAD_URL}) to also follow synonyms"
        )
    return f"ERIC descriptors are confirmed in the ERIC Thesaurus file ({len(thesaurus)} terms)"
