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
