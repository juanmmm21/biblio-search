"""Async client for NCBI E-utilities (PubMed esearch and esummary, raw calls to other dbs).

Usage rules from NCBI: identify the tool (and ideally an email), stay under 3 requests per
second without an API key or 10 with one, and back off when told to.
https://www.ncbi.nlm.nih.gov/books/NBK25497/
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

from biblio_search.models import ArticleSummary, SearchResult
from biblio_search.ratelimit import RateLimiter

DEFAULT_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
# esearch no devuelve más de 10 000 PMIDs por petición; más allá hay que paginar.
MAX_RETMAX = 10_000
# NCBI recomienda no pasar de ~200 UIDs por petición GET de esummary.
SUMMARY_BATCH_SIZE = 200
_PMID = re.compile(r"^\d+$")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class EutilsError(RuntimeError):
    """Raised when NCBI cannot be reached or answers with an error."""


@dataclass(frozen=True, slots=True)
class EutilsConfig:
    """Connection settings; ``email`` and ``api_key`` are sent to NCBI only if set."""

    tool: str = "biblio-search"
    email: str | None = None
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.tool or any(ch.isspace() for ch in self.tool):
            raise ValueError("tool must be a non-empty string without spaces")
        if self.email is not None and (not self.email or any(c.isspace() for c in self.email)):
            raise ValueError("email must not be empty or contain spaces")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.timeout_seconds <= 0 or self.backoff_seconds < 0:
            raise ValueError("timeout must be positive and backoff non-negative")

    @property
    def requests_per_second(self) -> int:
        return 10 if self.api_key else 3

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EutilsConfig:
        """Read ``NCBI_EMAIL`` and ``NCBI_API_KEY``; empty values count as unset."""
        source = os.environ if env is None else env
        return cls(
            email=source.get("NCBI_EMAIL") or None,
            api_key=source.get("NCBI_API_KEY") or None,
        )


def _as_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EutilsError(f"unexpected NCBI response: {where} is not an object")
    return value


def _as_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def parse_search(payload: Mapping[str, object]) -> SearchResult:
    """Turn an esearch JSON payload into a :class:`SearchResult`."""
    result = _as_mapping(payload.get("esearchresult"), "esearchresult")
    error = result.get("ERROR")
    if isinstance(error, str) and error:
        raise EutilsError(f"PubMed rejected the search: {error}")
    try:
        count = int(_as_str(result.get("count")))
    except ValueError as exc:
        raise EutilsError("unexpected NCBI response: count is not a number") from exc
    errors = result.get("errorlist")
    warnings = result.get("warninglist")
    not_found: tuple[str, ...] = ()
    if isinstance(errors, Mapping):
        not_found = _as_strings(errors.get("phrasesnotfound")) + tuple(
            f"[{field}]" for field in _as_strings(errors.get("fieldsnotfound"))
        )
    messages: list[str] = []
    if isinstance(warnings, Mapping):
        messages += [f"phrase ignored: {p}" for p in _as_strings(warnings.get("phrasesignored"))]
        messages += [
            f"quoted phrase not found: {p}"
            for p in _as_strings(warnings.get("quotedphrasesnotfound"))
        ]
        messages += list(_as_strings(warnings.get("outputmessages")))
    return SearchResult(
        count=count,
        ids=_as_strings(result.get("idlist")),
        query_translation=_as_str(result.get("querytranslation")),
        not_found=not_found,
        warnings=tuple(messages),
    )


def _year(record: Mapping[str, object]) -> int | None:
    # sortpubdate siempre empieza por AAAA; pubdate es texto libre («2026 Nov-Dec 01»).
    for key in ("sortpubdate", "pubdate"):
        text = _as_str(record.get(key))
        if len(text) >= 4 and text[:4].isdigit():
            return int(text[:4])
    return None


def _doi(record: Mapping[str, object]) -> str | None:
    ids = record.get("articleids")
    if not isinstance(ids, list):
        return None
    for item in ids:
        if isinstance(item, Mapping) and item.get("idtype") == "doi":
            value = _as_str(item.get("value"))
            if value:
                return value
    return None


def _authors(record: Mapping[str, object]) -> tuple[str, ...]:
    raw = record.get("authors")
    if not isinstance(raw, list):
        return ()
    return tuple(
        name for item in raw if isinstance(item, Mapping) and (name := _as_str(item.get("name")))
    )


def parse_summaries(payload: Mapping[str, object]) -> tuple[ArticleSummary, ...]:
    """Turn an esummary JSON payload into summaries, in the order NCBI lists them."""
    result = _as_mapping(payload.get("result"), "result")
    summaries: list[ArticleSummary] = []
    for uid in _as_strings(result.get("uids")):
        record = _as_mapping(result.get(uid), f"record {uid}")
        error = _as_str(record.get("error"))
        if error:
            raise EutilsError(f"PubMed has no summary for PMID {uid}: {error}")
        summaries.append(
            ArticleSummary(
                pmid=uid,
                title=_as_str(record.get("title")).strip(),
                authors=_authors(record),
                journal=_as_str(record.get("fulljournalname")) or _as_str(record.get("source")),
                year=_year(record),
                doi=_doi(record),
                publication_types=_as_strings(record.get("pubtype")),
            )
        )
    return tuple(summaries)


class EutilsClient:
    """PubMed client. Use as ``async with EutilsClient(config) as client: ...``."""

    def __init__(
        self,
        config: EutilsConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._limiter = limiter or RateLimiter(config.requests_per_second)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            headers={"User-Agent": f"{config.tool} (+https://github.com/juanmmm21/biblio-search)"},
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
        """Run ``term`` in PubMed and return the count and the first ``retmax`` PMIDs."""
        if not term.strip():
            raise ValueError("the search term cannot be empty")
        if not 0 <= retmax <= MAX_RETMAX:
            raise ValueError(f"retmax must be between 0 and {MAX_RETMAX}")
        if retstart < 0:
            raise ValueError("retstart cannot be negative")
        payload = await self.get_json(
            "esearch.fcgi",
            {"db": "pubmed", "term": term, "retmax": str(retmax), "retstart": str(retstart)},
        )
        return parse_search(payload)

    async def summaries(self, pmids: Sequence[str]) -> tuple[ArticleSummary, ...]:
        """Fetch the summaries of ``pmids``, batching requests as NCBI recommends."""
        invalid = [p for p in pmids if not _PMID.match(p)]
        if invalid:
            raise ValueError(f"invalid PMIDs: {', '.join(invalid)}")
        summaries: list[ArticleSummary] = []
        for start in range(0, len(pmids), SUMMARY_BATCH_SIZE):
            batch = pmids[start : start + SUMMARY_BATCH_SIZE]
            payload = await self.get_json("esummary.fcgi", {"db": "pubmed", "id": ",".join(batch)})
            summaries.extend(parse_summaries(payload))
        return tuple(summaries)

    def _identity(self) -> dict[str, str]:
        params = {"tool": self._config.tool, "retmode": "json"}
        if self._config.email:
            params["email"] = self._config.email
        if self._config.api_key:
            params["api_key"] = self._config.api_key
        return params

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                return float(retry_after)
        return self._config.backoff_seconds * 2.0**attempt

    async def get_json(self, endpoint: str, params: Mapping[str, str]) -> Mapping[str, object]:
        """Call any E-utility (e.g. ``esearch.fcgi`` on ``db=mesh``) with identity and retries."""
        query = {**params, **self._identity()}
        last_problem = ""
        for attempt in range(self._config.max_retries + 1):
            await self._limiter.acquire()
            response: httpx.Response | None = None
            try:
                response = await self._http.get(endpoint, params=query)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc.__class__.__name__}"
            else:
                if response.status_code in _RETRY_STATUSES:
                    last_problem = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    raise EutilsError(f"NCBI answered HTTP {response.status_code} on {endpoint}")
                else:
                    payload = self._decode(response, endpoint)
                    error = _as_str(payload.get("error"))
                    if not error:
                        return payload
                    # NCBI a veces avisa del límite de tasa con un 200 y un JSON de error.
                    if "rate limit" not in error.lower():
                        raise EutilsError(f"NCBI error on {endpoint}: {error}")
                    last_problem = error
            if attempt < self._config.max_retries:
                await self._sleep(self._retry_delay(attempt, response))
        raise EutilsError(
            f"NCBI {endpoint} failed after {self._config.max_retries + 1} attempts: {last_problem}"
        )

    @staticmethod
    def _decode(response: httpx.Response, endpoint: str) -> Mapping[str, object]:
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise EutilsError(f"NCBI {endpoint} did not return JSON") from exc
        return _as_mapping(payload, endpoint)
