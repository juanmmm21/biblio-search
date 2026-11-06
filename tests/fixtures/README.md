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
