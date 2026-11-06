# biblio-search

Turn a structured clinical question into a rigorous, reproducible PubMed search, and run it.

biblio-search models the bibliographic search method taught to health-science students:
decompose the question, map every concept to controlled vocabulary and free text, tag each
term with the right field, and assemble everything into a single boolean equation whose
sensitivity and specificity you can tune on purpose instead of by trial and error.

## What problem it solves

Writing a good PubMed search by hand is error prone. AND and OR are evaluated left to right,
so a missing parenthesis silently changes the meaning of the whole query. MeSH headings
explode into their subcategories unless told otherwise, and free-text terms are searched in
every field unless restricted to title and abstract. Multi-word truncation only works inside
quotes, but quoting everything makes the search too narrow. These are exactly the details
that end up wrong in coursework, final-year projects and quick clinical look-ups.

biblio-search lets you describe the question once, as data, and generates the equation
following explicit rules. The question file documents the search, so the same strategy can be
rerun, reviewed or adjusted later.

## The method

The tool follows the approach described by Rafael del Pino Casado (Department of Nursing,
University of Jaén) in *"Cómo aprender (y enseñar) a realizar búsquedas en CINAHL y PubMed"*
(Evidentia, 2017):

1. **Structure the question.** Use **SPC** (health *Situation*, *Population*, study
   *Question*), which fits any study design, or **PICO** (*Population*, *Intervention*,
   *Comparison*, *Outcome*) for intervention studies. A PICO element can be described and
   left out of the search, as the Cochrane Handbook recommends.
2. **Turn components into conditions.** Every condition is something each retrieved record
   must satisfy; conditions are joined with **AND**.
3. **Fill every condition with terms.** Controlled headings (MeSH) plus free-text synonyms
   with truncation; the terms of a condition are alternatives joined with **OR**.
4. **Confirm the headings in the thesaurus.** A MeSH heading is only useful if it exists:
   biblio-search looks every controlled term up in NCBI's MeSH database, points entry terms
   to their descriptor and searches anything that is not a heading as free text.
5. **Tag every term.** Headings get `[mh]`, `[mj]`, `[mh:noexp]` or `[mj:noexp]`, from most to
   least sensitive; free text gets `[tiab]` or `[ti]`.
6. **Tune sensitivity and specificity.** Repeat headings as free text to catch records not
   yet indexed, or limit free text to non-MEDLINE records with `NOT MEDLINE[sb]` for a more
   specific search.
7. **Run it and read what the database understood.** biblio-search sends the equation to
   PubMed through NCBI E-utilities and shows the record count, PubMed's own translation of
   the query, any term it did not recognise and the first records to screen.

## Engineering highlights

- **The domain is the validation layer.** Invalid searches cannot be represented: truncated
  MeSH headings, stray asterisks, embedded quotes or brackets, repeated terms, components that
  do not belong to the chosen format, or a question with nothing to search all raise a
  `QuestionError` with a precise message.
- **Quoting is decided, not guessed.** Terms are quoted only when PubMed would otherwise
  misread them (commas, parentheses, words like `and`/`or`/`not`, or truncated phrases),
  because unnecessary quotes reduce recall.
- **PubMed is never trusted blindly.** PubMed silently remaps headings and drops broken
  syntax (an unbalanced parenthesis simply disappears). Every search prints the query
  translation and surfaces NCBI's `phrasesnotfound` and warning lists.
- **Headings are checked, never assumed.** `check` confirms every controlled term against
  the MeSH database (`db=mesh`), maps entry terms such as *Carers* to their descriptor
  (*Caregivers*), discards subheadings that share the index, and rewrites MeSH entry terms
  into the natural, comma-free synonyms an author would actually write. The lookup sits
  behind a `Thesaurus` protocol so other vocabularies can plug in.
- **A polite async client.** The E-utilities client is built on `httpx.AsyncClient`, spaces
  requests to NCBI's quota (3 per second, 10 with an API key), always identifies the tool,
  retries 429/5xx and network errors with backoff (honouring `Retry-After`, including NCBI's
  rate-limit errors served with HTTP 200) and batches summary requests.
- **Offline, deterministic tests.** Real NCBI responses are recorded as fixtures and an
  autouse guard makes any test that opens a socket fail.
