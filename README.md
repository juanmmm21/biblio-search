# biblio-search

Turn a structured clinical question into a rigorous, reproducible PubMed search equation.

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
4. **Tag every term.** Headings get `[mh]`, `[mj]`, `[mh:noexp]` or `[mj:noexp]`, from most to
   least sensitive; free text gets `[tiab]` or `[ti]`.
5. **Tune sensitivity and specificity.** Repeat headings as free text to catch records not
   yet indexed, or limit free text to non-MEDLINE records with `NOT MEDLINE[sb]` for a more
   specific search.

## Engineering highlights

- **The domain is the validation layer.** Invalid searches cannot be represented: truncated
  MeSH headings, stray asterisks, embedded quotes or brackets, repeated terms, components that
  do not belong to the chosen format, or a question with nothing to search all raise a
  `QuestionError` with a precise message.
- **Quoting is decided, not guessed.** Terms are quoted only when PubMed would otherwise
  misread them (commas, parentheses, words like `and`/`or`/`not`, or truncated phrases),
  because unnecessary quotes reduce recall.
- **Dialects behind a protocol.** Query generation sits behind a small `QueryDialect`
  interface, so other databases can get their own syntax without touching the model.
- **Strict typing, no runtime dependencies.** Frozen dataclasses and enums, `mypy --strict`
  on sources and tests, and only the standard library at runtime (`tomllib` for input).

## Architecture

```text
biblio-search/
├── src/biblio_search/
│   ├── models.py         # SPC/PICO question, components, conditions, terms, strategy options
│   ├── protocols.py      # QueryDialect interface
│   ├── pubmed_query.py   # PubMed field tags, quoting rules and equation assembly
│   ├── question_file.py  # TOML question files -> validated domain objects
│   └── __main__.py       # command line interface
├── examples/
│   └── caregiver-burden.toml   # worked example from Del Pino (2017)
└── tests/                # unit tests, no network access
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

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check src tests
mypy --strict src tests
```

The test suite never touches the network.

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

## License

MIT, see [LICENSE](LICENSE).
