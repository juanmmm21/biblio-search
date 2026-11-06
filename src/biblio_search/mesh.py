"""MeSH thesaurus looked up through NCBI E-utilities (``db=mesh``).

A text is searched as ``"text"[MeSH Terms]``, which in the MeSH database matches the
descriptor name and any of its entry terms exactly (case-insensitive), so «Carers» finds
the descriptor «Caregivers». Qualifiers (subheadings) share that index and are discarded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from biblio_search.eutils import SUMMARY_BATCH_SIZE, EutilsError, parse_search
from biblio_search.models import ThesaurusEntry

VOCABULARY = "mesh"
# Un texto casa con un descriptor y, como mucho, con un calificador del mismo nombre.
_MAX_MATCHES = 20
_FORBIDDEN_CHARS = frozenset('"[]')


class JsonBackend(Protocol):
    """What the thesaurus needs from NCBI (EutilsClient satisfies it)."""

    async def get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
        """Call an E-utility and return its decoded JSON object."""


def mesh_query(text: str) -> str:
    """Return the esearch term that finds the descriptor named (or entry term) ``text``."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("cannot look up an empty heading")
    bad = _FORBIDDEN_CHARS.intersection(cleaned)
    if bad:
        raise ValueError(f"heading {cleaned!r} contains forbidden characters: {sorted(bad)}")
    return f'"{cleaned}"[MeSH Terms]'


def natural_synonyms(name: str, entry_terms: Sequence[str]) -> tuple[str, ...]:
    """Turn MeSH entry terms into synonyms an author would write.

    Inverted forms («Depression, Emotional») are put back in natural order and dropped when
    that form is already listed; terms with more than one comma have no natural order.
    """
    seen = {name.casefold()}
    synonyms: list[str] = []
    for raw in entry_terms:
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) > 2 or not all(parts):
            continue
        candidate = " ".join(reversed(parts))
        key = candidate.casefold()
        if key not in seen:
            seen.add(key)
            synonyms.append(candidate)
    return tuple(synonyms)


@dataclass(frozen=True, slots=True)
class _Descriptor:
    entry: ThesaurusEntry
    terms: frozenset[str]


def _as_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EutilsError(f"unexpected NCBI response: {where} is not an object")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _has_narrower(record: Mapping[str, object]) -> bool:
    links = record.get("ds_idxlinks")
    if not isinstance(links, list):
        return False
    return any(isinstance(link, Mapping) and _strings(link.get("children")) for link in links)


def _parse(payload: Mapping[str, object]) -> dict[str, _Descriptor]:
    result = _as_mapping(payload.get("result"), "result")
    descriptors: dict[str, _Descriptor] = {}
    for uid in _strings(result.get("uids")):
        record = _as_mapping(result.get(uid), f"record {uid}")
        if record.get("ds_recordtype") != "descriptor":
            continue
        terms = _strings(record.get("ds_meshterms"))
        identifier = record.get("ds_meshui")
        if not terms or not isinstance(identifier, str) or not identifier:
            raise EutilsError(f"unexpected NCBI response: MeSH record {uid} has no name or UI")
        scope_note = record.get("ds_scopenote")
        name = terms[0]
        descriptors[uid] = _Descriptor(
            entry=ThesaurusEntry(
                vocabulary=VOCABULARY,
                identifier=identifier,
                name=name,
                synonyms=natural_synonyms(name, terms[1:]),
                has_narrower=_has_narrower(record),
                scope_note=scope_note.strip() if isinstance(scope_note, str) else "",
            ),
            terms=frozenset(term.casefold() for term in terms),
        )
    return descriptors


class MeshThesaurus:
    """Confirms MeSH descriptors against NCBI: one esearch per text, esummary in batches."""

    name = VOCABULARY

    def __init__(self, backend: JsonBackend) -> None:
        self._backend = backend

    async def lookup(self, texts: Sequence[str]) -> dict[str, ThesaurusEntry | None]:
        queries = {text: mesh_query(text) for text in texts}
        uids_by_query: dict[str, tuple[str, ...]] = {}
        for query in dict.fromkeys(q.casefold() for q in queries.values()):
            payload = await self._backend.get_json(
                "esearch.fcgi", {"db": "mesh", "term": query, "retmax": str(_MAX_MATCHES)}
            )
            uids_by_query[query] = parse_search(payload).ids
        uids = list(dict.fromkeys(uid for ids in uids_by_query.values() for uid in ids))
        descriptors: dict[str, _Descriptor] = {}
        for start in range(0, len(uids), SUMMARY_BATCH_SIZE):
            batch = uids[start : start + SUMMARY_BATCH_SIZE]
            payload = await self._backend.get_json(
                "esummary.fcgi", {"db": "mesh", "id": ",".join(batch)}
            )
            descriptors.update(_parse(payload))
        return {
            text: _pick(
                text, [descriptors[u] for u in uids_by_query[query.casefold()] if u in descriptors]
            )
            for text, query in queries.items()
        }


def _pick(text: str, candidates: list[_Descriptor]) -> ThesaurusEntry | None:
    # El índice [MeSH Terms] casa términos exactos; si saliera más de un descriptor, manda
    # el que lista el texto entre sus términos de entrada.
    folded = " ".join(text.split()).casefold()
    for candidate in candidates:
        if folded in candidate.terms:
            return candidate.entry
    return candidates[0].entry if candidates else None
