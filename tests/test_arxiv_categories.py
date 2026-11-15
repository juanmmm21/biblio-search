from __future__ import annotations

import pytest

from biblio_search.arxiv_categories import ARXIV_ARCHIVES, ARXIV_CATEGORIES, is_arxiv_category


def test_the_taxonomy_is_complete_and_well_formed() -> None:
    assert len(ARXIV_CATEGORIES) == 155
    assert ARXIV_CATEGORIES["quant-ph"] == "Quantum Physics"
    assert ARXIV_CATEGORIES["cs.LG"] == "Machine Learning"
    assert "cond-mat" in ARXIV_ARCHIVES and "quant-ph" not in ARXIV_ARCHIVES
    for code in ARXIV_CATEGORIES:
        assert is_arxiv_category(code)


@pytest.mark.parametrize("code", ["cs.*", "math.*", "cond-mat.*", "quant-ph", "physics.optics"])
def test_categories_and_whole_archives_are_accepted(code: str) -> None:
    assert is_arxiv_category(code)


@pytest.mark.parametrize("code", ["cs.XX", "quant-ph.*", "physics", "biology.*", "cs.lg", ""])
def test_invented_categories_are_rejected(code: str) -> None:
    assert not is_arxiv_category(code)
