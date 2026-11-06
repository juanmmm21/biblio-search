from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from conftest import mesh_fixture_for

from biblio_search.eutils import EutilsError
from biblio_search.mesh import MeshThesaurus, mesh_query, natural_synonyms
from biblio_search.models import ThesaurusEntry


class FakeBackend:
    def __init__(self, overrides: Mapping[str, Mapping[str, object]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.overrides = overrides or {}

    async def get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((endpoint, dict(params)))
        if endpoint in self.overrides:
            return self.overrides[endpoint]
        return mesh_fixture_for(endpoint, params)


def _lookup(*texts: str, backend: FakeBackend | None = None) -> dict[str, ThesaurusEntry | None]:
    return asyncio.run(MeshThesaurus(backend or FakeBackend()).lookup(list(texts)))


def test_mesh_query_quotes_and_validates() -> None:
    assert mesh_query("  Aged,  80 and over ") == '"Aged, 80 and over"[MeSH Terms]'
    with pytest.raises(ValueError, match="empty"):
        mesh_query(" ")
    with pytest.raises(ValueError, match="forbidden"):
        mesh_query('Anxiety"[mh]')


def test_natural_synonyms_drop_inverted_duplicates() -> None:
    terms = [
        "Depressive Symptoms",
        "Symptom, Depressive",
        "Depression, Emotional",
        "Emotional Depression",
    ]
    assert natural_synonyms("Depression", terms) == (
        "Depressive Symptoms",
        "Depressive Symptom",
        "Emotional Depression",
    )
    assert natural_synonyms("Aged", ["Elderly", "elderly", "aged", "A, B, C", "X,"]) == ("Elderly",)


def test_lookup_descriptor_from_recorded_answers() -> None:
    entry = _lookup("Anxiety")["Anxiety"]
    assert entry is not None
    assert entry.vocabulary == "mesh"
    assert entry.identifier == "D001007"
    assert entry.name == "Anxiety"
    assert entry.has_narrower is True
    assert "Social Anxiety" in entry.synonyms
    assert "Anxiety, Social" not in entry.synonyms
    assert entry.scope_note.startswith("Feelings or emotions of dread")


def test_lookup_entry_term_points_to_its_descriptor() -> None:
    entry = _lookup("carers")["carers"]
    assert entry is not None
    assert entry.name == "Caregivers"
    assert entry.has_narrower is False


def test_lookup_heading_with_commas_and_unknown_text() -> None:
    found = _lookup("Aged, 80 and over", "Qwzxvplorkt")
    aged = found["Aged, 80 and over"]
    assert aged is not None and aged.identifier == "D000369"
    assert aged.synonyms == ("Oldest Old",)
    assert found["Qwzxvplorkt"] is None


def test_lookup_ignores_qualifiers() -> None:
    entry = _lookup("Nursing")["Nursing"]
    assert entry is not None
    assert entry.identifier == "D009729"
    assert entry.name == "Nursing"


def test_lookup_asks_once_per_text_and_batches_summaries() -> None:
    backend = FakeBackend()
    found = _lookup("Anxiety", "anxiety", "Carers", "Caregivers", backend=backend)
    assert set(found) == {"Anxiety", "anxiety", "Carers", "Caregivers"}
    searches = [p["term"] for e, p in backend.calls if e == "esearch.fcgi"]
    assert searches == ['"Anxiety"[MeSH Terms]', '"Carers"[MeSH Terms]', '"Caregivers"[MeSH Terms]']
    summaries = [p["id"] for e, p in backend.calls if e == "esummary.fcgi"]
    assert summaries == ["68001007,68017028"]


def test_lookup_without_matches_skips_esummary() -> None:
    backend = FakeBackend()
    assert _lookup("Qwzxvplorkt", backend=backend) == {"Qwzxvplorkt": None}
    assert [e for e, _ in backend.calls] == ["esearch.fcgi"]


def test_lookup_rejects_malformed_descriptor() -> None:
    broken = {"result": {"uids": ["68001007"], "68001007": {"ds_recordtype": "descriptor"}}}
    backend = FakeBackend({"esummary.fcgi": broken})
    with pytest.raises(EutilsError, match="no name or UI"):
        _lookup("Anxiety", backend=backend)
