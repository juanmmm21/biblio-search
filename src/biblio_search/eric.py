"""Async client for the ERIC API (``api.ies.ed.gov/eric/``).

ERIC is free, needs no key and publishes no rate limit; biblio-search keeps to two requests
per second over one connection anyway. The API is a Solr search: a query it cannot parse
comes back as HTTP 200 with an ``error`` object instead of results, and the gateway in front
of it sometimes answers ``Network error communicating with endpoint``, which is retried.
ERIC records carry no DOI, so they only merge with other databases' records by DOI or PMID
when a later source adds one. https://eric.ed.gov/?api
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx

from biblio_search.models import ArticleSummary, DatabaseError, SearchResult
from biblio_search.ratelimit import RateLimiter

DEFAULT_BASE_URL = "https://api.ies.ed.gov/eric/"
RECORD_URL_PREFIX = "https://eric.ed.gov/?id="
# La API admite hasta 2000 filas; sin resúmenes, 500 por página es un tamaño cómodo.
PAGE_SIZE = 500
ID_BATCH = 100
REQUESTS_PER_SECOND = 2.0
CACHE_SIZE = 5_000
RECORD_FIELDS = "id,title,author,source,publicationdateyear,publicationtype"
# ED = documentos que no son de revista, EJ = artículos de revista.
_ERIC_ID = re.compile(r"^E[DJ]\d+$")
_JAVA_CLASS = re.compile(r"^[\w.]+(?:Exception|Error): ")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class EricError(DatabaseError):
    """Raised when ERIC cannot be reached or rejects a request."""


class _GatewayError(EricError):
    """The gateway in front of ERIC answered for it: worth trying again."""


@dataclass(frozen=True, slots=True)
class EricConfig:
    """Connection settings. ERIC has no keys."""

    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 60.0
    max_retries: int = 3
    backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.timeout_seconds <= 0 or self.backoff_seconds < 0:
            raise ValueError("timeout must be positive and backoff non-negative")


@dataclass(frozen=True, slots=True)
class EricPage:
    """One page of a search: the total count and the documents with the fields asked for."""

    count: int
    docs: tuple[Mapping[str, object], ...]


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    # ERIC deja entidades HTML en algunos títulos («Children&apos;s»).
    return tuple(
        " ".join(html.unescape(item).split())
        for item in value
        if isinstance(item, str) and item.strip()
    )


def parse_page(content: bytes) -> EricPage:
    """Read a JSON answer; a Solr error raises :class:`EricError` with its message."""
    try:
        payload: object = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EricError("ERIC did not return valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise EricError("unexpected ERIC response: it is not an object")
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = error.get("msg")
        # Solr explica los errores de sintaxis en varias líneas: basta la primera.
        text = message.strip().splitlines()[0] if isinstance(message, str) and message else ""
        text = _JAVA_CLASS.sub("", text)
        raise EricError(f"ERIC rejected the query: {text or 'unknown error'}")
    response = payload.get("response")
    if not isinstance(response, Mapping):
        message = payload.get("message")
        if isinstance(message, str) and message:
            raise _GatewayError(message)
        raise EricError("unexpected ERIC response: it has no results")
    count = response.get("numFound")
    docs = response.get("docs")
    if not isinstance(count, int) or not isinstance(docs, list):
        raise EricError("unexpected ERIC response: no count or documents")
    return EricPage(count=count, docs=tuple(d for d in docs if isinstance(d, Mapping)))


def parse_record(doc: Mapping[str, object]) -> ArticleSummary:
    """Turn one ERIC document into an :class:`ArticleSummary`."""
    record_id = doc.get("id")
    if not isinstance(record_id, str) or not _ERIC_ID.match(record_id):
        raise EricError("unexpected ERIC response: a document has no ERIC number")
    year = doc.get("publicationdateyear")
    title = _strings(doc.get("title"))
    source = _strings(doc.get("source"))
    return ArticleSummary(
        record_id=record_id,
        title=title[0] if title else "",
        authors=_strings(doc.get("author")),
        journal=source[0] if source else "",
        year=year if isinstance(year, int) else None,
        doi=None,
        publication_types=_strings(doc.get("publicationtype")),
        url=RECORD_URL_PREFIX + record_id,
    )


def _gateway_message(response: httpx.Response) -> str:
    try:
        payload: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    message = payload.get("message") if isinstance(payload, Mapping) else None
    return message if isinstance(message, str) else ""


class EricClient:
    """ERIC client. Use as ``async with EricClient(config) as client: ...``."""

    def __init__(
        self,
        config: EricConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._limiter = limiter or RateLimiter(REQUESTS_PER_SECOND)
        self._sleep = sleep
        self._cache: OrderedDict[str, ArticleSummary] = OrderedDict()
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            headers={"User-Agent": "biblio-search (+https://github.com/juanmmm21/biblio-search)"},
            limits=httpx.Limits(max_connections=1),
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

    def _remember(self, records: Sequence[ArticleSummary]) -> None:
        for record in records:
            self._cache[record.record_id] = record
            self._cache.move_to_end(record.record_id)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    async def query(
        self, search: str, *, rows: int, start: int = 0, fields: str = RECORD_FIELDS
    ) -> EricPage:
        """One raw call to the API: ``search`` in ERIC syntax, ``rows`` documents from ``start``."""
        if not search.strip():
            raise ValueError("the search term cannot be empty")
        params = {
            "search": search,
            "rows": str(rows),
            "start": str(start),
            "fields": fields,
            "format": "json",
        }
        return await self._get(params)

    async def search(self, term: str, *, retmax: int = 20, retstart: int = 0) -> SearchResult:
        """Search ``term`` and return the count and ``retmax`` ERIC numbers.

        The records come with every page, so they are kept for :meth:`summaries`. ERIC does
        not say how it read the query, so the translation is left empty.
        """
        if retmax < 0 or retstart < 0:
            raise ValueError("retmax and retstart cannot be negative")
        ids: list[str] = []
        page: EricPage | None = None
        start = retstart
        while page is None or len(ids) < retmax:
            size = min(PAGE_SIZE, retmax - len(ids))
            page = await self.query(term, rows=size, start=start)
            records = tuple(parse_record(doc) for doc in page.docs)
            self._remember(records)
            ids.extend(record.record_id for record in records)
            if len(records) < size or not size:
                break
            start += size
        return SearchResult(count=page.count, ids=tuple(ids[:retmax]), query_translation="")

    async def summaries(self, ids: Sequence[str]) -> tuple[ArticleSummary, ...]:
        """The records ``ids`` in the given order; those not seen in a search are fetched."""
        invalid = [record_id for record_id in ids if not _ERIC_ID.match(record_id)]
        if invalid:
            raise ValueError(f"invalid ERIC numbers: {', '.join(invalid)}")
        missing = list(dict.fromkeys(i for i in ids if i not in self._cache))
        found = {record_id: self._cache[record_id] for record_id in ids if record_id in self._cache}
        for start in range(0, len(missing), ID_BATCH):
            batch = missing[start : start + ID_BATCH]
            page = await self.query(f"id:({' OR '.join(batch)})", rows=len(batch))
            records = tuple(parse_record(doc) for doc in page.docs)
            self._remember(records)
            found.update((record.record_id, record) for record in records)
        absent = [record_id for record_id in ids if record_id not in found]
        if absent:
            raise EricError(f"ERIC has no record for {', '.join(absent)}")
        return tuple(found[record_id] for record_id in ids)

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                return float(retry_after)
        return self._config.backoff_seconds * 2.0**attempt

    async def _get(self, params: Mapping[str, str]) -> EricPage:
        """Call the API with retries on rate limits, gateway errors and network errors."""
        last_problem = ""
        for attempt in range(self._config.max_retries + 1):
            await self._limiter.acquire()
            response: httpx.Response | None = None
            try:
                response = await self._http.get("", params=params)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code in _RETRY_STATUSES:
                    detail = _gateway_message(response)
                    last_problem = f"HTTP {response.status_code}" + (
                        f", {detail}" if detail else ""
                    )
                elif response.status_code >= 400:
                    detail = _gateway_message(response)
                    suffix = f": {detail}" if detail else ""
                    raise EricError(
                        f"ERIC rejected the request (HTTP {response.status_code}){suffix}"
                    )
                else:
                    try:
                        return parse_page(response.content)
                    except _GatewayError as exc:
                        last_problem = str(exc)
            if attempt == self._config.max_retries:
                break
            await self._sleep(self._retry_delay(attempt, response))
        raise EricError(
            f"ERIC failed after {self._config.max_retries + 1} attempts: {last_problem}"
        )
