---
name: tts-friendly-text
description: Rules so that speech synthesis pronounces German or English text correctly - numbers, units, abbreviations, foreign terms; never read out formulas. Use for any text that will be read aloud.
---

# TTS-friendly text

The script is read verbatim by a speech synthesizer, so write exactly what should be *said*, in the episode language.

## Numbers
- Small numbers in words: "drei Modelle" / "three models", not "3".
- Big numbers rounded and spoken: "rund zwölf Millionen" / "about twelve million", not "12,043,211".
- Decimals: "zwei Komma fünf" / "two point five".
- Percentages: "vierzig Prozent" / "forty percent", not "40 %".
- Years may stay as digits ("2024"); TTS reads them correctly.
- Ranges: "zwischen zehn und zwanzig" / "between ten and twenty", not "10–20".

## Units and symbols
- Always spell out units: "Kilometer pro Stunde" / "kilometers per hour", "Nanometer" / "nanometers".
- No symbols: "etwa" / "about" instead of "~", "mal" / "times" instead of "×", "größer als" / "greater than" instead of ">".

## Formulas: never read them out
Listeners cannot see a formula, and a spoken formula of any length is lost immediately.
- Explain what the formula *says* and *why it matters*, in words and with an example:
  "The cost grows quadratically: twice the text, four times the work."
- Only very short, widely known relations may be named ("E gleich m c Quadrat" / "E equals m c squared").
- Anything longer, such as fractions, sums, indices, matrices or Greek-letter chains, never appears in the script.
- If there is a handout, refer to it and list the formula in `handout_items`. Without a handout, do not point listeners to formulas they cannot look up.

## Abbreviations
- Common initialisms are read letter by letter anyway: KI/AI, DNA, USA, EU.
- Acronyms spoken as words stay: NASA, CERN.
- Spell out Latin and German abbreviations: "zum Beispiel" (not "z. B."), "das heißt" (not "d. h."); in English "for example" (not "e.g."), "that is" (not "i.e.").
- Uncommon abbreviations: say the long form on first use.
- See `reference/pronunciation.md` for tricky terms.

## Foreign terms
- German episodes: keep established English terms ("Machine Learning", "Transformer", "Large Language Model"); the multilingual voices pronounce them fine. Avoid hyphen chains mixing English and German in one compound.
- English episodes: use English terms throughout; translate German names of institutions only if an English name is common.

## Punctuation
- Punctuation shapes the rhythm: a comma is a short pause, a dash a slightly longer one, "…" a hesitation.
- No brackets: turn the content into its own sentence.
- No quotation marks around single words.
