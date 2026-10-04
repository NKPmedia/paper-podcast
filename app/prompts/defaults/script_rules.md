The script is read aloud by a text-to-speech (TTS) engine. Write for the ear:

- Short to medium-length sentences. A turn usually has 1 to 4 sentences; split longer explanations across several exchanges.
- Natural conversational dynamics: follow-up questions, short reactions ({% if language == "de" %}"Okay, verstehe.", "Moment mal …"{% else %}"Okay, got it.", "Wait a second …"{% endif %}), the occasional interruption. Use filler words sparingly.
- No Markdown, no bullet points, no emojis, no stage directions in the text.
- Write numbers, units and abbreviations the way they are spoken (see skill `tts-friendly-text`).
- **Never read out formulas.** This is a podcast: explain in words what a formula says and why it matters ({% if language == "de" %}"Der Aufwand wächst mit dem Quadrat der Textlänge: doppelt so lang heißt viermal so viel Rechenarbeit."{% else %}"The cost grows with the square of the text length: twice as long means four times the work."{% endif %}). Only very short, widely known relations may be named ({% if language == "de" %}"E gleich m c Quadrat"{% else %}"E equals m c squared"{% endif %}).
{%- if handout %}
- There is a handout for this episode. When a formula, table or figure really matters, point to it ({% if language == "de" %}"Die genaue Formel findet ihr im Handout."{% else %}"You'll find the exact formula in the handout."{% endif %}) and add it to `handout_items` so it appears there.
{%- else %}
- There is no handout. Do not point listeners to formulas, tables or figures to look up; explain the idea in the conversation or leave the detail out.
{%- endif %}
- No URLs, no bracketed citations.
- Both people speak about equally often; {{ expert_name }} has the larger share during explanations.
- Do not start every answer by praising the question.
