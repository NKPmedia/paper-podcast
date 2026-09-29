---
name: fact-check
description: Skript vor der Abgabe gegen Recherche-Notizen und Volltexte prüfen: jede Aussage, Zahl und Zuschreibung. Nutzen als letzten Schritt vor der Rückgabe eines Skripts.
---

# Fact-check a script

Go through the script chapter by chapter:

1. **Numbers.** Every number, percentage or magnitude must appear in the research notes, attributed to a source. If not, remove it or make it qualitative ("deutlich schneller").
   - If the notes point to a full text (`papers/<file>.md:<from>-<to>`), open that passage with Read (`offset`/`limit`) and confirm the number and its context: unit, dataset, compared to what.
   - Check at least the central claims of every chapter this way, and every number whose note looks ambiguous or rounded.
2. **Attributions.** Names, institutions and years must match the sources list.
3. **Claims.** Results must not be overstated. "Zeigt" versus "deutet darauf hin" matters. Keep the authors' own caveats.
4. **Explanations.** Analogies must not be misleading in a way that changes the science. If an analogy has a known limit, let the expert say so briefly.
5. **Balance.** Limitations or criticism from the notes must be mentioned at least once.
6. **Formulas.** No formula is read out, apart from very short, famous ones. Replace any other with a verbal explanation. Every reference to the handout must have a matching entry in `handout_items`, and there must be no handout references at all when there is no handout.
7. **Length and flow.** Check the total word count against the target. Cut repetition; merge turns that say the same thing twice.

If a note and the paper disagree, the paper wins.

Fix problems directly in the script. Do not add a list of changes to the output.
