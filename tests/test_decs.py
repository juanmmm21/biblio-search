from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable, Mapping

import httpx
import pytest
from conftest import load_fixture
from test_mesh import FakeBackend

from biblio_search.decs import (
    DecsClient,
    DecsConfig,
    DecsDescriptor,
    DecsError,
    DecsMeshThesaurus,
    fold,
    parse_descriptors,
    suggest_mesh,
)
from biblio_search.mesh import MeshThesaurus
from biblio_search.models import DatabaseError, ThesaurusEntry
from biblio_search.ratelimit import RateLimiter

MUSCLES = DecsDescriptor(
    decs_id="9324",
    mesh_id="D009132",
    english="Muscles",
    spanish=("Músculos", "músculos"),
    synonyms=("Tejido Muscular",),
)


def test_config_reads_the_key_from_the_environment() -> None:
    assert DecsConfig.from_env({}) is None
    assert DecsConfig.from_env({"DECS_API_KEY": ""}) is None
    config = DecsConfig.from_env({"DECS_API_KEY": "abc123"})
    assert config is not None and config.api_key == "abc123"
    with pytest.raises(ValueError, match="spaces"):
        DecsConfig(api_key="abc 123")
    with pytest.raises(ValueError, match="negative"):
        DecsConfig(api_key="k", max_retries=-1)


def test_fold_ignores_case_accents_and_spaces() -> None:
    assert fold("  Músculos ") == fold("musculos") == "musculos"
    assert fold("Tejido   Muscular") == "tejido muscular"
    assert MUSCLES.names("TEJIDO MUSCULAR") and MUSCLES.names("musculos")
    assert not MUSCLES.names("musculo")


def test_parse_descriptors_from_the_official_server() -> None:
    assert parse_descriptors(load_fixture("decs_words_musculos.json")) == (MUSCLES,)


def test_parse_descriptors_skips_qualifiers_and_repeats() -> None:
    payload = load_fixture("decs_words_with_qualifier.json")
    objects = payload["objects"]
    assert isinstance(objects, list)
    # Un descriptor con dos números de árbol llega dos veces: se queda una.
    repeated = {"objects": [*objects, objects[0]]}
    assert parse_descriptors(repeated) == (MUSCLES,)
    assert parse_descriptors(load_fixture("decs_words_empty.json")) == ()


def _muscles_record() -> dict[str, object]:
    payload = copy.deepcopy(dict(load_fixture("decs_words_musculos.json")))
    objects = payload["objects"]
    assert isinstance(objects, list)
    record: dict[str, object] = objects[0]["decsws_response"]["record_list"]["record"]
    return record


def test_parse_descriptors_skips_decs_only_headings_and_rejects_broken_ones() -> None:
    record = _muscles_record()
    record["unique_identifier_nlm"] = ""
    wrapped = {"objects": [{"decsws_response": {"record_list": {"record": record}}}]}
    assert parse_descriptors(wrapped) == ()
    record = _muscles_record()
    record["descriptor_list"] = [{"attr": {"lang": "es"}, "descriptor": "Músculos"}]
    wrapped = {"objects": [{"decsws_response": {"record_list": {"record": record}}}]}
    with pytest.raises(DecsError, match="no English name"):
        parse_descriptors(wrapped)
    with pytest.raises(DecsError, match="response"):
        parse_descriptors({"objects": [{"other": 1}]})


async def _no_sleep(_: float) -> None:
    return None


Handler = Callable[[httpx.Request], httpx.Response]


def _run_client(
    handler: Handler, call: Callable[[DecsClient], Awaitable[object]], retries: int = 2
) -> object:
    async def go() -> object:
        config = DecsConfig(api_key="secret-key", max_retries=retries, backoff_seconds=0)
        async with DecsClient(
            config,
            transport=httpx.MockTransport(handler),
            limiter=RateLimiter(1000, sleep=_no_sleep),
            sleep=_no_sleep,
        ) as client:
            return await call(client)

    return asyncio.run(go())