- **Dialects behind a protocol.** Query generation sits behind a small `QueryDialect`
  interface, so other databases can get their own syntax without touching the model.
- **Strict typing, one runtime dependency.** Frozen dataclasses and enums, `mypy --strict`
  on sources and tests, and only `httpx` beyond the standard library.

## Architecture

```text
biblio-search/
├── src/biblio_search/
│   ├── models.py         # SPC/PICO question, components, conditions, terms, strategy options
│   ├── protocols.py      # QueryDialect and Thesaurus interfaces
│   ├── pubmed_query.py   # PubMed field tags, quoting rules and equation assembly
│   ├── question_file.py  # TOML question files -> validated domain objects
│   ├── ratelimit.py      # async requests-per-second limiter
│   ├── eutils.py         # NCBI E-utilities client (esearch, esummary) and response parsing
│   ├── mesh.py           # MeSH lookups through E-utilities (db=mesh)
│   ├── check.py          # heading checks, synonym suggestions, unknown terms to free text
│   ├── pipeline.py       # question -> equation -> search -> records to screen
│   └── __main__.py       # command line interface (build, check, search)
├── examples/
│   └── caregiver-burden.toml   # worked example from Del Pino (2017)
└── tests/                # unit tests and recorded NCBI fixtures, no network access
```

## Installation

Requires Python 3.11 or newer.

```bash
git clone https://github.com/juanmmm21/biblio-search.git
cd biblio-search
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

Describe the question in a TOML file. This is the worked example from Del Pino's article:
*What is the relationship between burden and anxiety or depression in family caregivers of
older people?*

```toml
question = "What is the relationship between burden and anxiety or depression in family caregivers of older people?"
format = "spc"

[[components]]
role = "situation"
description = "Anxiety and depression in caregivers"

[[components.conditions]]
label = "anxiety or depression"
controlled = [
    { term = "Anxiety", scope = "major_no_explode" },
    { term = "Depression", scope = "major" },
]
free = ["anxiety", "depress*"]

[[components]]
role = "population"
description = "Family caregivers of older people"

[[components.conditions]]
label = "caregivers"
controlled = [{ term = "Caregivers", scope = "major" }]
free = ["caregiv*", "carer*"]

[[components.conditions]]
label = "older people"
controlled = ["Aged"]
free = ["elder*", "older people"]

[[components]]
role = "question"
description = "Relationship with caregiver burden"

[[components.conditions]]
label = "burden"
free = ["burden*", "overload*", "strain*"]
```

Build the equation and paste it into PubMed:

```console
$ biblio-search build examples/caregiver-burden.toml
(Anxiety[mj:noexp] OR Depression[mj] OR anxiety[tiab] OR depress*[tiab]) AND (Caregivers[mj] OR caregiv*[tiab] OR carer*[tiab]) AND (Aged[mh] OR elder*[tiab] OR older people[tiab]) AND (burden*[tiab] OR overload*[tiab] OR strain*[tiab])
```

`Aged` keeps its default `[mh]` scope on purpose: exploding it also retrieves
*Aged, 80 and over*, which the population needs.

Make the search more specific by limiting free text to records not yet indexed in MEDLINE,
and more sensitive by repeating every heading as free text. Headings already covered by a
truncated synonym (`Caregivers` by `caregiv*`) are not repeated:

```console
$ biblio-search build examples/caregiver-burden.toml --unindexed-only --repeat-controlled-as-free
(Anxiety[mj:noexp] OR Depression[mj] OR ((anxiety[tiab] OR depress*[tiab]) NOT MEDLINE[sb])) AND (Caregivers[mj] OR ((caregiv*[tiab] OR carer*[tiab]) NOT MEDLINE[sb])) AND (Aged[mh] OR ((elder*[tiab] OR older people[tiab] OR aged[tiab]) NOT MEDLINE[sb])) AND (burden*[tiab] OR overload*[tiab] OR strain*[tiab])
```

The MEDLINE filter is only applied to conditions that also have headings, so a free-text-only
condition never loses the indexed records. Avoid it for systematic reviews, which need the
extra sensitivity.

### Checking headings against MeSH

`check` looks up every controlled term in NCBI's MeSH database before you search:

```console
$ biblio-search check examples/caregiver-burden.toml
MeSH check of 4 controlled terms
  ok          Anxiety (D001007) [anxiety or depression]
  ok          Depression (D003863) [anxiety or depression]
  ok          Caregivers (D017028) [caregivers]
  ok          Aged (D000368) [older people]
              also retrieves its narrower headings; use a no_explode scope to leave them out

