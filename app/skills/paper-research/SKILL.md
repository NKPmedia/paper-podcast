---
name: paper-research
description: Find and rate scientific papers via the arXiv, Semantic Scholar and OpenAlex APIs (with WebFetch). Use when researching a paper or research topic.
---

# Paper research

## Workflow

1. **Pin down the core paper(s).**
   - A title or author was given: search Semantic Scholar or arXiv for it (see `reference/apis.md`).
   - An arXiv ID was given: fetch `https://arxiv.org/abs/<id>` directly.
   - Only a topic was given: WebSearch for recent surveys and highly cited work, then pick a focus.
2. **Read the paper, not just the abstract.** Prefer the HTML version (`https://arxiv.org/html/<id>`) or the PDF. Extract: research question, method, key results with numbers, limitations named by the authors.
3. **Build context.**
   - Earlier work: the references the paper builds on.
   - Later work: citing papers via Semantic Scholar `citations`, sorted by influence or recency.
   - Critique: search for `"<title>" critique`, replication attempts, OpenReview comments.
   - Relevance: news coverage, applications, official project pages.
4. **Record provenance.** Every number or claim in your notes gets a short source tag like `[Smith 2024]` that matches the sources list.

## Source quality (best first)

1. Peer-reviewed paper or conference proceedings; the preprint itself for the core paper
2. Survey articles, OpenReview discussions, author talks and project pages
3. Reputable science journalism (Quanta, Nature News, Spektrum, heise)
4. Blogs and social media (use only for pointers, never as sole evidence)

Mark anything unconfirmed as such. If two sources disagree, note both.

## Tips

- WebFetch works best with a specific question, e.g. "List the main quantitative results with their numbers."
- For long PDFs, ask for one section at a time.
- Keep an eye on the date: note if newer results supersede the paper.
