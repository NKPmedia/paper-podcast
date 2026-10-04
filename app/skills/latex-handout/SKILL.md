---
name: latex-handout
description: Rules for the LaTeX document body of the handout - allowed commands and environments, formulas, tables, figures via \plot, special characters. Use when writing or fixing the handout.
---

# LaTeX for the handout

The server compiles the handout with **pdflatex**. It provides the preamble: the
document class, packages, header and title block. You write only the **body**,
which is inserted after the title.

## Already loaded (use freely)
`amsmath`, `amssymb`, `graphicx`, `booktabs`, `tabularx`, `array`, `enumitem`, `xcolor`
(color `accent`), `caption`, `float`, `hyperref`, `url`, `babel` (the episode language: ngerman or english), `microtype`.

## Not allowed (the server rejects the document)
- Any preamble material: `\documentclass`, `\usepackage`, `\begin{document}`, `\end{document}`.
- Defining macros: `\newcommand`, `\renewcommand`, `\def`, `\let`, `\newenvironment`.
- File or system access: `\input`, `\include`, `\includegraphics`, `\write`, `\read`, `\openin`,
  `\immediate`, `\special`, `\catcode`, `\csname`, `\makeatletter`, `^^` sequences.

## Structure
Write all headings and text in the episode language; the example is in English.
```latex
\section{What it is about}
A short introduction in two or three sentences.

\section{The core idea}
Text …

\begin{equation}
  \mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right) V
\end{equation}
In words: every word forms a weighted average over all other words …

\plot{results}

\section{Glossary}
\begin{description}[style=nextline]
  \item[Transformer] A neural network that …
\end{description}

\section{Sources}
\begin{itemize}
  \item Vaswani et al. (2017): Attention Is All You Need. \url{https://arxiv.org/abs/1706.03762}
\end{itemize}
```

## Figures
- Put `\plot{name}` on its own line, using the `name` of an entry in `plots`.
- The server inserts the figure together with its caption.
- Never use `\includegraphics` or a `figure` environment yourself.

## Tables
Use `booktabs`: `\toprule`, `\midrule`, `\bottomrule`, and no vertical lines. For wide text
columns, use `tabularx` with `\linewidth` and an `X` column.

## Typical errors (and how to avoid them)
- Escape special characters in text: `\%`, `\&`, `\_`, `\#`, `\$`. Write the tilde as `\textasciitilde{}`.
- Underscores and `^` belong only in math mode: `$d_k$`, not `d_k` in text.
- Quotation marks: in German `\glqq …\grqq{}` or „…“ written directly (UTF-8 works); in English ``` ``…'' ``` or “…”.
- Put URLs only in `\url{…}` or `\href{…}{…}`; `%` and `#` inside a URL need no escaping there.
- Close every environment you open, and balance all `{` and `}`.
- Use a `description` list instead of a two-column `tabular` with long text.
