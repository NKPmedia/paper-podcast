# Paper Podcast

A self-hosted server that turns a topic or paper description into a German
two-person podcast: Claude Code researches the topic on the web, writes a dialogue
between a curious host and an expert, and a free online TTS speaks it.

See [PLAN.md](PLAN.md) for the full design. The core pipeline, the handout, the
password-protected web UI, the Telegram bot and the podcast feed are implemented.

## Prebuilt image (GitHub Actions)

On every push, GitHub Actions runs the tests and, if they pass, builds the Docker image
and pushes it to the GitHub Container Registry
(`.github/workflows/docker.yml`):

| Tag | When |
|---|---|
| `ghcr.io/nkpmedia/paper-podcast:latest` | Default branch |
| `:<branch>` | Every other branch |
| `:sha-<commit>` | Every build |
| `:1.2.3` / `:1.2` | Git tags like `v1.2.3` |

Pull requests are built but not pushed.

To run the prebuilt image, use [`docker-compose.example.yml`](docker-compose.example.yml):
copy it to `docker-compose.yml`, fill in `.env`, then run `docker compose up -d`.
Update with `docker compose pull && docker compose up -d`.

If the package is private, log in on the server once with a token that has
`read:packages`. Alternatively, make the package public under GitHub → Packages →
paper-podcast → Package settings.

## Quick start (Docker, build locally)

```bash
mkdir -p data && sudo chown 1000:1000 data   # the container runs as uid 1000
touch .env                                    # optional: settings from .env.example
docker compose build
docker compose up -d
docker compose logs app | grep -A1 Ersteinrichtung   # shows the one-time setup code
```

Open the web UI. On the first visit a short **setup dialog** asks for:

| Field | |
|---|---|
| Setup code | Printed in the server log, so nobody else who reaches your fresh server can claim it |
| Password | For the web UI |
| Claude token | Run `claude setup-token` once on your own machine |
| Podcast name, host and expert names | |
| Public address (optional) | For links |
| Telegram bot token and chat IDs (optional) | |
| Gemini API key (optional) | |
| Allow plain-HTTP login | Only offered over HTTP; for testing without a proxy |

**Where settings are stored:**
- Everything entered in the dialog is saved in `data/settings.json` (readable only by
  the owner).
- It can be changed later under **Einstellungen**.
- Values set in `.env` take precedence; the UI shows them as locked.
- A Telegram token change restarts the bot immediately.

**Networking:**
- The web UI listens on `127.0.0.1:8000`. Point your reverse proxy (HTTPS) at it.
- If the proxy runs on another machine or in another Docker network, change the
  `ports:` entry.

**Where episodes go:** `data/episodes/<datum>-<thema>/`, with the finished audio as
`episode.mp3`.

## Web UI

| Page | What you can do |
|---|---|
| Episodes | Start an episode: topic, length, research depth, extra wishes, and optional additions to prompt blocks or extra skills for this episode only. See the list with live status. |
| Episode | See live progress per stage. Play the episode with a chapter list and download the MP3. Read the script, research notes and sources. See the research details: candidates, selection, which full texts were downloaded, and every Claude call with model, turns and cost. Cancel, resume after an error, re-generate from a stage, or delete. |
| Prompts | Edit the prompt blocks with preview and syntax check, and reset them to the default. |
| Skills | Choose which skills each stage uses. View the bundled skills; editing one creates your own copy, which you can reset later. Create new skills or import a `.zip`. |

**Job handling:**
- One job runs at a time; further jobs wait in a queue.
- After a restart, an interrupted job continues from its last finished step.

**Security:**
- One password, stored as a scrypt hash.
- Login is throttled after 5 failed attempts.
- All forms carry CSRF tokens.
- Session cookies are `SameSite=Lax`.
- Research notes are rendered without raw HTML.
- Telegram: only chats listed in `TELEGRAM_ALLOWED_CHAT_IDS` are served. The bot
  token is removed from Claude's environment like every other secret.

## Telegram bot

