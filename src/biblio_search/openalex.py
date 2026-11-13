"""Async client for the OpenAlex works API.

OpenAlex is free and needs no key: without one, every request draws from a small daily
budget shared by the whole IP address; a free key (``OPENALEX_API_KEY``) gives its own,
ten times larger. Searches with more than five boolean operators, which a strategy
almost always has, are limited to 1 request per second without a key and 5 with one.
https://help.openalex.org/api/authentication/
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx

from biblio_search.models import ArticleSummary, DatabaseError, SearchResult
from biblio_search.ratelimit import RateLimiter

DEFAULT_BASE_URL = "https://api.openalex.org/"
WORK_URL_PREFIX = "https://openalex.org/"
DOI_URL_PREFIX = "https://doi.org/"
SEARCH_PARAM = "search.title_and_abstract.exact"
PAGE_SIZE = 100
# OpenAlex no pagina más allá de page * per_page = 10 000 sin cursor (y el cursor pide clave).
MAX_RESULTS = 10_000
SUMMARY_FIELDS = "id,ids,doi,display_name,publication_year,authorships,primary_location,type"
LINK_FIELDS = "id,doi,ids,referenced_works"
# Los filtros admiten hasta 100 valores unidos con «|»; la lista de citas, hasta 200 por página.
FILTER_BATCH = 100
CITING_PAGE_SIZE = 200
_WORK_ID = re.compile(r"^W\d+$")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Un Retry-After largo es el presupuesto diario agotado: esperar no sirve, se avisa ya.
MAX_RETRY_AFTER_SECONDS = 60.0


class OpenAlexError(DatabaseError):
    """Raised when OpenAlex cannot be reached or rejects a request."""


@dataclass(frozen=True, slots=True)
class OpenAlexConfig:
    """Connection settings; ``api_key`` is sent only if set."""

    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        key = self.api_key
        if key is not None and (not key or any(c.isspace() for c in key)):
            raise ValueError("api_key must not be empty or contain spaces")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.timeout_seconds <= 0 or self.backoff_seconds < 0:
            raise ValueError("timeout must be positive and backoff non-negative")

    @property
    def requests_per_second(self) -> int:
        return 5 if self.api_key else 1

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OpenAlexConfig:
        """Read ``OPENALEX_API_KEY``; an empty value counts as unset."""
        source = os.environ if env is None else env
        return cls(api_key=source.get("OPENALEX_API_KEY") or None)


def _as_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OpenAlexError(f"unexpected OpenAlex response: {where} is not an object")
    return value


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _short_id(url: str) -> str:
    return url.removeprefix(WORK_URL_PREFIX)


def parse_search(payload: Mapping[str, object]) -> SearchResult:
    """Turn a ``/works`` search page into a :class:`SearchResult`.

    OpenAlex says how it read the query in ``meta.x_query.oql``, which plays the role of
    PubMed's query translation.
    """
    meta = _as_mapping(payload.get("meta"), "meta")
    count = meta.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        raise OpenAlexError("unexpected OpenAlex response: count is not a number")
    x_query = meta.get("x_query")
    translation = _as_str(x_query.get("oql")) if isinstance(x_query, Mapping) else ""
    ids = tuple(
        _short_id(work_id)
        for item in _as_list(payload.get("results"))
        if isinstance(item, Mapping) and (work_id := _as_str(item.get("id")))
    )
    return SearchResult(count=count, ids=ids, query_translation=translation)


def _authors(work: Mapping[str, object]) -> tuple[str, ...]:
    names: list[str] = []
    for authorship in _as_list(work.get("authorships")):
        if not isinstance(authorship, Mapping):
            continue
        author = authorship.get("author")
        name = _as_str(author.get("display_name")) if isinstance(author, Mapping) else ""
        if name:
            names.append(name)
    return tuple(names)


def _source(work: Mapping[str, object]) -> str:
    location = work.get("primary_location")
    if not isinstance(location, Mapping):
        return ""
    source = location.get("source")
    return _as_str(source.get("display_name")) if isinstance(source, Mapping) else ""


def _pmid(work: Mapping[str, object]) -> str | None:
    ids = work.get("ids")
    if not isinstance(ids, Mapping):
        return None
    # OpenAlex da el PMID como URL de PubMed (https://pubmed.ncbi.nlm.nih.gov/20501606).
    pmid = _as_str(ids.get("pmid")).rstrip("/").rsplit("/", 1)[-1]
    return pmid if pmid.isdigit() else None


def parse_work(work: Mapping[str, object]) -> ArticleSummary:
    """Turn one OpenAlex work into an :class:`ArticleSummary`."""
    url = _as_str(work.get("id"))
    if not url:
        raise OpenAlexError("unexpected OpenAlex response: a work has no id")
    year = work.get("publication_year")
    work_type = _as_str(work.get("type"))
    return ArticleSummary(
        record_id=_short_id(url),
        title=_as_str(work.get("display_name")).strip(),
        authors=_authors(work),
        journal=_source(work),
        year=year if isinstance(year, int) and not isinstance(year, bool) else None,
        doi=_as_str(work.get("doi")).removeprefix(DOI_URL_PREFIX) or None,
        publication_types=(work_type,) if work_type else (),
        url=url,
        pmid=_pmid(work),
    )


@dataclass(frozen=True, slots=True)
class WorkLinks:
    """A work's identifiers and the works it cites, to snowball from it."""

    work_id: str
    doi: str | None
    pmid: str | None
    referenced: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CitingWorks:
    """The works that cite one work: how many there are and the first ones fetched."""

    total: int
    records: tuple[ArticleSummary, ...]


