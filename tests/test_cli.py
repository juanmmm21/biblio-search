from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from conftest import load_fixture, mesh_fixture_for
from test_tuning import CountingBackend

import biblio_search.__main__ as cli
from biblio_search.__main__ import format_check, format_record, format_suggestion, main
from biblio_search.check import QuestionCheck
from biblio_search.decs import DecsClient, DecsConfig, DecsDescriptor, DecsSuggestion
from biblio_search.eutils import EutilsClient, EutilsConfig
from biblio_search.models import ArticleSummary, Database, Term, Vocabulary
from biblio_search.openalex import OpenAlexClient, OpenAlexConfig
from biblio_search.pipeline import SearchBackend
from biblio_search.pubmed_query import PubMedDialect
from biblio_search.question_file import load_question
from biblio_search.ratelimit import RateLimiter
from biblio_search.screening import Decision, ScreeningStore

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE = EXAMPLES / "caregiver-burden.toml"
EDUCATION = EXAMPLES / "peer-tutoring-reading.toml"


def test_build_prints_equation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("(Anxiety[mj:noexp] OR")
    assert "NOT MEDLINE[sb]" not in out


def test_build_flags_enable_options(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(EXAMPLE), "--unindexed-only", "--repeat-controlled-as-free"]) == 0
    out = capsys.readouterr().out
    assert "NOT MEDLINE[sb]" in out
    assert "aged[tiab]" in out


def test_build_reports_errors_on_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(tmp_path / "missing.toml")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot read" in captured.err


