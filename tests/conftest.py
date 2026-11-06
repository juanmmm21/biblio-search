from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Mapping[str, object]:
    data: object = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # Los tests nunca hablan con NCBI: cualquier conexión real es un fallo del test.
    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


MESH_SEARCH_FIXTURES = {
    '"anxiety"[mesh terms]': "mesh_esearch_anxiety.json",
    '"depression"[mesh terms]': "mesh_esearch_depression.json",
    '"caregivers"[mesh terms]': "mesh_esearch_caregivers.json",
    '"aged"[mesh terms]': "mesh_esearch_aged.json",
    '"carers"[mesh terms]': "mesh_esearch_carers.json",
    '"aged, 80 and over"[mesh terms]': "mesh_esearch_aged_80.json",
    '"nursing"[mesh terms]': "mesh_esearch_nursing.json",
}


def mesh_fixture_for(endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
    """Recorded NCBI answer for a ``db=mesh`` call; unknown headings get the not-found one."""
    assert params["db"] == "mesh"
    if endpoint == "esummary.fcgi":
        return load_fixture("mesh_esummary.json")
    assert endpoint == "esearch.fcgi"
    name = MESH_SEARCH_FIXTURES.get(params["term"].casefold(), "mesh_esearch_not_found.json")
    return load_fixture(name)