def test_search_words_sends_the_key_in_the_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("decs_words_musculos.json"))

    found = _run_client(handler, lambda c: c.search_words("  tejido   muscular "))
    assert found == (MUSCLES,)
    request = seen[0]
    assert request.url.path == "/decs/v2/search-by-words"
    assert dict(request.url.params) == {"words": "tejido muscular", "lang": "es", "format": "json"}
    assert request.headers["apikey"] == "secret-key"
    assert "secret-key" not in str(request.url)
    with pytest.raises(ValueError, match="empty"):
        _run_client(handler, lambda c: c.search_words(" "))


def test_not_found_is_an_empty_result() -> None:
    found = _run_client(lambda _: httpx.Response(404), lambda c: c.search_words("qwzx"))
    assert found == ()


def test_bad_key_says_where_to_get_one() -> None:
    with pytest.raises(DecsError, match=r"DECS_API_KEY.*decs\.bvsalud\.org") as error:
        _run_client(lambda _: httpx.Response(401), lambda c: c.search_words("musculos"))
    assert isinstance(error.value, DatabaseError)


def test_retries_server_errors_then_gives_up() -> None:
    answers = [httpx.Response(503), httpx.Response(200, json={"objects": []})]
    assert _run_client(lambda _: answers.pop(0), lambda c: c.search_words("x")) == ()
    with pytest.raises(DecsError, match="3 attempts: HTTP 502"):
        _run_client(lambda _: httpx.Response(502), lambda c: c.search_words("x"))

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(DecsError, match="network error: ConnectError"):
        _run_client(broken, lambda c: c.search_words("x"), retries=0)
    with pytest.raises(DecsError, match="rejected the request"):
        _run_client(lambda _: httpx.Response(400), lambda c: c.search_words("x"))
    with pytest.raises(DecsError, match="JSON"):
        _run_client(lambda _: httpx.Response(200, text="<xml/>"), lambda c: c.search_words("x"))


class FakeDecs:
    def __init__(self, answers: Mapping[str, tuple[DecsDescriptor, ...]]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def search_words(self, words: str) -> tuple[DecsDescriptor, ...]:
        self.asked.append(words)
        return self.answers.get(fold(words), ())


SKELETAL = DecsDescriptor("9999", "D018482", "Muscle, Skeletal", ("Músculo Esquelético",))


def test_suggestions_are_confirmed_in_mesh_and_exact_ones_go_first() -> None:
    decs = FakeDecs({"musculos": (SKELETAL, MUSCLES)})
    found = asyncio.run(suggest_mesh("Músculos", decs, MeshThesaurus(FakeBackend())))
    assert [(s.descriptor.english, s.exact) for s in found] == [
        ("Muscles", True),
        ("Muscle, Skeletal", False),
    ]
    muscles = found[0].entry
    assert muscles is not None
    assert (muscles.identifier, muscles.name, muscles.translated_from) == (
        "D009132",
        "Muscles",
        "decs",
    )
    # Las grabaciones de NCBI no traen «Muscle, Skeletal»: sin confirmar, no hay entrada.
    assert found[1].entry is None
    assert asyncio.run(suggest_mesh("qwzx", decs, MeshThesaurus(FakeBackend()))) == ()


def test_bridge_asks_decs_only_for_what_mesh_does_not_know() -> None:
    decs = FakeDecs({"musculos": (MUSCLES,), "tejido muscular": (MUSCLES,), "musculo": (MUSCLES,)})
    thesaurus = DecsMeshThesaurus(MeshThesaurus(FakeBackend()), decs)
    found = asyncio.run(
        thesaurus.lookup(["Anxiety", "músculos", "Tejido Muscular", "músculo", "qwzx"])
    )
    anxiety = found["Anxiety"]
    assert anxiety is not None and anxiety.translated_from == ""
    for text in ("músculos", "Tejido Muscular"):
        entry = found[text]
        assert entry is not None
        assert (entry.name, entry.translated_from) == ("Muscles", "decs")
    # «músculo» comparte palabras con «Músculos», pero no es ninguno de sus nombres.
    assert found["músculo"] is None and found["qwzx"] is None
    assert decs.asked == ["músculos", "Tejido Muscular", "músculo", "qwzx"]


def test_bridge_only_translates_into_mesh() -> None:
    class Eric:
        name = "eric"

        async def lookup(self, texts: object) -> dict[str, ThesaurusEntry | None]:
            return {}

    with pytest.raises(ValueError, match="not into eric"):
        DecsMeshThesaurus(Eric(), FakeDecs({}))