def _patch_client(monkeypatch: pytest.MonkeyPatch, seen: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = (
            "esearch_caregiver.json" if "esearch" in request.url.path else "esummary_caregiver.json"
        )
        return httpx.Response(200, json=load_fixture(name))

    async def no_wait(_: float) -> None:
        return None

    def factory(config: EutilsConfig) -> EutilsClient:
        return EutilsClient(
            config,
            transport=httpx.MockTransport(handler),
            limiter=RateLimiter(1000, sleep=no_wait),
            sleep=no_wait,
        )

    monkeypatch.setattr(cli, "EutilsClient", factory)
    _patch_openalex(monkeypatch, seen)


def _patch_openalex(monkeypatch: pytest.MonkeyPatch, seen: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = (
            "openalex_works_caregiver.json"
            if "filter" in request.url.params
            else "openalex_search_caregiver.json"
        )
        return httpx.Response(200, json=load_fixture(name))

    async def no_wait(_: float) -> None:
        return None

    def factory(config: OpenAlexConfig) -> OpenAlexClient:
        return OpenAlexClient(
            config,
            transport=httpx.MockTransport(handler),
            limiter=RateLimiter(1000, sleep=no_wait),
            sleep=no_wait,
        )

    monkeypatch.setattr(cli, "OpenAlexClient", factory)


def test_search_prints_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    _patch_client(monkeypatch, seen)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.setenv("NCBI_EMAIL", "env@example.org")

    assert main(["search", str(EXAMPLE), "--max", "3", "--email", "flag@example.org"]) == 0

    out = capsys.readouterr().out
    pubmed, openalex = out.split("\n\nOpenAlex\n")
    assert pubmed.startswith("Searching in: PubMed, OpenAlex\n\nPubMed\nEquation: (")
    assert "Results: 2808" in pubmed
    assert "PubMed understood: (" in pubmed
    assert "  1. PMID 42970524 (2026) Music-Based Interventions" in pubmed
    assert "[Journal Article, Review]" in pubmed
    assert openalex.startswith("Equation: (anxiety OR depress*) AND")
    assert "OpenAlex understood: get works with" in openalex
    assert "Results: 2516" in openalex
    assert "  1. OpenAlex W2145256182 (2010) Associations between drug burden" in openalex
    assert "| Age and Ageing [article]" in openalex
    assert seen[0].url.params["email"] == "flag@example.org"
    assert seen[0].url.params["retmax"] == "3"
    openalex_calls = [r for r in seen if r.url.host == "api.openalex.org"]
    assert len(openalex_calls) == 2
    assert openalex_calls[1].url.params["filter"] == (
        "ids.openalex:W2145256182|W4391296183|W1923367678"
    )


def test_search_writes_log_ris_and_csv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _patch_client(monkeypatch, [])
    log, ris, csv = tmp_path / "log.md", tmp_path / "records.ris", tmp_path / "records.csv"
    args = ["search", str(EXAMPLE), "--max", "3", "--log", str(log), "--ris", str(ris)]
    assert main([*args, "--csv", str(csv)]) == 0

    out = capsys.readouterr().out
    assert "Merged: 6 records retrieved, 0 duplicates removed (same DOI or PMID), 6 unique" in out
    assert f"Log written to {log}" in out and f"CSV written to {csv}" in out
    text = log.read_text(encoding="utf-8")
    assert text.startswith("# Search log") and "--database pubmed --database openalex" in text
    assert ris.read_bytes().count(b"ER  - \r\n") == 6
    assert csv.read_bytes().startswith(b"\xef\xbb\xbftitle,authors")


def test_search_reports_files_it_cannot_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _patch_client(monkeypatch, [])
    target = tmp_path / "missing" / "log.md"
    assert main(["search", str(EXAMPLE), "--max", "3", "--log", str(target)]) == 1
    assert f"cannot write {target}" in capsys.readouterr().err


def test_search_adds_to_a_screening_and_screening_exports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _patch_client(monkeypatch, [])
    screening = tmp_path / "screening.sqlite"
    args = ["search", str(EXAMPLE), "--max", "3", "--screening", str(screening)]
    assert main(args) == 0
    assert (
        f"Screening: search 1 added to {screening}, 6 new records, 0 already there"
        in capsys.readouterr().out
    )
    assert main(args) == 0
    second_run = capsys.readouterr().out
    assert "search 2 added" in second_run and "0 new records, 6 already there" in second_run

    with ScreeningStore.open(screening) as store:
        first, second, *_ = store.entries()
        store.decide(first.entry_id, Decision.INCLUDE)
        store.decide(second.entry_id, Decision.EXCLUDE, "not caregivers")
    ris, logs = tmp_path / "included.ris", tmp_path / "logs.md"
    assert main(["screening", str(screening), "--ris", str(ris), "--log", str(logs)]) == 0
    out = capsys.readouterr().out
    assert "Records identified: 12\nDuplicates removed: 6\nRecords screened: 6" in out
    assert "    not caregivers: 1" in out and f"RIS (include) written to {ris}" in out
    assert ris.read_bytes().count(b"ER  - \r\n") == 1
    assert logs.read_text(encoding="utf-8").count("# Search log") == 2


def test_screening_needs_an_existing_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "nothing.sqlite"
    assert main(["screening", str(missing)]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists()


def test_search_reports_ncbi_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(config: EutilsConfig) -> EutilsClient:
        return EutilsClient(
            config, transport=httpx.MockTransport(lambda _: httpx.Response(400, text="bad"))
        )

    monkeypatch.setattr(cli, "EutilsClient", failing)
    assert main(["search", str(EXAMPLE), "--database", "pubmed"]) == 1
    assert "HTTP 400" in capsys.readouterr().err


def test_search_rejects_out_of_range_max(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["search", str(EXAMPLE), "--max", "20000"])
    assert "between 0 and 10000" in capsys.readouterr().err


def test_search_reports_openalex_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def rejecting(config: OpenAlexConfig) -> OpenAlexClient:
        answer = httpx.Response(400, json=load_fixture("openalex_error_wildcard.json"))
        return OpenAlexClient(config, transport=httpx.MockTransport(lambda _: answer))

    monkeypatch.setattr(cli, "OpenAlexClient", rejecting)
    assert main(["search", str(EXAMPLE), "--database", "openalex"]) == 1
    assert "OpenAlex rejected the request (HTTP 400): Wildcards" in capsys.readouterr().err


def test_format_record_without_year_journal_or_types() -> None:
    record = ArticleSummary("1", "Title.", (), "Journal", None, None, (), "https://x")
    assert format_record(12, record) == " 12. PMID 1 (n.d.) Title. | Journal"
    unpublished = ArticleSummary("W1", "Title.", (), "", 2020, None, (), "https://x")
    assert format_record(1, unpublished, "OpenAlex") == "  1. OpenAlex W1 (2020) Title."


def _patch_mesh_client(monkeypatch: pytest.MonkeyPatch, seen: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=mesh_fixture_for(endpoint, request.url.params))

    async def no_wait(_: float) -> None:
        return None

    def factory(config: EutilsConfig) -> EutilsClient:
        return EutilsClient(
            config,
            transport=httpx.MockTransport(handler),
            limiter=RateLimiter(1000, sleep=no_wait),
            sleep=no_wait,
        )

    monkeypatch.setattr(cli, "EutilsClient", factory)


def test_check_confirms_example_headings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    _patch_mesh_client(monkeypatch, seen)

    assert main(["check", str(EXAMPLE), "--email", "flag@example.org"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("MeSH check of 4 controlled terms")
    assert "  ok          Anxiety (D001007) [anxiety or depression]" in out
    assert "  ok          Aged (D000368) [older people]" in out
    assert "use a no_explode scope" in out
    assert "  caregivers: Care Givers, Family Caregivers" in out
    assert "Equation after the check: (Anxiety[mj:noexp] OR" in out
    assert all(r.url.params["email"] == "flag@example.org" for r in seen)
    assert len(seen) == 5


def test_check_demotes_unknown_and_renames_entry_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_mesh_client(monkeypatch, [])
    question = tmp_path / "q.toml"
    question.write_text(
        'question = "q"\nformat = "spc"\n'
        '[[components]]\nrole = "population"\ndescription = "carers"\n'
        '[[components.conditions]]\nlabel = "carers"\n'
        'controlled = ["Carers", "Qwzxvplorkt"]\nfree = ["carer*"]\n',
        encoding="utf-8",
    )

    assert main(["check", str(question)]) == 0

    out = capsys.readouterr().out
    assert "  entry term  Carers -> Caregivers (D017028) [carers]" in out
    assert "  not found   Qwzxvplorkt [carers]: searched as free text instead" in out
    assert "Equation after the check: (Caregivers[mh] OR Qwzxvplorkt[tiab] OR carer*[tiab])" in out


def test_check_reports_ncbi_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(config: EutilsConfig) -> EutilsClient:
        return EutilsClient(
            config, transport=httpx.MockTransport(lambda _: httpx.Response(404, text="no"))
        )

    monkeypatch.setattr(cli, "EutilsClient", failing)
    assert main(["check", str(EXAMPLE)]) == 1
    assert "HTTP 404" in capsys.readouterr().err


def test_format_check_without_controlled_terms() -> None:
    out = format_check(QuestionCheck("mesh", ()), "(x[tiab])")
    assert out == (
        "MeSH check of 0 controlled terms\n"
        "  (the question has no MeSH terms)\n"
        "\n"
        "Equation after the check: (x[tiab])"
    )


def test_build_warns_about_foreign_headings(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(EDUCATION)]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("(peer teaching[tiab] OR")
    assert captured.err == (
        "biblio-search: PubMed does not index these headings, so they are searched as free"
        " text: Peer Teaching (ERIC), Reading Comprehension (ERIC),"
        " Elementary School Students (ERIC)\n"
    )


def test_build_without_foreign_headings_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(EXAMPLE), "--dialect", "pubmed"]) == 0
    assert capsys.readouterr().err == ""


def test_databases_explains_the_automatic_choice(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["databases", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(
        "Databases (chosen automatically):\n"
        "  PubMed\n"
        "    - the question uses MeSH headings\n"
        "    - SPC is a health-science question format\n"
        "  OpenAlex\n"
    )
    assert "\nPubMed equation: (Anxiety[mj:noexp] OR" in out
    assert "\nOpenAlex equation: (anxiety OR depress*) AND" in out
    assert out.endswith(
        "\nNote: OpenAlex does not index these headings, so they are searched as free text: "
        "Anxiety (MeSH), Depression (MeSH), Caregivers (MeSH), Aged (MeSH)\n"
    )


def test_databases_for_an_education_question(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["databases", str(EDUCATION)]) == 0
    out = capsys.readouterr().out
    assert "  ERIC (biblio-search cannot search it yet)\n    - the discipline is education" in out
    assert '\nOpenAlex equation: ("peer teaching" OR "peer tutor*"~0 OR' in out
    assert "PubMed equation" not in out


def test_databases_none_searchable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["databases", str(EDUCATION), "--database", "eric"]) == 0
    out = capsys.readouterr().out
    assert out.endswith(
        "\nNone of them can be searched yet; add --database pubmed or --database openalex "
        "to search one that can be.\n"
    )
    assert "equation" not in out


def test_databases_by_hand_include_the_rule_reasons(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["databases", str(EDUCATION), "--database", "pubmed"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Databases (chosen by hand):\n  PubMed\n    - chosen by hand\n")
    assert "\nPubMed equation: (peer teaching[tiab] OR" in out
    assert "\nNote: PubMed does not index these headings" in out


def test_databases_rejects_unknown_names(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["databases", str(EXAMPLE), "--database", "scopus"])
    assert "invalid choice" in capsys.readouterr().err


def test_search_needs_a_searchable_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    _patch_client(monkeypatch, seen)
    assert main(["search", str(EDUCATION), "--database", "eric", "--database", "arxiv"]) == 2
    err = capsys.readouterr().err
    assert "none of the chosen databases can be searched yet (ERIC, arXiv)" in err
    assert "--database pubmed or --database openalex" in err
    assert seen == []


def test_search_in_pubmed_by_hand(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    _patch_client(monkeypatch, seen)
    assert main(["search", str(EDUCATION), "--database", "pubmed", "--max", "1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Searching in: PubMed\n\nPubMed\nEquation: (peer teaching[tiab] OR")
    assert seen[0].url.params["term"].startswith("(peer teaching[tiab]")


def test_format_check_lists_unchecked_headings() -> None:
    eric = Term.controlled("Peer Teaching", vocabulary=Vocabulary.ERIC)
    out = format_check(QuestionCheck("mesh", (), (eric,)), "(x[tiab])")
    assert "\nNot checked (biblio-search cannot check them against MeSH):\n" in out
    assert "  Peer Teaching (ERIC)\n" in out


def _patch_decs_client(monkeypatch: pytest.MonkeyPatch, fixture: str) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture(fixture))

    async def no_wait(_: float) -> None:
        return None

    def factory(config: DecsConfig) -> DecsClient:
        return DecsClient(
            config,
            transport=httpx.MockTransport(handler),
            limiter=RateLimiter(1000, sleep=no_wait),
            sleep=no_wait,
        )

    monkeypatch.setattr(cli, "DecsClient", factory)
    monkeypatch.setenv("DECS_API_KEY", "test-key")
    return seen


def _spanish_question(tmp_path: Path) -> Path:
    question = tmp_path / "q.toml"
    question.write_text(
        'question = "q"\nformat = "concepts"\n'
        '[[components]]\ndescription = "muscles"\n'
        '[[components.conditions]]\nlabel = "muscles"\n'
        'controlled = ["Músculos"]\nfree = ["muscle*"]\n',
        encoding="utf-8",
    )
    return question


def test_check_translates_spanish_headings_with_a_decs_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_mesh_client(monkeypatch, [])
    seen = _patch_decs_client(monkeypatch, "decs_words_musculos.json")

    assert main(["check", str(_spanish_question(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert "  in Spanish  Músculos -> Muscles (D009132) [muscles]: found through DeCS" in out
    assert "Equation after the check: (Muscles[mh] OR muscle*[tiab])" in out
    assert "DECS_API_KEY" not in out
    assert [r.url.params["words"] for r in seen] == ["Músculos"]


def test_check_without_a_decs_key_says_how_to_translate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_mesh_client(monkeypatch, [])

    assert main(["check", str(_spanish_question(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert "  not found   Músculos [muscles]" in out
    assert out.rstrip().endswith("(free licence: https://decs.bvsalud.org/en/for-developers/).")


def test_decs_lists_confirmed_descriptors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_mesh_client(monkeypatch, [])
    _patch_decs_client(monkeypatch, "decs_words_with_qualifier.json")

    assert main(["decs", "tejido", "muscular"]) == 0

    assert capsys.readouterr().out == (
        "MeSH descriptors for 'tejido muscular' (through DeCS, confirmed in MeSH):\n"
        "  exact   Músculos -> Muscles (D009132)\n"
    )


def test_decs_without_results_or_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["decs", "cuidadores"]) == 2
    assert "DECS_API_KEY" in capsys.readouterr().err

    _patch_mesh_client(monkeypatch, [])
    _patch_decs_client(monkeypatch, "decs_words_empty.json")
    assert main(["decs", "qwzx"]) == 0
    assert capsys.readouterr().out == "DeCS has no MeSH descriptor for 'qwzx'\n"


def test_format_suggestion_marks_unconfirmed_descriptors() -> None:
    descriptor = DecsDescriptor("1", "D018482", "Muscle, Skeletal", ("Músculo Esquelético",))
    line = format_suggestion(DecsSuggestion(descriptor, None, exact=False))
    assert line == (
        "  related Músculo Esquelético -> Muscle, Skeletal (D018482): "
        "not confirmed in MeSH, not used"
    )


def _patch_counts(monkeypatch: pytest.MonkeyPatch, counts: dict[str, int]) -> None:
    @asynccontextmanager
    async def open_backend(
        database: Database, args: argparse.Namespace
    ) -> AsyncIterator[SearchBackend]:
        yield CountingBackend(counts)

    monkeypatch.setattr(cli, "_open_backend", open_backend)


def test_tune_prints_counts_levers_and_advice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    question, options = load_question(EXAMPLE)
    equation = PubMedDialect().render(question, options)
    _patch_counts(monkeypatch, {equation: 40})

    assert main(["tune", str(EXAMPLE), "--database", "pubmed"]) == 0

    out = capsys.readouterr().out
    assert out.startswith(f"Searching in: PubMed\n\nPubMed: 40 records\n  {equation}\n")
    assert "\nConditions (records on its own / total without it):\n  anxiety or depression: " in out
    levers = out.split("Levers, one at a time:\n")[1].split("\n\n")[0].splitlines()
    assert len(levers) == 6
    assert levers[0].startswith("  more sensitive also search every heading as free text: ")
    assert levers[-1].startswith("  more specific  ")
    assert "\nFewer than 100 records: if the topic has enough scientific output" in out
    assert out.rstrip().endswith("run again with --none-relevant.")


def test_tune_with_none_relevant_asks_how_to_narrow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_counts(monkeypatch, {})

    assert main(["tune", str(EXAMPLE), "--database", "openalex", "--none-relevant"]) == 0

    out = capsys.readouterr().out
    assert "Levers" not in out
    assert "None of the first 50 records is relevant" in out
    assert "  - add a condition the relevant records must meet" in out
    assert "--none-relevant." not in out
