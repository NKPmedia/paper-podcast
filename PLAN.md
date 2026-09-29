# Paper Podcast Server — Plan

A small self-hosted Docker service. You describe a topic (which may name specific
papers); Claude Code researches it and writes a German two-person podcast script
(expert + curious host); a free online TTS turns it into a polished MP3. An
optional PDF handout with Python-generated plots can be produced alongside.
You control it from a password-protected website and a Telegram bot.

Status: **all milestones implemented** (email and REST API dropped on request) — see README.md. Next: periodic digest episodes (design in section 7).

---

## 1. Decisions so far

| Topic | Decision |
|---|---|
| Claude auth | Subscription token (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`), personal use |
| Claude runtime | Claude Agent SDK for Python (bundles the Claude Code CLI) |
| Language | German |
| Input | Free-text topic description (may mention papers, authors, arXiv IDs, URLs) |
| Research | Always runs first; depth `quick` / `medium` (default) / `deep` per request. Parallel Haiku scouts return a ranked shortlist, Opus picks papers, our code downloads the full texts, and Opus reads all of them |
| Length | Per request: `kurz` (~5 min), `mittel` (~12 min), `lang` (~25 min) |
| Review | Fully automatic — no manual script approval step |
| TTS | Free + online: **Edge TTS** by default; **Gemini TTS** (free tier) if a key is set, falling back to Edge when the quota runs out |
| Handout | Optional PDF (Markdown + matplotlib plots → PDF) |
| Prompt editing | Named prompt blocks, editable in the web UI, reset-to-default, per-request override |
| Skills | Claude Code Agent Skills (`SKILL.md` folders) that Claude loads on demand; bundled defaults plus your own, managed in the web UI |
| Users | Single user: one web password, Telegram whitelist of chat IDs |
| Hosting | One exposed port behind your existing reverse proxy; 4 GB RAM / 2 vCPU |
| Connectors | Web UI, Telegram, private podcast RSS feed (email and REST API dropped on request) |
| Later | Periodic "new relevant papers" digest episodes (designed for, not built) |

## 2. Lessons from existing projects

| Project | What we take from it |
|---|---|
| Podcastfy | Declarative, user-editable conversation config (roles, style, structure); Edge TTS as free engine |
| Open Notebook / podcast-creator | Keep every stage's artifact on disk (outline, transcript, clips); speaker profiles; retries |
| NVIDIA pdf-to-podcast | Staged generation: outline → segments → dialogue → revision; job/status API |
| Together AI / Open NotebookLM | Schema-validated JSON script with a planning "scratchpad" first |
| Meta NotebookLlama | Separate "write for the ear" pass adapted to the TTS engine |
| Azzedde paper_to_podcast | Plan per section to reduce hallucination; enhancement pass removes repetition |
| zarazhangrui personalized-podcast | Claude Code itself as the writer, prompt kept in an editable Markdown file |

What almost none of them do, and we will: **a web research step before writing**,
**a sources list**, **loudness-normalized audio with chapters**, **a handout with
plots**, and **chat and feed delivery**.

## 3. Architecture

One application container plus a data volume. Kept deliberately small: no Redis,
no Postgres, no separate worker service.

```
                ┌──────────────────── app container ─────────────────────┐
 Browser ──────▶│ FastAPI (web UI + RSS feed)                            │
 Telegram ◀────▶│ Telegram bot (long polling, no inbound port needed)    │
                │ Scheduler (APScheduler; cleanup now, digests later)    │
                │        │ all create Jobs                               │
                │        ▼                                               │
                │ Job queue (SQLite) ──▶ Worker (1 job at a time)        │
                │                          │                             │
                │   Pipeline: research → script → handout → tts → audio  │
                │        │ Claude Agent SDK      │ edge-tts / Gemini     │
                │        ▼                       ▼ ffmpeg                │
                │ Notifiers: Telegram · Email · Webhook                  │
                └────────────────────────────┬───────────────────────────┘
                                             ▼
                         /data  (SQLite, episodes/, prompts/, settings)
```

Everything runs in **one Python process** (asyncio): FastAPI via uvicorn, the
Telegram bot, the scheduler and a single worker task. The queue lives in SQLite,
so jobs survive restarts; a job that was interrupted resumes from its last
completed stage.

### Key abstractions (these make later features plug in cleanly)

- **Job**: `{id, origin (web|telegram|api|schedule), topic, options, status, stage, progress, error, created_at, finished_at}`.
  Every interface only creates jobs and reads their state.
- **Pipeline stage**: `run(job_dir, options) → artifacts`. Stages are idempotent
  and skipped if their artifact already exists (makes resume and re-runs cheap).
- **TTSProvider**: `synthesize(lines, voices) → clips` (Edge, Gemini; later others).
- **Notifier**: `episode_ready(episode)` / `job_failed(job)` (Telegram, Email, Webhook).
- **Source** (for later): `fetch_new(since, query) → [PaperRef]` (arXiv, Semantic Scholar, RSS).

## 4. Generation pipeline

Every job has its own directory `data/episodes/<id>/`:

```
.claude/skills/   snapshot of the skills enabled for this job
request.json      topic + options + resolved prompt blocks
research.md       research notes (Claude)
sources.json      [{title, authors, year, url, why_relevant}]
script.json       validated dialogue script
handout.md        handout text (optional)
plots.py          plotting code (optional)
figures/*.png     generated plots
handout.pdf       rendered handout
clips/*.mp3       per-line or per-chunk TTS output
episode.mp3       final audio (ID3 tags + chapters + cover)
log.jsonl         stage timings, Claude cost/turns, errors
```

### Stage 1 — Research: scouts → selection → full-text reading

Modelled on Anthropic's multi-agent research system (strong lead model, cheaper
parallel workers, start wide then narrow) and PaperQA2 (cheap models search, the
strong model answers). Our Python code orchestrates the steps, not an LLM, so they
are deterministic, testable and resumable.

**1a. Scouts** (default model **Haiku**; selectable)
- Each scout is a separate Agent SDK call. They run in parallel, using `WebSearch`,
  `WebFetch` and the `paper-research` skill (arXiv, Semantic Scholar, OpenAlex).
- The number of scouts scales with research depth:

  | Depth | Scouts | Angles |
  |---|---|---|
  | `quick` | 1 | broad |
  | `medium` | 3 | background and prior work · method and results · critique, follow-ups and applications |
  | `deep` | 5 | the above plus citation traversal (who cites the core paper, what it builds on) and recent developments |

- Each scout searches broadly first and then narrows down.
- It **ranks rather than filters**. It returns 10–20 candidates as structured
  output: arXiv ID or DOI, title, authors, year, URL, a relevance score from 0 to
  10, a one-line reason and a verbatim quote from the abstract.
- Scouts take metadata (authors, year) from the APIs, never from memory.
- Our code merges the candidates, deduplicates them by arXiv ID, DOI or normalized
  title, and writes `candidates.json`, a ranked shortlist of about 20–40 entries
  that you can inspect.

**1b. Selection** (main model, default **Opus**)
- The main agent sees the shortlist and picks the papers to read (`quick` about 3,
  `medium` about 6, `deep` about 10). It may pick from anywhere in the list, not
  just the top.
- Output: the selected IDs, each with a reason.

**1c. Full-text download** (our code, no LLM)
- For each selected paper, fetch the arXiv HTML version (`arxiv.org/html/<id>`,
  which keeps structure and maths) and convert it to Markdown.
- Fallback: the PDF, converted to text with PyMuPDF. Other URLs: the open-access
  PDF from Semantic Scholar or OpenAlex.
- Saved as `papers/<id>.md` along with `papers/index.json`; failures are recorded
  there.

**1d. Deep reading and notes** (main model; a fresh call that is given the selection and its reasons, which keeps it resumable)
- The main agent `Read`s **every** downloaded paper in full from the job
  directory. These are local files, so there is no WebFetch summarization and the
  text arrives unaltered.
- It may also use `WebSearch` and `WebFetch` for anything still missing.
- Claude Code compacts the context on its own if it grows large. Very long papers
  are read in sections.
- Output: `research.md` plus `sources.json` (structured output).
  - Every claim in the notes carries a pointer into the full text, e.g.
    `[Gu 2023, papers/arxiv_2312.00752.md:210-245]`.
  - A required section **Material für den Podcast** holds examples, analogies,
    surprising results, anecdotes and quotes.

**Handover to the script step.**
- Research and script stay separate calls: this keeps the script context small,
  makes retries cheap, and lets you regenerate the script without re-reading.
- The notes are not the only bridge. The script agent has `Read` access to
  `papers/` and follows the pointers when a chapter needs depth.
- The `fact-check` skill verifies central numbers against the cited paper passages.
  If a note and the paper disagree, the paper wins.

**Configuration** (settings, later also editable in the web UI):
- `RESEARCH_SCOUT_MODEL=haiku`, `RESEARCH_MAIN_MODEL=opus`, `SCRIPT_MODEL=opus`.
- Scout count, candidates per scout and papers to read, set per depth.
- `max_turns` per role.

**Logging:** `log.jsonl` records cost, turns and model for each call and role.

**Guardrails:**
- No `Bash`. The web is reached only through `WebSearch` and `WebFetch`; papers
  are downloaded by our code.
- Downloads are limited in size (for example 20 MB per PDF) and allowed only over
  `https`.
- Scouts that fail are logged, and research continues as long as at least one
  scout succeeds.

### Stage 2 — Script (Claude Agent SDK)
- Input: `research.md` and the prompt blocks `personas`, `style`, `structure`,
  `script_rules`, `length`.
- Two steps inside one session:
  1. Outline with chapters.
  2. Full dialogue, followed by a self-review pass (accuracy against the research,
     no repetition, natural German spoken language).
- Output via JSON schema:
  ```json
  {"title": "...", "summary": "...",
   "chapters": [{"title": "...", "lines": [
       {"speaker": "host|expert", "text": "...", "style": "neugierig"}]}]}
  ```
- Server-side validation checks the speaker values, the word count against the target
  length (~130 words/min), and a maximum line length. Retry once with feedback on failure.
- Rules for writing for the ear: spell out formulas, units and abbreviations; no
  Markdown; short sentences; the host asks the questions a listener would ask.

### Stage 3 — Handout (optional)
- Claude writes `handout.md` (key ideas, glossary, sources) and `plots.py`
  (matplotlib, data taken from the research, clearly marked as illustrative when
  approximate).
- **Our code**, not Claude, runs `plots.py` in a subprocess with a timeout, a clean
  environment (no secrets) and a write-only `figures/` directory. On error, Claude
  gets one retry with the traceback.
- Markdown → HTML → PDF via WeasyPrint with a simple print stylesheet.

### Stage 4 — TTS
- **Edge TTS** (default, free, no key): one clip per line.
  - Default voices: host `de-DE-SeraphinaMultilingualNeural`, expert
    `de-DE-FlorianMultilingualNeural`; alternatives Katja/Conrad.
  - `style` maps to small rate/pitch tweaks. Limited concurrency (3), retries with
    backoff.
- **Gemini TTS** (if `GEMINI_API_KEY` is set): native two-speaker synthesis in chunks
  of about 1 chapter, with per-speaker voice and a style prompt. If quota or rate
  limit errors occur, the job falls back to Edge for the remaining chunks. Mixing
  engines within one episode should be avoided, so either restart the whole episode
  in Edge or make this configurable.

### Stage 5 — Audio post-processing (ffmpeg)
- Concatenate clips with short natural pauses: longer at speaker changes and between
  chapters.
- Optional intro/outro jingle from `data/assets/`.
- Two-pass `loudnorm` to −16 LUFS; mono MP3, 64–96 kbps. A 25-minute episode is about
  15 MB, well under Telegram's 50 MB bot upload limit.
- ID3 tags (title, date, description with sources), chapter markers, cover image.

## 5. Customizable prompts

- Defaults ship in the image: `app/prompts/defaults/*.md` (Jinja2).
- Overrides live in `data/prompts/*.md`. An override replaces its default block.
- Blocks:

  | Block | Content |
  |---|---|
  | `personas` | Names, backgrounds, speaking style of host & expert (default: Lena, curious science journalist; Dr. Jonas, expert) |
  | `style` | Tone, humor level, formality (du/Sie), audience level |
  | `structure` | Intro/hook, chapters, recap, outro/sign-off, podcast name |
  | `research` | How to research, which sources to prefer, depth rules |
  | `script_rules` | Write for the ear, TTS-specific constraints, accuracy rules |
  | `handout` | Handout structure and plot guidance |
  | `system` | Global rules (German output, cite sources, no invented numbers) |

- Web UI: a list of blocks, textarea editor, preview of the fully assembled prompt,
  "reset to default", and a diff against the default.
- Per request: an "advanced" section on the website, or `/new` in Telegram with
  something like `stil: locker`, which is appended to the relevant block for that job.
- `request.json` stores the resolved prompt so every episode is reproducible.

## 5a. Skills for Claude

Prompt blocks and skills do different jobs:
- **Prompt blocks** say *what* to produce and in which style. They are always
  included in the prompt.
- **Skills** are reusable *how-to* knowledge: instructions, reference files and
  optionally scripts. Claude loads a skill only when it is relevant, so a skill can
  hold far more detail than a prompt without costing context on every call.

**How they are wired in:**
- Skills use the standard Claude Code format: `<name>/SKILL.md` with front-matter
  (`name`, `description`), plus optional `reference/*.md` and `scripts/*`.
- Bundled defaults ship in `app/skills/`. Your own skills, or overrides of the
  bundled ones, live in `data/skills/`, which takes precedence.
- At job start, the enabled skills are copied into `<job_dir>/.claude/skills/`. This
  keeps a snapshot, so each episode is reproducible.
- The Agent SDK runs with `setting_sources=["project"]` and the `Skill` tool allowed,
  so Claude discovers the skills automatically.
- Each pipeline stage has a list of which skills it enables. A skill can also be
  switched on for a single request.

**Bundled skills (first version):**

| Skill | Used in | Content |
|---|---|---|
| `paper-research` | research | Using the arXiv API, Semantic Scholar API and OpenAlex through WebFetch (endpoints and query syntax in `reference/`); finding the original paper, citations and follow-ups; judging source quality |
| `german-podcast-dialogue` | script | Spoken German style: how to phrase questions, fillers used sparingly, analogies, how to handle du/Sie, bad and good examples |
| `tts-friendly-text` | script | Rules and lexicon (`reference/aussprache.md`) for numbers, units, formulas, English terms and abbreviations as they should be spoken by Edge or Gemini |
| `fact-check` | script (review) | Checklist to verify each claim and number in the script against `research.md` / `sources.json` |
| `handout-plots` | handout | matplotlib style guide (fonts, colors, sizes for A4), plot types per content (timeline, comparison, bar, schematic), labelling when data is approximate |
| `digest-ranking` | *(later)* digest | How to score new papers against your interests |

**Managing skills:**
- The web UI has a **Skills** page: list skills, enable or disable them per stage,
  edit `SKILL.md` and its reference files, create a new skill, reset a bundled skill,
  and upload a skill as a zip (for example one from `anthropics/skills`).
- Telegram: `/skills` shows which skills are active.
- Episode logs record which skills Claude actually loaded, so you can see whether a
  skill is used.

**Scripts in skills and safety:**
- By default Claude has no `Bash`, so scripts inside skills are not executed; only
  their instructions and reference files are used.
- A skill can be marked `allow_scripts: true` in the UI. For that stage Claude then
  gets `Bash` restricted to `python .claude/skills/<name>/scripts/*`, running with the
  clean environment (no secrets), the job directory as its working folder and a
  timeout.
- Plot rendering stays with our own runner, as described in Stage 3.

## 6. Interfaces

### Website (FastAPI + Jinja2 + htmx, no JS build step)
- Login with a single password (bcrypt hash from env), signed session cookie, login
  rate limit.
- Pages:
  - **New episode**: topic, length, research depth, handout on/off, optional extra
    instructions.
  - **Episodes**: list, live status of the running job, audio player, script, sources,
    handout download, delete/regenerate.
  - **Prompts**: block editor.
  - **Settings**: voices, TTS engine, notification targets, RSS link.

### Telegram bot (python-telegram-bot, long polling)
- Only whitelisted chat IDs (`TELEGRAM_ALLOWED_CHAT_IDS`); everyone else is ignored.
- Commands:
  - Plain text or `/new <Thema>`: asks for length, depth and handout via inline
    buttons (with defaults), then queues the job.
  - `/current`: shows the status of the running job; otherwise sends the newest
    episode (audio + handout PDF).
  - `/list` shows the last 10 episodes; `/get <n>` sends one of them.
  - `/status` shows the queue; `/cancel` cancels the running job.
- Progress messages are edited in place as stages advance.

### Other connectors
- **Podcast RSS**: `/feed/<secret-token>.xml` with iTunes tags, subscribable in
  AntennaPod, Pocket Casts and similar apps. Audio URLs carry the same token.
- Email notifications and a REST API with webhooks were dropped on request.

## 7. Future: periodic digest episodes (design only)

What already exists or will be prepared:
- Job `origin=schedule`.
- The in-process scheduler.
- The `Source` interface.
- The notifier fan-out.
- The RSS feed.

Later additions:
- **Table `subscriptions`**: `{name, interests (free text), sources (arXiv categories,
  keywords), cron, length, enabled}`.
- **Table `seen_papers`**: `{source, paper_id, first_seen, used_in_episode}` for
  deduplication.
- **Digest flow**:
  1. Fetch candidates from the sources since the last run.
  2. Claude ranks them against `interests`.
  3. The top N go into the normal pipeline with a `digest` structure block ("3 papers
     this week").
  4. Store the used IDs.
- Web UI: a subscriptions page. Telegram: `/abos`.

No change to the pipeline is needed. It is just a new job producer and a prompt block.

## 8. Docker setup

```
paper-podcast/
├── Dockerfile            python:3.12-slim + ffmpeg + WeasyPrint libs + fonts
├── docker-compose.yml    one service `app`, volume ./data:/data, port 8000
├── .env.example
├── app/
│   ├── main.py           starts web, bot, scheduler, worker
│   ├── config.py         pydantic-settings from env
│   ├── db.py             SQLite (SQLModel)
│   ├── jobs.py           queue + worker + resume
│   ├── pipeline/         research.py script.py handout.py tts/ audio.py
│   ├── claude.py         Agent SDK wrapper (tools, schema, cost logging)
│   ├── prompts/          defaults/*.md + loader
│   ├── skills/           bundled Agent Skills (<name>/SKILL.md, reference/, scripts/)
│   ├── web/              routes, templates, static
│   ├── telegram_bot.py
│   ├── telegram_bot.py   (bot + worker listener)
│   └── feed.py
└── tests/
```

Environment variables:
- **Claude**: `CLAUDE_CODE_OAUTH_TOKEN`.
- **Web**: `WEB_PASSWORD_HASH`, `SESSION_SECRET`, `PUBLIC_BASE_URL`, `FEED_TOKEN`.
- **Telegram**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`.
- **Optional**: `GEMINI_API_KEY`.

Runtime details:
- The container runs as a non-root user.
- The Claude subprocess gets a minimal environment containing only its token, never
  the Telegram, SMTP or other secrets.
- Resources: about 1 GB RAM for the Claude session, and ffmpeg is light, so this fits
  a 4 GB VPS with headroom.
- Cleanup: old clips are deleted after success; there is an optional retention policy.

## 9. Milestones

1. **Core pipeline (CLI)**: config, prompt blocks, skill loading with the bundled
   skills, research → script → Edge TTS → ffmpeg, runnable as
   `python -m app.cli "Thema"`. Includes Docker image and compose file.
2. **Job queue + web UI**: login, new episode, episode list and player, prompt
   editor, skills page.
3. **Telegram bot**: `/new`, `/current`, `/list`, `/get`, `/status`, `/cancel`, and
   progress updates.
4. **Handout**: Markdown + plots → PDF, delivered via web and Telegram.
5. **Connectors**: RSS feed.
6. **Polish**: Gemini TTS provider with fallback, jingle, chapters, cover, retries,
   tests.
7. *(Later)* Periodic digest episodes.

## 10. Risks & open points

- **Edge TTS is unofficial**: it could break or be rate-limited. It sits behind the
  TTS interface, so it can be swapped for Gemini, OpenAI or local engines.
- **Gemini free tier**: the daily limits are small and change over time, so treat it
  as best-effort.
- **Subscription quota**: deep research plus long scripts use a noticeable part of
  the plan's usage limits. Cost and turns are logged per job, and `max_turns` is
  capped per stage.
- **Subscription token terms**: fine for a personal, single-user server. If the
  server is ever opened to others, switch to `ANTHROPIC_API_KEY`.
- **Accuracy**: the prompts require every number to come from `research.md`, and the
  sources are listed in the episode notes and handout. Hallucination is reduced, not
  eliminated.
- **Topic-only input**: PDF upload or direct links could be added later as another
  input type without touching the pipeline, since research just gets extra material.