Synonyms you could add as free text:
  anxiety or depression: Angst, Nervousness, Hypervigilance, Social Anxiety, Anxiousness, Emotional Depression
  caregivers: Care Givers, Family Caregivers, Spouse Caregivers, Informal Caregivers

Equation after the check: (Anxiety[mj:noexp] OR Depression[mj] OR anxiety[tiab] OR depress*[tiab]) AND (Caregivers[mj] OR caregiv*[tiab] OR carer*[tiab]) AND (Aged[mh] OR elder*[tiab] OR older people[tiab]) AND (burden*[tiab] OR overload*[tiab] OR strain*[tiab])
```

- **ok**: the term is a MeSH descriptor. A note tells you when the scope matters: a heading
  with narrower headings explodes unless you pick a `*_no_explode` scope, and a no-explode
  scope on a heading without narrower ones changes nothing.
- **entry term**: the text is a synonym of a descriptor (`Carers -> Caregivers (D017028)`);
  the corrected equation uses the descriptor.
- **not found**: the text is not a MeSH heading, so it is searched as free text instead of
  silently matching nothing as `[mh]`.

Suggested synonyms come from the descriptor's entry terms, put back in natural order
(*Depression, Emotional* becomes *Emotional Depression*) and left out when a free term you
already have covers them (`caregiv*` covers *Caregiver*). Nothing is written to your file:
copy the ones that fit your question.

### Running the search in PubMed

`search` builds the same equation, runs it through NCBI E-utilities and lists the first
records (20 by default, up to 10,000):

```console
$ biblio-search search examples/caregiver-burden.toml --max 3
Equation: (Anxiety[mj:noexp] OR Depression[mj] OR anxiety[tiab] OR depress*[tiab]) AND (Caregivers[mj] OR caregiv*[tiab] OR carer*[tiab]) AND (Aged[mh] OR elder*[tiab] OR older people[tiab]) AND (burden*[tiab] OR overload*[tiab] OR strain*[tiab])
PubMed understood: ("anxiety"[MeSH Major Topic:noexp] OR ("depressive disorder"[MeSH Major Topic] OR "depression"[MeSH Major Topic]) OR "anxiety"[Title/Abstract] OR "depress*"[Title/Abstract]) AND ("caregivers"[MeSH Major Topic] OR "caregiv*"[Title/Abstract] OR "carer*"[Title/Abstract]) AND ("aged"[MeSH Terms] OR "elder*"[Title/Abstract] OR "older people"[Title/Abstract]) AND ("burden*"[Title/Abstract] OR "overload*"[Title/Abstract] OR "strain*"[Title/Abstract])
Results: 2808

  1. PMID 42970524 (2026) Music-Based Interventions for People Living With Dementia. | Home healthcare now [Journal Article, Review]
  2. PMID 42956697 (2026) Family functioning and depressive symptoms among caregivers of disabled old people: A multiple mediation model. | Geriatric nursing (New York, N.Y.) [Journal Article]
  3. PMID 42919807 (2026) "The Effect of Forgiveness Psychoeducation on Death Anxiety, Caregiver Burden, and Forgiveness Tendencies in Caregivers of Patients at the End of Life: A Randomized Controlled Trial": Şahin 49(3) May/June 2026. | Cancer nursing [Journal Article, Randomized Controlled Trial]
