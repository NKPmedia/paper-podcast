# Paper Podcast

A self-hosted server that turns a topic or paper description into a German
two-person podcast: Claude Code researches the topic on the web, writes a dialogue
between a curious host and an expert, and a free online TTS speaks it.

See [PLAN.md](PLAN.md) for the full design. **Current state: milestone 1, the core
pipeline as a CLI.** The web UI, Telegram bot, handout and connectors come next.

## Quick start (Docker)

```bash
cp .env.example .env
# On your own machine: `claude setup-token`, then put the token into .env
#   CLAUDE_CODE_OAUTH_TOKEN=...
mkdir -p data && sudo chown 1000:1000 data   # the container runs as uid 1000

docker compose build
docker compose run --rm app new "Das Paper 'Attention Is All You Need'" --length kurz --depth quick
```

The finished episode lands in `data/episodes/<datum>-<thema>/episode.mp3`.

## CLI

```bash
# New episode
docker compose run --rm app new "Neue Festkörperbatterien" \
    --length mittel          # kurz (~5 min) | mittel (~12 min) | lang (~25 min)
    --depth medium           # quick | medium | deep research
    --extra "Fokus auf Anwendungen in E-Autos"
    --block "style=Etwas mehr Humor."   # append to a prompt block for this episode
    --skill mein-skill       # enable an extra skill

# Resume after an error, or re-run from a stage (research | script | tts | audio)
docker compose run --rm app resume /data/episodes/<id> --from-stage script

docker compose run --rm app prompts   # list prompt blocks (* = customized)
docker compose run --rm app skills    # list skills
```

## Pipeline

| Stage | What happens | Output |
|---|---|---|
| research | 1. Scouts (Haiku, in parallel by angle) search and rank candidates. 2. The main model (Opus) selects papers. 3. Our code downloads the full texts (arXiv HTML → PDF → open-access PDF). 4. Opus reads every paper and writes notes | `scouts/`, `candidates.json`, `selection.json`, `papers/`, `research.md`, `sources.json` |
| script | Claude writes the dialogue from the notes and looks up details in `papers/` when needed; the result is checked (length, speakers, no formulas) and retried with feedback | `script.json` |
| tts | Edge TTS, one clip per line, with retries; existing clips are reused on resume | `clips/` |
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

**Audio assets.** Optional `data/assets/intro.mp3`, `outro.mp3` and `cover.jpg`.

## Security notes

- Claude gets no `Bash` tool. Research uses only WebSearch, WebFetch and Read;
  scripting uses only Read.
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
