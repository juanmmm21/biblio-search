"""DeCS (BIREME/PAHO/WHO): Spanish health terms that lead to MeSH descriptors.

DeCS is MeSH translated and extended, and every DeCS descriptor that comes from MeSH keeps
its NLM unique identifier, so a Spanish text such as «Cuidadores» leads to the descriptor
«Caregivers». The DeCS API is free but licensed: each user requests their own key
(``DECS_API_KEY``) through BIREME's form, so biblio-search works without it and only uses
DeCS when the key is set. https://decs.bvsalud.org/en/for-developers/

The heading DeCS proposes is never trusted as is: its English name is confirmed in MeSH
through NCBI, like any other heading.
"""

from __future__ import annotations

import asyncio
import os
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Protocol, Self

import httpx

from biblio_search.models import DatabaseError, ThesaurusEntry, Vocabulary
from biblio_search.protocols import Thesaurus
from biblio_search.ratelimit import RateLimiter

DEFAULT_BASE_URL = "https://api.bvsalud.org/decs/v2/"
LICENCE_URL = "https://decs.bvsalud.org/en/for-developers/"
SOURCE = "decs"
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# DeCS no publica un límite de peticiones: se va despacio para no abusar de la licencia.
REQUESTS_PER_SECOND = 2.0
# Los descriptores de DeCS que vienen de MeSH llevan su UI de la NLM (D…); los calificadores
# (Q…) y los descriptores propios de DeCS (sin UI) no tienen equivalente en MeSH.
_MESH_DESCRIPTOR_PREFIX = "D"


class DecsError(DatabaseError):
    """Raised when DeCS cannot be reached or rejects a request."""


