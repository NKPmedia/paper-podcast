Das Skript wird von einer Sprachsynthese (TTS) vorgelesen. Schreibe für das Ohr:

- Kurze bis mittellange Sätze. Eine Äußerung umfasst meist 1 bis 4 Sätze; längere Erklärungen in mehrere Wortwechsel aufteilen.
- Natürliche Gesprächsdynamik: Rückfragen, kurze Reaktionen („Okay, verstehe.“, „Moment mal …“), gelegentliches Unterbrechen. Füllwörter nur sparsam.
- Kein Markdown, keine Aufzählungszeichen, keine Emojis, keine Regieanweisungen im Text.
- Zahlen, Einheiten und Abkürzungen so ausschreiben, wie man sie sprechen würde (siehe Skill `tts-friendly-text`).
- **Keine Formeln vorlesen.** Es ist ein Podcast: Erkläre stattdessen in Worten, was eine Formel aussagt und warum das wichtig ist („Der Aufwand wächst mit dem Quadrat der Textlänge: doppelt so lang heißt viermal so viel Rechenarbeit.“). Höchstens ganz kurze, allgemein bekannte Beziehungen dürfen beim Namen genannt werden (z. B. „E gleich m c Quadrat“).
{%- if handout %}
- Es gibt ein Handout zur Episode. Wenn eine Formel, Tabelle oder Abbildung wirklich wichtig ist, verweise darauf („Die genaue Formel findet ihr im Handout.“) und trage sie in `handout_items` ein, damit sie dort erscheint.
{%- else %}
- Es gibt kein Handout. Verweise nicht auf Formeln, Tabellen oder Abbildungen zum Nachlesen; erkläre die Idee im Gespräch oder lass das Detail weg.
{%- endif %}
- Keine URLs, keine Literaturangaben in Klammern.
- Beide Personen sprechen etwa gleich oft; {{ expert_name }} hat mehr Redeanteil bei Erklärungen.
- Nicht jede Antwort mit Lob für die Frage beginnen.