**Setup:**
1. Create a bot with [@BotFather](https://t.me/BotFather) and put its token into
   `.env` as `TELEGRAM_BOT_TOKEN`.
2. Leave `TELEGRAM_ALLOWED_CHAT_IDS` empty and restart the container.
3. Write anything to your bot. It replies with your chat ID.
4. Put the ID into `TELEGRAM_ALLOWED_CHAT_IDS` and restart again. From then on the
   bot ignores every other chat.

The bot uses long polling, so it needs no inbound port or webhook.

| Command | What it does |
|---|---|
| any text, or `/neu <Thema>` | Starts a new episode. Buttons for length, research depth and handout, then **▶ Starten**. |
| `/aktuell` | Live status of running or waiting jobs. While something is running, you get a **▶ Senden** button for the newest episode instead of the whole episode again. |
| `/liste` | The last 10 episodes; tap a number to receive one. Episodes that did not finish come with a **🔁** resume button. (`/folge <Nr>` still works.) |
| `/status` | The queue. |
| `/abbrechen` | Cancel the running job, after a confirmation. |

**While an episode runs:**
- The bot edits one status message as the stages advance, e.g.
  ✓ Recherche · ▶ **Skript** · Sprache · Audio.
- The message has an **✖ Abbrechen** button, which asks for confirmation first.
- When the episode is done, the MP3 arrives as an audio message with title and
  summary, followed by chapters and sources.
- If a job fails, the message names the step that failed and shows a **🔁 Fortsetzen** button, which resumes at that step.

**Episodes started from the web UI** are announced and delivered the same way
unless `TELEGRAM_NOTIFY_ALL=false`.

**Size limit:** Telegram bots can upload at most 50 MB, which is well above a
25-minute episode. For larger files, point `TELEGRAM_API_BASE_URL` at a
self-hosted Bot API server.

## Podcast feed

The **Verbindungen** page shows your private feed URL, with a copy button.
- Subscribe to it in AntennaPod, Pocket Casts or Apple Podcasts ("follow a show
  by URL"), and new episodes appear automatically.
- Each episode includes its chapters (Podcasting 2.0), show notes with sources,
  and a handout link.
- The URL contains a secret token. For a new one, delete `data/feed_token` (or set
  `FEED_TOKEN`) and restart.

## CLI

Inside the running container:

```bash
# Queue an episode for the server (it shows up in the web UI)
docker compose exec app python -m app.cli enqueue "Neue Festkörperbatterien" --length kurz

# Or run one directly in the foreground, without the queue
docker compose exec app python -m app.cli new "Neue Festkörperbatterien" \
    --length mittel          # kurz (~5 min) | mittel (~12 min) | lang (~25 min)
    --depth medium           # quick | medium | deep research
    --extra "Fokus auf Anwendungen in E-Autos"
    --block "style=Etwas mehr Humor."   # append to a prompt block for this episode
    --skill mein-skill       # enable an extra skill

# Resume, or re-run from a stage (research | script | tts | audio)
docker compose exec app python -m app.cli resume /data/episodes/<id> --from-stage script

docker compose exec app python -m app.cli prompts   # list prompt blocks (* = customized)
docker compose exec app python -m app.cli skills    # list skills
```

## Pipeline

| Stage | What happens | Output |
|---|---|---|
| research | 1. Scouts (Haiku, in parallel by angle) search and rank candidates. 2. The main model (Opus) selects papers. 3. Our code downloads the full texts (arXiv HTML → PDF → open-access PDF). 4. Opus reads every paper and writes notes | `scouts/`, `candidates.json`, `selection.json`, `papers/`, `research.md`, `sources.json` |
| script | Claude writes the dialogue from the notes and looks up details in `papers/` when needed; the result is checked (length, speakers, no formulas) and retried with feedback | `script.json` |
| handout (optional) | Claude writes the handout body in **LaTeX**, with properly typeset formulas, tables and a glossary, plus matplotlib code for 1–3 figures. Our code renders the figures as vector PDFs in a sandbox and compiles the document with **pdflatex** under a fixed preamble. If compilation fails, Claude gets the error log and up to two repair rounds. Everything the script points to ("steht im Handout") is included | `handout/` (incl. `handout.tex`), `handout.pdf` |
| tts | **Gemini** multi-speaker TTS (one request per chapter, both voices in one natural take) when `GEMINI_API_KEY` is set. When its free quota runs out, it switches to **Edge TTS** for the whole episode, so voices are never mixed. Without a key it uses Edge TTS: one clip per line, free, no key. Finished clips are reused on resume. | `clips/` |
| audio | Clips joined with pauses, optional intro/outro, loudness normalized to -16 LUFS, MP3 with ID3 tags and chapters | `episode.mp3`, `episode.json` |

Research depth sets the number of scouts and papers:

| Depth | Scouts | Angles | Papers read in full |
|---|---|---|---|
| `quick` | 1 | overview | 3 |
| `medium` | 3 | background, core results, critique | 6 |
| `deep` | 5 | the above plus citation network and recent work | 10 |

If a download fails, the main model falls back to `WebFetch` for that paper.

`research.md` is the handover to the script step:
- Every claim is cited with a pointer into the full text, e.g.
  `[Gu 2023, papers/arxiv_2312.00752.md:210-245]`.
- A section **Material für den Podcast** collects examples, analogies, surprising
  findings and quotes.
- The script agent reads the notes and opens the cited passages when it needs
  depth or checks facts.

Every stage is skipped if its output already exists, and so is every research step. `log.jsonl` records timings,
Claude cost and turns, and which skills Claude actually used.

## Customizing

**Prompt blocks.** Copy a file from `app/prompts/defaults/` to `data/prompts/`
and edit it. Blocks are Jinja2 templates with `{{ host_name }}`, `{{ expert_name }}`,
`{{ podcast_name }}`, `{{ minutes }}`, `{{ target_words }}` and
`{{ research_depth }}` available.

| Block | Controls |
|---|---|
| `system` | Global rules: German, no invented numbers, citing sources |
| `personas` | Host and expert |
| `style` | Tone, audience, humor |
| `structure` | Episode outline and target length |
| `research` | How to research, and depth |
| `script_rules` | Writing for the ear |
| `handout` | Handout (milestone 4) |

**Skills.** Claude Code Agent Skills in `app/skills/`:

| Skill | Content |
|---|---|
| `paper-research` | arXiv, Semantic Scholar and OpenAlex APIs |
| `german-podcast-dialogue` | Natural spoken German dialogue |
| `tts-friendly-text` | Writing text that TTS pronounces correctly |
| `fact-check` | Checking the script against the research |
| `handout-plots` | Plot style guide for the handout |

- **Add your own:** put a folder with a `SKILL.md` into `data/skills/<name>/`. The
  same name as a bundled skill overrides it.
- **Choose skills per stage:** use `data/skills.json`:

  ```json
  {"stages": {"research": ["paper-research", "mein-skill"]}}
  ```

**Intro, outro and cover.** Upload these under **Verbindungen → Klang & Cover**:
- Audio is checked and converted; jingles can be at most 60 s.
- The cover is cropped to a square 1400 × 1400 JPEG.
- They are used for new episodes, the MP3 tags and the feed.

## Security notes

- Claude gets no `Bash` tool. Research uses only WebSearch, WebFetch and Read;
  scripting uses only Read.
- Handout plot code comes from Claude and is treated as untrusted:
  - An allow-list check before it runs: only `matplotlib`, `numpy` and `math`; no file,
    network or introspection functions.
  - A separate isolated Python process with an empty environment and CPU, memory
    and file-size limits.
- The LaTeX body from Claude is untrusted too:
  - It is checked before compiling: no preamble, packages, macro definitions,
    file access (`\input`, `\write` …), `\csname` or `^^` escapes.
  - `pdflatex` runs with shell escape off and kpathsea's paranoid file access
    (no absolute paths, no `..`), in a temporary folder, with an empty environment
    and limits.
- Papers are downloaded by our code, not by Claude:
  - only public `https` addresses, checked again on every redirect;
  - a size limit, 20 MB by default.
- App secrets are blanked out in the environment of the Claude subprocess.
- The container runs as a non-root user.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest        # needs ffmpeg on PATH
```

The tests use a fake Claude and a fake TTS, so they need no network or tokens.