```

Counts change as PubMed grows. The translation line is worth reading: here PubMed expanded
`Depression[mj]` to also cover *depressive disorder*. Unknown terms are listed as
`Not found:` and anything PubMed ignored as `Warning:`.

NCBI asks every tool to identify itself. biblio-search always sends `tool=biblio-search`;
set a contact email and, optionally, a free
[NCBI API key](https://www.ncbi.nlm.nih.gov/books/NBK25497/) to raise the limit from 3
to 10 requests per second:

```bash
export NCBI_EMAIL="you@example.org"   # or pass --email
export NCBI_API_KEY="..."             # optional, read from the environment only
```

### Question file reference

- `question` (string) and `format` (`spc` or `pico`) are required.
- `[[components]]`: `role` (`situation`, `population`, `question` for SPC; `population`,
  `intervention`, `comparison`, `outcome` for PICO), a `description`, and zero or more
  `[[components.conditions]]`.
- A condition has a `label` and at least one term:
  - `controlled`: strings, or `{ term, scope }` with `scope` one of `heading` (`[mh]`, default),
    `major` (`[mj]`), `heading_no_explode` (`[mh:noexp]`), `major_no_explode` (`[mj:noexp]`).
  - `free`: strings, or `{ term, field }` with `field` one of `title_abstract` (`[tiab]`,
    default) or `title` (`[ti]`). A trailing `*` truncates the term.
- `[[exclusions]]`: conditions appended with `NOT`, e.g. animal studies.
- `[options]`: `repeat_controlled_as_free` and `free_text_unindexed_only` (booleans). The
  command line flags can switch them on, never off.

## Programmatic use

Build an equation from code:

```python
from biblio_search.models import (
    Component, ComponentRole, Condition, ControlledScope,
    QuestionFormat, StrategyOptions, StructuredQuestion, Term,
)
from biblio_search.pubmed_query import PubMedDialect

question = StructuredQuestion(
    text="Does exercise prevent falls in older adults?",
    format=QuestionFormat.PICO,
    components=(
        Component(ComponentRole.POPULATION, "older adults", (
            Condition("older adults", (Term.controlled("Aged"), Term.free("older adult*"))),
        )),
        Component(ComponentRole.INTERVENTION, "exercise", (
            Condition("exercise", (
                Term.controlled("Exercise", ControlledScope.MAJOR), Term.free("exercis*"),
            )),
        )),
        Component(ComponentRole.OUTCOME, "falls", (
            Condition("falls", (Term.controlled("Accidental Falls"), Term.free("fall*"))),
        )),
        Component(ComponentRole.COMPARISON, "usual care"),
    ),
)

print(PubMedDialect().render(question, StrategyOptions()))
# (Aged[mh] OR "older adult*"[tiab]) AND (Exercise[mj] OR exercis*[tiab]) AND (Accidental Falls[mh] OR fall*[tiab])
```

Or run a question end to end with the async client:

```python
import asyncio
from pathlib import Path

from biblio_search.eutils import EutilsClient, EutilsConfig
from biblio_search.pipeline import run_search
from biblio_search.pubmed_query import PubMedDialect
from biblio_search.question_file import load_question


async def main() -> None:
    question, options = load_question(Path("examples/caregiver-burden.toml"))
    async with EutilsClient(EutilsConfig(email="you@example.org")) as client:
        report = await run_search(question, options, PubMedDialect(), client, max_records=5)
    print(report.result.count, [record.title for record in report.records])


asyncio.run(main())
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check src tests
mypy --strict src tests
```

The test suite never touches the network: NCBI responses are recorded under
`tests/fixtures/` and opening a socket during a test makes it fail.

## Troubleshooting

- **`controlled term 'X*' cannot be truncated`**: truncation only applies to free text. A
  truncated MeSH heading stops being a heading; move it to `free`.
- **PubMed returns far more records than expected**: check that headings you meant to keep
  narrow use `major` or a `*_no_explode` scope, and that the search is not mixing many broad
  synonyms in one condition.
- **Very few results on a well-studied topic**: drop `free_text_unindexed_only`, add
  `repeat_controlled_as_free`, or widen scopes from `major` to `heading`. As a rule of thumb,
  be suspicious of fewer than 100 results on a topic with plenty of literature.
- **A heading with commas is not repeated as free text**: headings such as
  *Aged, 80 and over* have no natural free-text form; add the synonyms you want under `free`.

- **`check` says a heading is `not found`**: MeSH matches headings and entry terms exactly,
  so plurals, abbreviations or Spanish terms are not found. Look the concept up in the
  [MeSH Browser](https://meshb.nlm.nih.gov/) or keep it as free text.
- **`NCBI ... failed after 4 attempts: HTTP 429`**: too many requests from your IP, often
  several tools sharing one connection. Wait a minute, or set `NCBI_API_KEY`.
- **`Warning: (` or a translation shorter than the equation**: PubMed dropped part of the
  query, usually an unbalanced parenthesis in a hand-edited equation.

## License

MIT, see [LICENSE](LICENSE).
