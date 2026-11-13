"""Async client for the arXiv API (``export.arxiv.org/api/query``).

arXiv is free and needs no key, but its terms of use allow one request every three seconds
over a single connection, counted for all of a user's machines together. Answers are Atom
feeds; a rejected query comes back as a feed whose only entry is titled «Error».
https://info.arxiv.org/help/api/user-manual.html · https://info.arxiv.org/help/api/tou.html
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx

from biblio_search.models import ArticleSummary, DatabaseError, SearchResult
from biblio_search.ratelimit import RateLimiter

DEFAULT_BASE_URL = "https://export.arxiv.org/api/"
ABS_URL_PREFIX = "https://arxiv.org/abs/"
# arXiv pide trozos de 2000 como mucho; con resúmenes completos, 500 por página ya pesa ~1 MB.
PAGE_SIZE = 500
ID_BATCH = 100
# arXiv no devuelve más allá de los 30 000 primeros resultados de una consulta.
MAX_RESULTS = 30_000
SECONDS_BETWEEN_REQUESTS = 3.0
# Resúmenes ya leídos en las búsquedas, para no volver a pedirlos con id_list.
CACHE_SIZE = 5_000
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
_QUERY_TITLE = re.compile(r"search_query=(.*?)&id_list=", re.DOTALL)
_VERSION = re.compile(r"v\d+$")
# Identificadores nuevos (2301.01234) y antiguos (hep-th/9711200, math.AG/0601001).
_ARXIV_ID = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z]+(?:-[a-z]+)*(?:\.[A-Z]{2})?/\d{7})$")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class ArxivError(DatabaseError):
    """Raised when arXiv cannot be reached or rejects a request."""


@dataclass(frozen=True, slots=True)
class ArxivConfig:
    """Connection settings. arXiv has no keys: the pace is the same for everyone."""

    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 60.0
    max_retries: int = 3
    backoff_seconds: float = 3.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.timeout_seconds <= 0 or self.backoff_seconds < 0:
            raise ValueError("timeout must be positive and backoff non-negative")


@dataclass(frozen=True, slots=True)
class ArxivPage:
    """One page of a search: the total count, its records and how arXiv read the query."""

    count: int
    records: tuple[ArticleSummary, ...]
    query: str


def _text(element: ET.Element, tag: str) -> str:
    found = element.find(tag)
    return " ".join((found.text or "").split()) if found is not None else ""


def _int(element: ET.Element, tag: str) -> int:
    value = _text(element, tag)
    if not value.isdigit():
        raise ArxivError(f"unexpected arXiv response: {tag.rsplit('}', 1)[-1]} is not a number")
    return int(value)


def _parse_feed(content: bytes) -> ET.Element:
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise ArxivError("arXiv did not return a valid Atom feed") from exc


def _error_message(feed: ET.Element) -> str | None:
    """The message of an error feed, or ``None`` when the feed is a normal answer."""
    for entry in feed.iter(f"{_ATOM}entry"):
        if _text(entry, f"{_ATOM}title") == "Error":
            return _text(entry, f"{_ATOM}summary") or "unknown error"
    return None


def short_id(url: str) -> str:
    """``http://arxiv.org/abs/2301.01234v2`` → ``2301.01234`` (the id of every version)."""
    return _VERSION.sub("", url.rsplit("/abs/", 1)[-1])


def parse_entry(entry: ET.Element) -> ArticleSummary:
    """Turn one Atom entry into an :class:`ArticleSummary`."""
    record_id = short_id(_text(entry, f"{_ATOM}id"))
    if not record_id:
        raise ArxivError("unexpected arXiv response: an entry has no id")
    published = _text(entry, f"{_ATOM}published")
    year = published[:4]
    journal_ref = _text(entry, f"{_ARXIV}journal_ref")
    authors = tuple(
        name
        for author in entry.findall(f"{_ATOM}author")
        if (name := _text(author, f"{_ATOM}name"))
    )
    return ArticleSummary(
        record_id=record_id,
        title=_text(entry, f"{_ATOM}title"),
        authors=authors,
        journal=journal_ref,
        year=int(year) if year.isdigit() else None,
        doi=_text(entry, f"{_ARXIV}doi") or None,
        # Con referencia de revista el preprint ya está publicado; si no, sigue siendo preprint.
        publication_types=("article",) if journal_ref else ("preprint",),
        url=ABS_URL_PREFIX + record_id,
    )


