---
name: handout-plots
description: Style guide for matplotlib figures in the podcast handout PDF, covering which plot type fits which content, layout for A4, and honest labelling. Use when writing plotting code for the handout.
---

# Handout plots (matplotlib)

## Rules for the code (enforced by the server)
- Each snippet draws **exactly one figure** using `plt` and `np`, which are already imported.
- Allowed imports: `matplotlib`, `numpy` and `math` only. No file access, no network, no `open`, `exec` or `eval`, no attributes starting with `_`.
- Do **not** call `savefig` or `show`: the server saves the current figure as PNG at 200 dpi.
- The default size is 6.5 × 3.8 inches (full A4 width). For another size, start with `fig, ax = plt.subplots(figsize=(6.5, 3))`.
- Keep it short and deterministic: no random data unless you seed it and label the plot "schematisch".

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
- German labels with units; a title that states the takeaway ("Neue Methode halbiert den Fehler"), not only the variable.
- Font size at least 9 pt.

## Honesty
- Use only numbers from the research notes, and name the source in the plot's `caption` field.
- If the data is illustrative, write "schematisch" in the title or caption.
