---
name: tts-friendly-text
description: Rules for writing German text so a speech synthesizer (Edge TTS, Gemini TTS) pronounces it correctly, covering numbers, units, formulas, abbreviations and English terms. Use when writing any text that will be read aloud.
---

# TTS-friendly German text

The script is read verbatim by a speech synthesizer, so write exactly what should be *said*.

## Numbers
- Small numbers in words: "drei Modelle", not "3 Modelle".
- Big numbers rounded and spoken: "rund zwölf Millionen", not "12.043.211".
- Decimals: "zwei Komma fünf", not "2,5" or "2.5".
- Percentages: "vierzig Prozent", not "40 %".
- Years may stay as digits ("2024"); TTS reads them correctly.
- Ranges: "zwischen zehn und zwanzig", not "10–20".

## Units and symbols
- "Kilometer pro Stunde", "Grad Celsius", "Milliampere-Stunden", "Nanometer": always spelled out.
- No symbols: "etwa" instead of "~", "mal" instead of "×", "größer als" instead of ">".

## Formulas
Describe them instead of reading them out: "Die Energie ist gleich Masse mal Lichtgeschwindigkeit zum Quadrat", or better, explain the idea in words.

## Abbreviations
- Common initialisms are read letter by letter anyway: KI, DNA, USA, EU.
- Acronyms spoken as words stay: NASA, CERN.
- Spell out Latin/German abbreviations: "zum Beispiel" (not "z. B."), "das heißt" (not "d. h."), "et cetera".
- Uncommon abbreviations: say the long form on first use.
- See `reference/aussprache.md` for tricky terms.

## English terms
Keep established terms ("Machine Learning", "Transformer", "Large Language Model"). The multilingual voices pronounce them fine. Avoid mixing English inside a German compound with a hyphen chain.

## Punctuation
- Punctuation shapes the rhythm: a comma is a short pause, a dash a slightly longer one, "…" a hesitation.
- No brackets: turn the content into its own sentence.
- No quotation marks around single words.
