# Peer review, top venues and reviews

Use this when a research request restricts sources to peer-reviewed or top work, or asks for
reviews and surveys. Decide from **API metadata**, never from memory.

## Is it peer-reviewed?

- Semantic Scholar: add `venue,publicationVenue,publicationTypes,journal,citationCount` to `fields`.
  - `publicationTypes` contains `JournalArticle`, `Conference` or `Review` and `venue` names a journal
    or conference → peer-reviewed (`yes`).
  - Only `arXiv`/`bioRxiv`/`SSRN` as venue, or no venue → preprint (`no`).
- OpenAlex: `primary_location.source.type` is `journal` or `conference` → `yes`; `repository` → `no`
  (check `locations` for a later journal version). `type` = `review` marks a review article.
- An arXiv preprint whose comments say "Accepted at ICLR 2024" (or similar) counts as `yes`; set
  `venue` accordingly. OpenReview pages show the decision ("Accept (poster)").
- Workshops at a conference are peer-reviewed but **not** top-tier.
- Blogs, news, white papers, theses and technical reports without review: `no`.
- When the metadata does not say: `unknown`. Do not guess `yes`.

## Is it top-tier?

`top_tier` is true when **either** holds:

1. **Top venue** (main track, not workshops), for example:
   - Machine learning / AI: NeurIPS, ICML, ICLR, AAAI, IJCAI, JMLR, TMLR, TPAMI
   - Vision: CVPR, ICCV, ECCV · Language: ACL, EMNLP, NAACL, TACL · Data mining: KDD, WWW
   - Robotics / RL: CoRL, RSS, ICRA, IJRR, Science Robotics
   - Systems / theory / security: OSDI, SOSP, STOC, FOCS, CCS, USENIX Security, IEEE S&P
   - Multidisciplinary: Nature, Science, Cell, PNAS, Nature family journals (e.g. Nature Physics,
     Nature Machine Intelligence), Science Advances
   - Medicine: NEJM, The Lancet, JAMA, BMJ · Physics: PRL, Reviews of Modern Physics
   - Reviews: Annual Reviews series, Nature Reviews journals, Chemical Reviews, Physics Reports,
     ACM Computing Surveys
   For other fields, use the field's flagship journals and conferences (as rated e.g. by CORE A*
   or the field's top journals by impact); say which in `reason`.
2. **Top paper**: very highly cited for its age and field, e.g. roughly 500+ citations for a paper
   older than three years, or clearly among the most cited of its subfield in the last two years.
   Use `citationCount` / `cited_by_count`; state the count in `citations`.

## Is it a review?

`is_review` is true for review articles, surveys, tutorials and systematic reviews (titles with
"A survey of", "A review", "Tutorial", "Overview", "Systematic review"; `publicationTypes` contains
`Review`; OpenAlex `type` = `review`). Prefer the **most recent** comprehensive ones: a review older
than about four years misses the current state in fast-moving fields.

## Searching for them

- Semantic Scholar: `.../paper/search?query=<topic> survey&year=2022-&fields=...` and filter by
  `publicationTypes`, or use `venue=` to restrict to a venue.
- OpenAlex: `https://api.openalex.org/works?search=<topic>&filter=type:review,from_publication_date:2022-01-01&sort=cited_by_count:desc&per-page=10`
  and `filter=primary_location.source.type:journal` for journal articles only.
