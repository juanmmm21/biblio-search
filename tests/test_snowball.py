from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from biblio_search.merge import RecordSource
from biblio_search.models import ArticleSummary, Database
from biblio_search.openalex import CitingWorks, WorkLinks
from biblio_search.screening import Decision, ScreeningEntry
from biblio_search.snowball import (
    SnowballDirection,
    SnowballRun,
    render_snowball,
    snowball,
)

WHEN = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)


def _work(work_id: str, title: str = "") -> ArticleSummary:
    return ArticleSummary(
        record_id=work_id,
        title=title or f"Work {work_id}",
        authors=(),
        journal="",
        year=2020,
        doi=None,
        publication_types=("article",),
        url=f"https://openalex.org/{work_id}",
    )


def _entry(
    entry_id: int,
    *,
    openalex: str | None = None,
    doi: str | None = None,
    pmid: str | None = None,
    database: Database = Database.PUBMED,
) -> ScreeningEntry:
    sources = [RecordSource(database, f"r{entry_id}", "https://example.org")]
    if openalex is not None:
        sources.append(
            RecordSource(Database.OPENALEX, openalex, f"https://openalex.org/{openalex}")
        )
    record = ArticleSummary(
        record_id=f"r{entry_id}",
        title=f"Included {entry_id}",
        authors=(),
        journal="",
        year=2021,
        doi=doi,
        publication_types=(),
        url="https://example.org",
        pmid=pmid,
    )
    return ScreeningEntry(entry_id, record, tuple(sources), Decision.INCLUDE)


class FakeOpenAlex:
    def __init__(
        self,
        works: Sequence[WorkLinks],
        citing: dict[str, CitingWorks],
        known: Sequence[str],
    ) -> None:
        self.works = works
        self.citing_works = citing
        self.known = set(known)
        self.asked: dict[str, list[str]] = {}
        self.citing_calls: list[tuple[str, int]] = []
        self.fetched: list[str] = []

    async def links(
        self,
        *,
        work_ids: Sequence[str] = (),
        dois: Sequence[str] = (),
        pmids: Sequence[str] = (),
    ) -> tuple[WorkLinks, ...]:
        self.asked = {"work_ids": list(work_ids), "dois": list(dois), "pmids": list(pmids)}
        return tuple(
            w
            for w in self.works
            if w.work_id in work_ids or (w.doi or "").lower() in dois or w.pmid in pmids
        )

    async def summaries(
        self, ids: Sequence[str], *, missing_ok: bool = False
    ) -> tuple[ArticleSummary, ...]:
        assert missing_ok
        self.fetched = list(ids)
        return tuple(_work(i) for i in ids if i in self.known)

    async def citing(self, work_id: str, *, limit: int) -> CitingWorks:
        self.citing_calls.append((work_id, limit))
        return self.citing_works.get(work_id, CitingWorks(0, ()))


def _source() -> FakeOpenAlex:
    return FakeOpenAlex(
        works=[
            WorkLinks("W10", None, None, ("W1", "W2", "W20")),
            WorkLinks("W20", "10.5/Seed", None, ("W2", "W3", "W404")),
            WorkLinks("W30", None, "777", ()),
        ],
        citing={
            "W10": CitingWorks(500, (_work("W2"), _work("W7"))),
            "W20": CitingWorks(1, (_work("W10"),)),
        },
        known=["W1", "W2", "W3"],
    )


def test_snowball_follows_references_and_citations() -> None:
    source = _source()
    seeds = [
        _entry(1, openalex="W10", doi="10.9/ignored"),
        _entry(2, doi="https://doi.org/10.5/SEED"),
        _entry(3, pmid="777"),
        _entry(4, database=Database.ERIC),
        _entry(5, doi="10.6/unknown"),
    ]
    run = asyncio.run(snowball(source, seeds, max_citing=50, run_at=WHEN))
    # Una sola consulta por tipo de identificador y el más fiable de cada registro.
    assert source.asked == {
        "work_ids": ["W10"],
        "dois": ["10.5/seed", "10.6/unknown"],
        "pmids": ["777"],
    }
    assert [s.work_id for s in run.seeds] == ["W10", "W20", "W30"]
    first = run.seeds[0]
    assert (first.references, first.citing_total, first.citing_kept) == (3, 500, 2)
    assert source.citing_calls == [("W10", 50), ("W20", 50), ("W30", 50)]
    # Los registros de partida nunca son candidatos (W20 está en las referencias de W10).
    by_id = {c.record.record_id: c for c in run.candidates}
    assert list(by_id) == ["W1", "W2", "W7", "W3"]
    assert (by_id["W2"].cited_by, by_id["W2"].cites) == ((1, 2), (1,))
    assert by_id["W7"].cites == (1,) and by_id["W7"].cited_by == ()
    assert source.fetched == ["W1", "W3", "W404"], "citing works come with their record"
    assert run.missing_references == 1
    assert [(u.entry_id, u.reason) for u in run.unmatched] == [
        (4, "it has no DOI, PMID or OpenAlex id to find it by"),
        (5, "OpenAlex does not know its identifiers"),
    ]
    merged = run.merged
    assert merged.identified == 4 and merged.duplicates == 0
    assert merged.records[0].databases == (Database.OPENALEX,)


def test_snowball_in_one_direction_only() -> None:
    source = _source()
    run = asyncio.run(
        snowball(source, [_entry(1, openalex="W10")], directions=[SnowballDirection.CITATIONS])
    )
    assert [c.record.record_id for c in run.candidates] == ["W2", "W7"]
    assert run.seeds[0].references == 0
    assert source.fetched == []
    source = _source()
    run = asyncio.run(
        snowball(source, [_entry(1, openalex="W10")], directions=[SnowballDirection.REFERENCES])
    )
    assert source.citing_calls == []
    assert run.seeds[0].citing_total == 0
    # W20 no es registro de partida aquí, pero OpenAlex ya no lo tiene.
    assert [c.record.record_id for c in run.candidates] == ["W1", "W2"]
    assert run.missing_references == 1


@pytest.mark.parametrize("kwargs", [{"directions": []}, {"max_citing": 0}, {"max_citing": 10_001}])
def test_snowball_refuses_bad_settings(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        asyncio.run(snowball(_source(), [], **kwargs))  # type: ignore[arg-type]


def test_render_snowball_documents_the_round() -> None:
    source = _source()
    seeds = [_entry(1, openalex="W10"), _entry(4, database=Database.ERIC)]
    run = asyncio.run(snowball(source, seeds, max_citing=50, run_at=WHEN))
    text = render_snowball(run)
    assert text.splitlines()[:6] == [
        "# Snowballing log",
        "",
        "- Date: 2026-11-13 09:30 UTC",
        f"- Tool: biblio-search {run.version}",
        "- Database: OpenAlex",
        "- Followed: references and citations",
    ]
    assert "- Citing works kept per record: 50 (most cited first)" in text
    assert "- #1 Included 1 (W10): 3 references, 2 of 500 citing works" in text
    assert "## Included records left out" in text
    assert "- Candidates reached: 3" in text
    assert "- References OpenAlex no longer has (merged or withdrawn): 1" in text
    empty = SnowballRun(WHEN, (SnowballDirection.REFERENCES,), 100, (), (), ())
    assert "None could be found in OpenAlex." in render_snowball(empty)
    assert "Citing works kept" not in render_snowball(empty)
    with pytest.raises(ValueError, match="time zone"):
        SnowballRun(datetime(2026, 1, 1), (SnowballDirection.REFERENCES,), 100, (), (), ())
