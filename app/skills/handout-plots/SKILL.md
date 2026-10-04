---
name: handout-plots
description: Style rules for matplotlib figures in the handout PDF - the right chart type, A4 layout, honest labelling. Use when writing plot code.
---

# Handout plots (matplotlib)

## Rules for the code (enforced by the server)
- Each snippet draws **exactly one figure** using `plt` and `np`, which are already imported.
- Allowed imports: `matplotlib`, `numpy` and `math` only. No file access, no network, no `open`, `exec` or `eval`, no attributes starting with `_`.
- Do **not** call `savefig` or `show`: the server saves the current figure as a vector PDF for the LaTeX handout.
- The default size is 6.5 × 3.8 inches (full A4 width). For another size, start with `fig, ax = plt.subplots(figsize=(6.5, 3))`.
- Keep it short and deterministic: no random data unless you seed it and label the plot as schematic ("schematic" / "schematisch").

## Choosing a plot
| Content | Plot |
|---|---|
| Result compared across methods | horizontal bar chart, sorted |
| Development over time | line chart or timeline with annotations |
| Trade-off between two quantities | scatter with labelled points |
| Process or architecture | simple schematic with boxes and arrows (`matplotlib.patches`) |
| Proportions | stacked bar (avoid pie charts) |

## Style
- One accent color (`#2F6DB5`) for the key item and grey (`#9AA0A6`) for the rest.
- Remove the top and right spines; use light horizontal gridlines only when needed.
- Labels with units in the episode language; a title that states the takeaway ("New method halves the error" / "Neue Methode halbiert den Fehler"), not only the variable.
- Font size at least 9 pt.

## Honesty
- Use only numbers from the research notes, and name the source in the plot's `caption` field. The caption is LaTeX text, so escape `%`, `&`, `_` and `#`.
- If the data is illustrative, say "schematic" ("schematisch" in German) in the title or caption.
