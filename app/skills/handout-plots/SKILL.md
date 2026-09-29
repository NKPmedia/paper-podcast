---
name: handout-plots
description: Style guide for matplotlib figures in the podcast handout PDF, covering which plot type fits which content, layout for A4, and honest labelling. Use when writing plotting code for the handout.
---

# Handout plots (matplotlib)

## Rules for the code
- Plain matplotlib (numpy allowed). No seaborn, no network access, no reading files.
- One figure per function; save with `fig.savefig("figures/<name>.png", dpi=200, bbox_inches="tight")`.
- Size: `figsize=(6.5, 3.8)` for full width on A4.
- Do not call `plt.show()`.

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
- Use only numbers from the research notes and put the source in a caption line under the plot (`fig.text`).
- If the data is illustrative, write "schematisch" in the title or caption.
