Responses recorded from NCBI E-utilities on 2026-11-06 (retmode=json):

- `esearch_caregiver.json`: the equation built from `examples/caregiver-burden.toml`, retmax=3.
- `esearch_not_found.json`: `caregiv*[tiab] AND qwzxvplorkt[tiab]` (unknown phrase).
- `esearch_syntax.json`: `(anxiety[tiab]` (unbalanced parenthesis, silently dropped).
- `esummary_caregiver.json`: summaries of the three PMIDs in `esearch_caregiver.json`.

MeSH database (`db=mesh`), recorded the same day:

- `mesh_esearch_<heading>.json`: `"<heading>"[MeSH Terms]` for Anxiety, Depression,
  Caregivers, Aged, Carers (an entry term of Caregivers), "Aged, 80 and over" (a heading with
  commas), Nursing (a descriptor and a qualifier) and Qwzxvplorkt (`not_found`).
- `mesh_esummary.json`: summaries of every uid returned by those searches.

OpenAlex works API (`api.openalex.org/works`, no key), recorded on 2026-11-06:

- `openalex_search_caregiver.json`: the OpenAlex equation of `examples/caregiver-burden.toml`
  as `search.title_and_abstract.exact`, `select=id`, `per_page=100`, page 1.
- `openalex_works_caregiver.json`: `filter=ids.openalex:` the first three of those works,
  with the fields biblio-search reads.
- `openalex_error_wildcard.json`: the HTTP 400 answer to a wildcard inside a quoted phrase
  (`"family caregiv*"`).
