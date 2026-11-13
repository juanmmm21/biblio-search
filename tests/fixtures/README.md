Responses recorded from NCBI E-utilities on 2026-11-06 (retmode=json):

- `esearch_caregiver.json`: the equation built from `examples/caregiver-burden.toml`, retmax=3.
- `esearch_not_found.json`: `caregiv*[tiab] AND qwzxvplorkt[tiab]` (unknown phrase).
- `esearch_syntax.json`: `(anxiety[tiab]` (unbalanced parenthesis, silently dropped).
- `esummary_caregiver.json`: summaries of the three PMIDs in `esearch_caregiver.json`.

MeSH database (`db=mesh`), recorded the same day:

- `mesh_esearch_<heading>.json`: `"<heading>"[MeSH Terms]` for Anxiety, Depression,
  Caregivers, Aged, Carers (an entry term of Caregivers), "Aged, 80 and over" (a heading with
  commas), Nursing (a descriptor and a qualifier) and Qwzxvplorkt (`not_found`).
- `mesh_esearch_muscles.json`: `"Muscles"[MeSH Terms]`, recorded on 2026-11-07 for the DeCS
  tests.
- `mesh_esummary.json`: summaries of every uid returned by those searches (re-recorded on
  2026-11-07 with Muscles added; the other records came back unchanged).

OpenAlex works API (`api.openalex.org/works`, no key), recorded on 2026-11-06:

- `openalex_search_caregiver.json`: the OpenAlex equation of `examples/caregiver-burden.toml`
  as `search.title_and_abstract.exact`, `select=id`, `per_page=100`, page 1.
- `openalex_works_caregiver.json`: `filter=ids.openalex:` the first three of those works,
  with the fields biblio-search reads (re-recorded on 2026-11-08 with `ids`, which carries
  the PMID).
- `openalex_links_zarit.json`: `filter=doi:10.1093/geront/20.6.649` (Zarit et al., 1980,
  W2130900065) with `select=id,doi,ids,referenced_works`, recorded on 2026-11-13.
- `openalex_references_zarit.json`: `filter=ids.openalex:` the first three of its 25
  `referenced_works` plus `W99999999999`, which does not exist, with the summary fields
  (3 results), recorded on 2026-11-13.
- `openalex_citing_zarit.json`: `filter=cites:W2130900065`, `sort=cited_by_count:desc`,
  `per_page=3`, page 1, with the summary fields (5968 citing works), recorded on 2026-11-13.
- `openalex_error_wildcard.json`: the HTTP 400 answer to a wildcard inside a quoted phrase
  (`"family caregiv*"`).

DeCS API (`api.bvsalud.org/decs/v2/search-by-words`, `lang=es`, `format=json`), recorded on
2026-11-09 with a licence key (the key travels in a header and is not in the files). The real
answers are large (25 to 33 objects), so each fixture keeps only the objects the tests need,
unchanged:

- `decs_words_musculos.json`: `words=musculos`, trimmed to its Muscles hit (D009132).
- `decs_words_with_qualifier.json`: that hit plus the qualifier hit (/poisoning, Q000506) of
  `words=envenenamiento`.
- `decs_words_empty.json`: `words=xyzqwv`, a search with no hits (HTTP 200, empty list).

Ollama chat API (`/api/chat`, `stream: false`, `think: false`, `temperature: 0`), recorded on
2026-11-09 from a local Ollama with `qwen3:8b`, for the question *¿El ejercicio en el agua
mejora el dolor en la fibromialgia comparado con el ejercicio en tierra?*:

- `ollama_classify_fibromyalgia.json`: the classification (health, PICO).
- `ollama_draft_fibromyalgia.json`: the PICO decomposition with its schema.
- `ollama_error_model_not_found.json`: the HTTP 404 body for a model that is not pulled.

arXiv API (`export.arxiv.org/api/query`, Atom), recorded on 2026-11-13:

- `arxiv_search_qec.xml`: the arXiv equation of `examples/neural-decoders-qec.toml`,
  `start=0`, `max_results=3` (115 results; the third one carries a journal reference and DOI).
- `arxiv_search_empty.xml`: `ti:qwzxvplorkt OR abs:qwzxvplorkt`, a search with no results.
- `arxiv_error_syntax.xml`: the HTTP 400 error feed for `(ti:qubit` (unbalanced parenthesis).
- `arxiv_id_list.xml`: `id_list=1706.03762,hep-th/9711200,2007.05129`, which arXiv answered
  in a different order from the one asked for.

ERIC API (`api.ies.ed.gov/eric/`, `format=json`, no key), recorded on 2026-11-13:

- `eric_search_peer_tutoring.json`: the ERIC equation of `examples/peer-tutoring-reading.toml`,
  `rows=3`, with the fields biblio-search reads (160 results; the second title carries the
  `&apos;` entity ERIC leaves in some titles).
- `eric_error_syntax.json`: `title:(peer` (unbalanced parenthesis), which Solr rejects with
  an `error` object and HTTP 200.
- `eric_id_list.json`: `id:(EJ1257338 OR ED395269 OR EJ888659)`, which ERIC answers in its
  own order.
- `eric_subject_<term>.json`: `subject:"<term>"`, `rows=50`, `fields=subject`, for peer
  teaching (a descriptor), peer tutoring (a synonym that slipped into one record) and qwzx
  learning (nothing).
- `eric_count_<term>.json`: `subject_ss:"<Term>"`, `rows=0`: 6447 records carry Peer Teaching
  and one carries Peer Tutoring.

ERIC Thesaurus (`ERICThesaurus2025.zip` from eric.ed.gov/?download, January 2025 edition):

- `eric_thesaurus_excerpt.xml`: seven terms of the XML file, unchanged: the descriptors Peer
  Teaching, Tutoring, Reading Comprehension and Elementary School Students, the synonyms Peer
  Tutoring (used for two descriptors) and Tutorial Instruction, and the dead descriptor
  American Culture (1966 1980).
