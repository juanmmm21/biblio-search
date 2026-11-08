from __future__ import annotations

from conftest import load_fixture

from biblio_search.eutils import parse_summaries
from biblio_search.merge import merge_records, normalize_doi
from biblio_search.models import ArticleSummary, Database
from biblio_search.openalex import parse_work


def _record(
    record_id: str, *, doi: str | None = None, pmid: str | None = None, title: str = "A study"
) -> ArticleSummary:
    return ArticleSummary(
        record_id=record_id,
        title=title,
        authors=(),
        journal="",
        year=None,
        doi=doi,
        publication_types=(),
        url=f"https://example.org/{record_id}",
        pmid=pmid,
    )


def test_normalize_doi_drops_prefixes_and_case() -> None:
    assert normalize_doi("https://doi.org/10.1111/JNU.12957") == "10.1111/jnu.12957"
    assert normalize_doi("doi:10.1/X ") == "10.1/x"
    assert normalize_doi("HTTPS://DX.DOI.ORG/10.1/x") == "10.1/x"


def test_same_doi_in_two_databases_is_one_record() -> None:
    pubmed = _record("1", doi="10.1/ABC", pmid="1")
    openalex = _record("W1", doi="10.1/abc")
    merged = merge_records([(Database.PUBMED, [pubmed]), (Database.OPENALEX, [openalex])])
    assert merged.identified == 2 and merged.duplicates == 1
    (only,) = merged.records
    assert only.record.record_id == "1"
    assert only.databases == (Database.PUBMED, Database.OPENALEX)
    assert [s.record_id for s in only.sources] == ["1", "W1"]


def test_pmid_matches_and_fills_the_missing_doi() -> None:
    pubmed = _record("7", pmid="7")
    openalex = _record("W7", doi="10.2/z", pmid="7")
    (only,) = merge_records([(Database.PUBMED, [pubmed]), (Database.OPENALEX, [openalex])]).records
    assert (only.record.doi, only.record.pmid) == ("10.2/z", "7")


def test_a_record_with_both_keys_joins_two_groups() -> None:
    by_pmid = _record("5", pmid="5")
    by_doi = _record("W9", doi="10.3/q")
    bridge = _record("W5", doi="10.3/q", pmid="5")
    merged = merge_records([(Database.PUBMED, [by_pmid]), (Database.OPENALEX, [by_doi, bridge])])
    assert merged.identified == 3 and merged.duplicates == 2
    (only,) = merged.records
    assert only.record.record_id == "5"
    assert {s.record_id for s in only.sources} == {"5", "W9", "W5"}


def test_records_without_ids_or_with_the_same_title_stay_apart() -> None:
    merged = merge_records(
        [(Database.PUBMED, [_record("1"), _record("2")]), (Database.OPENALEX, [_record("W1")])]
    )
    assert len(merged.records) == 3 and merged.duplicates == 0


def test_order_follows_databases_then_records() -> None:
    merged = merge_records(
        [
            (Database.PUBMED, [_record("1", pmid="1"), _record("2", pmid="2")]),
            (Database.OPENALEX, [_record("W3", pmid="3"), _record("W2", pmid="2")]),
        ]
    )
    assert [m.record.record_id for m in merged.records] == ["1", "2", "W3"]


def test_empty_batches() -> None:
    merged = merge_records([(Database.PUBMED, [])])
    assert merged.records == () and merged.identified == 0 and merged.duplicates == 0


def test_real_records_from_both_fixtures() -> None:
    pubmed = parse_summaries(load_fixture("esummary_caregiver.json"))
    works = load_fixture("openalex_works_caregiver.json")["results"]
    assert isinstance(works, list)
    openalex = tuple(map(parse_work, works))
    # Mismo trabajo que el primero de OpenAlex, tal como lo daría PubMed.
    twin = _record(openalex[0].pmid or "", doi=openalex[0].doi, pmid=openalex[0].pmid)
    merged = merge_records([(Database.PUBMED, [*pubmed, twin]), (Database.OPENALEX, openalex)])
    assert merged.identified == 7 and merged.duplicates == 1
