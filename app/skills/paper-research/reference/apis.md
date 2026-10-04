# Scholarly APIs (free, no key needed; fetch with WebFetch)

## arXiv
- Search: `http://export.arxiv.org/api/query?search_query=all:<terms>&sortBy=submittedDate&sortOrder=descending&max_results=10`
  - Fields: `ti:` title, `au:` author, `abs:` abstract, `cat:` category (e.g. `cat:cs.CL`). Combine with `+AND+`.
- Abstract page: `https://arxiv.org/abs/<id>`
- Full text HTML (most papers since late 2023): `https://arxiv.org/html/<id>`
- PDF: `https://arxiv.org/pdf/<id>`

## Semantic Scholar Graph API
- Search: `https://api.semanticscholar.org/graph/v1/paper/search?query=<terms>&limit=10&fields=title,year,authors,citationCount,externalIds,abstract,url,venue,publicationTypes`
- A paper: `https://api.semanticscholar.org/graph/v1/paper/arXiv:<id>?fields=title,year,authors,abstract,citationCount,tldr,url`
  - Other ID forms: `DOI:<doi>`, or the S2 paper ID.
- Citing papers: `.../paper/<id>/citations?fields=title,year,citationCount,isInfluential&limit=20`
- References: `.../paper/<id>/references?fields=title,year,citationCount&limit=20`
- Rate limit without a key is low; if you get HTTP 429, wait or switch to OpenAlex.

## OpenAlex
- Search: `https://api.openalex.org/works?search=<terms>&sort=cited_by_count:desc&per-page=10`
- By DOI: `https://api.openalex.org/works/doi:<doi>`
- Recent works on a topic: add `&filter=from_publication_date:2025-01-01`