def parse_links(work: Mapping[str, object]) -> WorkLinks:
    """Read the identifiers and ``referenced_works`` of one work."""
    url = _as_str(work.get("id"))
    if not url:
        raise OpenAlexError("unexpected OpenAlex response: a work has no id")
    referenced = tuple(
        _short_id(ref) for ref in _as_list(work.get("referenced_works")) if isinstance(ref, str)
    )
    return WorkLinks(
        work_id=_short_id(url),
        doi=_as_str(work.get("doi")).removeprefix(DOI_URL_PREFIX) or None,
        pmid=_pmid(work),
        referenced=referenced,
    )


def _batches(values: Sequence[str]) -> list[Sequence[str]]:
    return [values[start : start + FILTER_BATCH] for start in range(0, len(values), FILTER_BATCH)]


class OpenAlexClient:
    """OpenAlex client. Use as ``async with OpenAlexClient(config) as client: ...``."""

    def __init__(
        self,
        config: OpenAlexConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._limiter = limiter or RateLimiter(config.requests_per_second)
        self._sleep = sleep
        headers = {"User-Agent": "biblio-search (+https://github.com/juanmmm21/biblio-search)"}
        if config.api_key:
            # La clave va en la cabecera y no en la URL, para que no acabe en ningún registro.
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            headers=headers,
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

    async def search(self, term: str, *, retmax: int = 20, retstart: int = 0) -> SearchResult:
        """Search ``term`` in title and abstract; return the count and ``retmax`` work ids.

        Every page of up to 100 ids is one paid search call against the daily budget.
        """
        if not term.strip():
            raise ValueError("the search term cannot be empty")
        if retmax < 0 or retstart < 0:
            raise ValueError("retmax and retstart cannot be negative")
        if retstart + retmax > MAX_RESULTS:
            raise ValueError(f"OpenAlex lists at most the first {MAX_RESULTS} results")
        first_page = retstart // PAGE_SIZE + 1
        last_page = max(first_page, (retstart + retmax - 1) // PAGE_SIZE + 1)
        result: SearchResult | None = None
        ids: list[str] = []
        for page in range(first_page, last_page + 1):
            # Con retmax=0 basta el recuento: se pide una página mínima y se descarta.
            per_page = PAGE_SIZE if retmax else 1
            payload = await self.get_json(
                "works",
                {SEARCH_PARAM: term, "select": "id", "per_page": str(per_page), "page": str(page)},
            )
            result = parse_search(payload)
            ids.extend(result.ids)
            if len(result.ids) < per_page:
                break
        if result is None:
            raise OpenAlexError("the search finished without a result")
        offset = retstart - (first_page - 1) * PAGE_SIZE
        selected = tuple(ids[offset : offset + retmax])
        return SearchResult(
            count=result.count, ids=selected, query_translation=result.query_translation
        )

    async def summaries(
        self, ids: Sequence[str], *, missing_ok: bool = False
    ) -> tuple[ArticleSummary, ...]:
        """Fetch the works ``ids`` (``W…``) in the given order, 100 per request.

        A work OpenAlex no longer has is an error, or is left out with ``missing_ok``: the
        references of a work can point to records OpenAlex has merged or withdrawn since.
        """
        invalid = [work_id for work_id in ids if not _WORK_ID.match(work_id)]
        if invalid:
            raise ValueError(f"invalid OpenAlex work ids: {', '.join(invalid)}")
        found: dict[str, ArticleSummary] = {}
        for batch in _batches(ids):
            payload = await self._filter("ids.openalex", batch, SUMMARY_FIELDS)
            for item in _as_list(payload.get("results")):
                record = parse_work(_as_mapping(item, "work"))
                found[record.record_id] = record
        # El filtro no respeta el orden de relevancia de la búsqueda: se recompone aquí.
        missing = [work_id for work_id in ids if work_id not in found]
        if missing and not missing_ok:
            raise OpenAlexError(f"OpenAlex has no record for {', '.join(missing)}")
        return tuple(found[work_id] for work_id in ids if work_id in found)

    async def links(
        self,
        *,
        work_ids: Sequence[str] = (),
        dois: Sequence[str] = (),
        pmids: Sequence[str] = (),
    ) -> tuple[WorkLinks, ...]:
        """Find works by OpenAlex id, DOI or PMID, with the works each one cites.

        Identifiers OpenAlex does not know are simply not in the answer; the caller matches
        the works it gets back by their identifiers.
        """
        invalid = [work_id for work_id in work_ids if not _WORK_ID.match(work_id)]
        invalid += [pmid for pmid in pmids if not pmid.isdigit()]
        # «|» y «,» separan valores y filtros: un DOI que los lleve no cabe en un filtro.
        invalid += [doi for doi in dois if not doi or "|" in doi or "," in doi]
        if invalid:
            raise ValueError(f"invalid identifiers: {', '.join(invalid)}")
        found: dict[str, WorkLinks] = {}
        for field, values in (("ids.openalex", work_ids), ("doi", dois), ("pmid", pmids)):
            for batch in _batches(values):
                payload = await self._filter(field, batch, LINK_FIELDS)
                for item in _as_list(payload.get("results")):
                    work = parse_links(_as_mapping(item, "work"))
                    found[work.work_id] = work
        return tuple(found.values())

    async def citing(self, work_id: str, *, limit: int) -> CitingWorks:
        """The works that cite ``work_id``, most cited first, up to ``limit`` of them."""
        if not _WORK_ID.match(work_id):
            raise ValueError(f"invalid OpenAlex work id: {work_id}")
        if not 0 <= limit <= MAX_RESULTS:
            raise ValueError(f"limit must be between 0 and {MAX_RESULTS}")
        per_page = min(CITING_PAGE_SIZE, limit) or 1
        records: list[ArticleSummary] = []
        total = 0
        page = 1
        while True:
            payload = await self.get_json(
                "works",
                {
                    "filter": f"cites:{work_id}",
                    "select": SUMMARY_FIELDS,
                    "sort": "cited_by_count:desc",
                    "per_page": str(per_page),
                    "page": str(page),
                },
            )
            total = parse_search(payload).count
            results = _as_list(payload.get("results"))
            records.extend(parse_work(_as_mapping(item, "work")) for item in results)
            if len(records) >= limit or len(results) < per_page:
                break
            page += 1
        return CitingWorks(total=total, records=tuple(records[:limit]))

    async def _filter(self, field: str, values: Sequence[str], fields: str) -> Mapping[str, object]:
        return await self.get_json(
            "works",
            {
                "filter": f"{field}:" + "|".join(values),
                "select": fields,
                "per_page": str(FILTER_BATCH),
            },
        )

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                return float(retry_after)
        return self._config.backoff_seconds * 2.0**attempt

    async def get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
        """Call ``endpoint`` with retries on rate limits, server errors and network errors."""
        last_problem = ""
        for attempt in range(self._config.max_retries + 1):
            await self._limiter.acquire()
            response: httpx.Response | None = None
            try:
                response = await self._http.get(endpoint, params=params)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code in _RETRY_STATUSES:
                    last_problem = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    raise OpenAlexError(self._rejection(response))
                else:
                    return self._decode(response)
            if attempt == self._config.max_retries:
                break
            delay = self._retry_delay(attempt, response)
            if delay > MAX_RETRY_AFTER_SECONDS:
                raise OpenAlexError(
                    "OpenAlex asks to wait "
                    f"{delay:.0f} s: the daily budget is probably spent; a free key in "
                    "OPENALEX_API_KEY gives a larger one"
                )
            await self._sleep(delay)
        raise OpenAlexError(
            f"OpenAlex {endpoint} failed after {self._config.max_retries + 1} attempts: "
            f"{last_problem}"
        )

    @staticmethod
    def _rejection(response: httpx.Response) -> str:
        # OpenAlex explica en «message» qué no le gusta de la consulta (p. ej. un comodín).
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        message = _as_str(payload.get("message")) if isinstance(payload, Mapping) else ""
        detail = f": {message}" if message else ""
        return f"OpenAlex rejected the request (HTTP {response.status_code}){detail}"

    @staticmethod
    def _decode(response: httpx.Response) -> Mapping[str, object]:
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise OpenAlexError("OpenAlex did not return JSON") from exc
        return _as_mapping(payload, "response")