@dataclass(frozen=True, slots=True)
class DecsConfig:
    """Connection settings; ``api_key`` is the user's own DeCS licence key."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.api_key or any(c.isspace() for c in self.api_key):
            raise ValueError("api_key must not be empty or contain spaces")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.timeout_seconds <= 0 or self.backoff_seconds < 0:
            raise ValueError("timeout must be positive and backoff non-negative")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DecsConfig | None:
        """Read ``DECS_API_KEY``; without it (or with it empty) there is no DeCS."""
        source = os.environ if env is None else env
        key = source.get("DECS_API_KEY")
        return cls(api_key=key) if key else None


@dataclass(frozen=True, slots=True)
class DecsDescriptor:
    """A DeCS descriptor that comes from MeSH, with its names in Spanish and English.

    ``spanish`` holds the Latin American and Spanish (es-es) names; ``synonyms`` are its
    Spanish entry terms.
    """

    decs_id: str
    mesh_id: str
    english: str
    spanish: tuple[str, ...]
    synonyms: tuple[str, ...] = ()

    def names(self, text: str) -> bool:
        """Whether ``text`` is one of its Spanish names or synonyms (ignoring case and accents)."""
        key = fold(text)
        return any(fold(name) == key for name in self.spanish + self.synonyms)


def fold(text: str) -> str:
    """Compare Spanish terms the way a user types them: no case, no accents, single spaces."""
    decomposed = unicodedata.normalize("NFKD", " ".join(text.split()).casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _as_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DecsError(f"unexpected DeCS response: {where} is not an object")
    return value


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _lang(item: Mapping[str, object]) -> str:
    attr = item.get("attr")
    return _as_str(attr.get("lang")) if isinstance(attr, Mapping) else ""


def _parse_record(record: Mapping[str, object]) -> DecsDescriptor | None:
    mesh_id = _as_str(record.get("unique_identifier_nlm"))
    if not mesh_id.startswith(_MESH_DESCRIPTOR_PREFIX):
        return None
    attr = record.get("attr")
    decs_id = _as_str(attr.get("mfn")) if isinstance(attr, Mapping) else ""
    names: dict[str, str] = {}
    for item in _as_list(record.get("descriptor_list")):
        if isinstance(item, Mapping) and (name := _as_str(item.get("descriptor"))):
            names.setdefault(_lang(item), name)
    english = names.get("en", "")
    if not english or not decs_id:
        raise DecsError(f"unexpected DeCS response: descriptor {mesh_id} has no English name")
    spanish = tuple(dict.fromkeys(n for lang in ("es", "es-es") if (n := names.get(lang, ""))))
    synonyms = tuple(
        dict.fromkeys(
            synonym
            for item in _as_list(record.get("synonym_list"))
            if isinstance(item, Mapping) and (synonym := _as_str(item.get("synonym")))
        )
    )
    return DecsDescriptor(decs_id, mesh_id, english, spanish, synonyms)


def parse_descriptors(payload: Mapping[str, object]) -> tuple[DecsDescriptor, ...]:
    """Turn a DeCS word search into the MeSH descriptors it found, once each, in order.

    DeCS answers with one object per tree number of a descriptor, so the same descriptor can
    come several times.
    """
    found: dict[str, DecsDescriptor] = {}
    for item in _as_list(payload.get("objects")):
        response = _as_mapping(_as_mapping(item, "object").get("decsws_response"), "response")
        record_list = _as_mapping(response.get("record_list"), "record_list")
        descriptor = _parse_record(_as_mapping(record_list.get("record"), "record"))
        if descriptor is not None:
            found.setdefault(descriptor.decs_id, descriptor)
    return tuple(found.values())


class DecsSearch(Protocol):
    """What the Spanish bridge needs from DeCS (DecsClient satisfies it)."""

    async def search_words(self, words: str) -> tuple[DecsDescriptor, ...]:
        """Return the MeSH descriptors whose Spanish name or synonym has all ``words``."""


class DecsClient:
    """DeCS API client. Use as ``async with DecsClient(config) as client: ...``."""

    def __init__(
        self,
        config: DecsConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._limiter = limiter or RateLimiter(REQUESTS_PER_SECOND)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            # La clave va en la cabecera que pide la API, nunca en la URL.
            headers={
                "User-Agent": "biblio-search (+https://github.com/juanmmm21/biblio-search)",
                "apikey": config.api_key,
            },
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search_words(self, words: str) -> tuple[DecsDescriptor, ...]:
        """Search ``words`` (joined with AND by DeCS) among Spanish names and synonyms."""
        cleaned = " ".join(words.split())
        if not cleaned:
            raise ValueError("cannot look up an empty term")
        payload = await self.get_json(
            "search-by-words", {"words": cleaned, "lang": "es", "format": "json"}
        )
        return parse_descriptors(payload)

    async def get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
        """Call ``endpoint`` with retries on rate limits, server errors and network errors.

        The public API answers an empty ``objects`` list when nothing matches; the server code
        documents a 404 for that too, so a 404 is also an empty result and not an error.
        """
        last_problem = ""
        for attempt in range(self._config.max_retries + 1):
            await self._limiter.acquire()
            try:
                response = await self._http.get(endpoint, params=params)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                status = response.status_code
                if status == 404:
                    return {"objects": []}
                if status in (401, 403):
                    raise DecsError(
                        f"DeCS rejected the API key (HTTP {status}); check DECS_API_KEY or "
                        f"request a free licence at {LICENCE_URL}"
                    )
                if status in _RETRY_STATUSES:
                    last_problem = f"HTTP {status}"
                elif status >= 400:
                    raise DecsError(f"DeCS rejected the request (HTTP {status})")
                else:
                    return self._decode(response)
            if attempt == self._config.max_retries:
                break
            await self._sleep(self._config.backoff_seconds * 2.0**attempt)
        raise DecsError(
            f"DeCS {endpoint} failed after {self._config.max_retries + 1} attempts: {last_problem}"
        )

    @staticmethod
    def _decode(response: httpx.Response) -> Mapping[str, object]:
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise DecsError("DeCS did not return JSON") from exc
        return _as_mapping(payload, "response")


@dataclass(frozen=True, slots=True)
class DecsSuggestion:
    """A MeSH descriptor DeCS proposes for a Spanish text.

    ``entry`` is the descriptor as MeSH confirms it (``None`` if NCBI does not know the name
    DeCS gives, e.g. a descriptor added to DeCS before MeSH publishes it); ``exact`` tells
    whether the text is one of its Spanish names or synonyms, not just shares its words.
    """

    descriptor: DecsDescriptor
    entry: ThesaurusEntry | None
    exact: bool


async def suggest_mesh(text: str, decs: DecsSearch, mesh: Thesaurus) -> tuple[DecsSuggestion, ...]:
    """List the MeSH descriptors DeCS finds for ``text``, exact matches first."""
    descriptors = await decs.search_words(text)
    if not descriptors:
        return ()
    entries = await mesh.lookup(list(dict.fromkeys(d.english for d in descriptors)))
    suggestions = [
        DecsSuggestion(
            descriptor=d,
            entry=_translated(entries.get(d.english)),
            exact=d.names(text),
        )
        for d in descriptors
    ]
    return tuple(sorted(suggestions, key=lambda s: not s.exact))


def _translated(entry: ThesaurusEntry | None) -> ThesaurusEntry | None:
    return replace(entry, translated_from=SOURCE) if entry is not None else None


class DecsMeshThesaurus:
    """MeSH that also understands Spanish: what MeSH does not know is looked up in DeCS.

    Only an exact Spanish name or synonym counts, as MeSH's own index does with entry terms:
    a text that merely shares words with a descriptor is not renamed behind the user's back.
    """

    name = Vocabulary.MESH.value

    def __init__(self, mesh: Thesaurus, decs: DecsSearch) -> None:
        if mesh.name != self.name:
            raise ValueError(f"DeCS translates into MeSH, not into {mesh.name}")
        self._mesh = mesh
        self._decs = decs

    async def lookup(self, texts: Sequence[str]) -> dict[str, ThesaurusEntry | None]:
        found = dict(await self._mesh.lookup(texts))
        for text in [t for t in dict.fromkeys(texts) if found.get(t) is None]:
            exact = [s for s in await suggest_mesh(text, self._decs, self._mesh) if s.exact]
            found[text] = next((s.entry for s in exact if s.entry is not None), None)
        return found