def parse_page(content: bytes) -> ArxivPage:
    """Read a search or ``id_list`` answer; an error feed raises :class:`ArxivError`."""
    feed = _parse_feed(content)
    message = _error_message(feed)
    if message is not None:
        raise ArxivError(f"arXiv rejected the query: {message}")
    # El título del feed repite la consulta tal como llegó: es la traducción de arXiv.
    title = _QUERY_TITLE.search(_text(feed, f"{_ATOM}title"))
    return ArxivPage(
        count=_int(feed, f"{_OPENSEARCH}totalResults"),
        records=tuple(parse_entry(entry) for entry in feed.findall(f"{_ATOM}entry")),
        query=title.group(1) if title else "",
    )


class ArxivClient:
    """arXiv client. Use as ``async with ArxivClient(config) as client: ...``."""

    def __init__(
        self,
        config: ArxivConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._limiter = limiter or RateLimiter(1.0 / SECONDS_BETWEEN_REQUESTS)
        self._sleep = sleep
        self._cache: OrderedDict[str, ArticleSummary] = OrderedDict()
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            headers={"User-Agent": "biblio-search (+https://github.com/juanmmm21/biblio-search)"},
            # Una sola conexión a la vez, como piden las condiciones de uso.
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

    async def search(self, term: str, *, retmax: int = 20, retstart: int = 0) -> SearchResult:
        """Search ``term`` (arXiv syntax) and return the count and ``retmax`` arXiv ids.

        The records come with every page, so they are kept for :meth:`summaries`.
        """
        if not term.strip():
            raise ValueError("the search term cannot be empty")
        if retmax < 0 or retstart < 0:
            raise ValueError("retmax and retstart cannot be negative")
        if retstart + retmax > MAX_RESULTS:
            raise ValueError(f"arXiv lists at most the first {MAX_RESULTS} results")
        ids: list[str] = []
        page: ArxivPage | None = None
        start = retstart
        while page is None or len(ids) < retmax:
            # max_results=0 da un error 500 en arXiv: para el recuento basta un registro.
            size = min(PAGE_SIZE, retmax - len(ids)) or 1
            page = await self._query(
                {"search_query": term, "start": str(start), "max_results": str(size)}
            )
            self._remember(page.records)
            if retmax:
                ids.extend(record.record_id for record in page.records)
            if len(page.records) < size:
                break
            start += size
        return SearchResult(count=page.count, ids=tuple(ids[:retmax]), query_translation=page.query)

    async def summaries(self, ids: Sequence[str]) -> tuple[ArticleSummary, ...]:
        """The records ``ids`` in the given order; those not seen in a search are fetched."""
        invalid = [record_id for record_id in ids if not _ARXIV_ID.match(record_id)]
        if invalid:
            raise ValueError(f"invalid arXiv ids: {', '.join(invalid)}")
        missing = list(dict.fromkeys(i for i in ids if i not in self._cache))
        found = {record_id: self._cache[record_id] for record_id in ids if record_id in self._cache}
        for start in range(0, len(missing), ID_BATCH):
            batch = missing[start : start + ID_BATCH]
            page = await self._query({"id_list": ",".join(batch), "max_results": str(len(batch))})
            self._remember(page.records)
            found.update((record.record_id, record) for record in page.records)
        # id_list no respeta el orden pedido: se recompone aquí.
        absent = [record_id for record_id in ids if record_id not in found]
        if absent:
            raise ArxivError(f"arXiv has no record for {', '.join(absent)}")
        return tuple(found[record_id] for record_id in ids)

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                return float(retry_after)
        return self._config.backoff_seconds * 2.0**attempt

    async def _query(self, params: Mapping[str, str]) -> ArxivPage:
        """Call ``query`` with retries on rate limits, server errors and network errors."""
        last_problem = ""
        for attempt in range(self._config.max_retries + 1):
            await self._limiter.acquire()
            response: httpx.Response | None = None
            try:
                response = await self._http.get("query", params=params)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code == 429:
                    # arXiv no manda Retry-After y el bloqueo dura minutos: se dice qué hacer.
                    last_problem = "HTTP 429, arXiv is limiting requests; wait a few minutes"
                elif response.status_code in _RETRY_STATUSES:
                    last_problem = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    raise ArxivError(self._rejection(response))
                else:
                    return parse_page(response.content)
            if attempt == self._config.max_retries:
                break
            await self._sleep(self._retry_delay(attempt, response))
        raise ArxivError(
            f"arXiv failed after {self._config.max_retries + 1} attempts: {last_problem}"
        )

    @staticmethod
    def _rejection(response: httpx.Response) -> str:
        # Una consulta mal formada llega como 400 con un feed de error que dice qué falla.
        try:
            message = _error_message(ET.fromstring(response.content))
        except ET.ParseError:
            message = None
        detail = f": {message}" if message else ""
        return f"arXiv rejected the query (HTTP {response.status_code}){detail}"
