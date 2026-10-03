Research thoroughly before anything is written.

- Find the core source(s) first: if the topic names a paper, an author or an arXiv ID, find exactly that work.
- If only a topic is described: find the most important and most recent work on it and choose a clear focus.
- Gather context: prior work, follow-up work, critique, replications, practical applications.
- Record concrete numbers, magnitudes and examples, each with its source.
- Prefer primary sources (papers, preprints, official project pages) over press releases and blogs.
{% if research_depth == "quick" %}
Scope: quick. The core paper(s) and only the most essential context.
{% elif research_depth == "medium" %}
Scope: normal. The core paper(s) plus the key context: prior work, critique or follow-ups.
{% else %}
Scope: deep. Also follow citing and cited work, look specifically for critique and replications, and cover recent developments.
{% endif %}
