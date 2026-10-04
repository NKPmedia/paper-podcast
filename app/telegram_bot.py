"""Telegram bot: request episodes, follow their progress, receive the audio.

The bot logic (``TelegramBot``) talks to Telegram only through the small
``Messenger`` interface, so it can be tested without network access. The
python-telegram-bot adapter at the bottom connects it to the real Bot API
(long polling, so no inbound port is needed).

Only chat IDs in ``TELEGRAM_ALLOWED_CHAT_IDS`` are served. With an empty list the
bot runs in setup mode and answers every chat with its chat ID.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.db import Job
from app.errors import split_error
from app.jobs import JobError, JobService, Worker
from app.models import EpisodeOptions, EpisodeRequest, Language, Length, ResearchDepth
from app.pipeline import clarify
from app.pipeline.research import depth_hint
from app.pipeline import STAGE_ARTIFACTS, stages_for

log = logging.getLogger(__name__)

Button = tuple[str, str]  # (label, callback data)
Keyboard = list[list[Button]]

MAX_AUDIO_BYTES = 49 * 1024 * 1024  # Bot API upload limit is 50 MB
STAGE_LABELS = {"research": "Recherche", "script": "Skript", "handout": "Handout", "tts": "Sprachausgabe",
                "audio": "Audio"}
LENGTH_LABELS = {"kurz": "Kurz (~5 min)", "mittel": "Mittel (~12 min)", "lang": "Lang (~25 min)"}
LENGTH_BUTTONS = {"kurz": "Kurz · 5 min", "mittel": "Mittel · 12 min", "lang": "Lang · 25 min"}
DEPTH_LABELS = {"quick": "Schnell", "medium": "Normal", "deep": "Gründlich"}
DEPTH_HINTS = {d.value: depth_hint(d) + (", dauert länger" if d is ResearchDepth.deep else "") for d in ResearchDepth}
LANGUAGE_LABELS = {"de": "🇩🇪 Deutsch", "en": "🇬🇧 English"}
STATUS_ICONS = {"queued": "⏳", "running": "⚙️", "waiting": "❓", "done": "✅", "failed": "❌", "cancelled": "🚫"}
STATUS_LABELS = {"queued": "Wartet", "running": "Läuft", "waiting": "Rückfragen", "done": "Fertig", "failed": "Fehler",
                 "cancelled": "Abgebrochen"}

HELP = (
    "<b>Paper Podcast</b>\n"
    "Schick mir ein Thema oder eine Paper-Beschreibung als Nachricht, dann frage ich nach Länge "
    "und Recherche-Tiefe.\n\n"
    "/neu &lt;Thema&gt; – neue Episode\n"
    "/aktuell – laufender Job und neueste Episode\n"
    "/liste – die letzten Episoden (zum Antippen)\n"
    "/status – Warteschlange\n"
    "/abbrechen – laufenden Job abbrechen"
)

COMMANDS = [
    ("neu", "Neue Episode zu einem Thema"),
    ("aktuell", "Laufender Job und neueste Episode"),
    ("liste", "Die letzten Episoden"),
    ("folge", "Episode aus der Liste senden"),
    ("status", "Warteschlange anzeigen"),
    ("abbrechen", "Laufenden Job abbrechen"),
    ("hilfe", "Hilfe"),
]
ALIASES = {"start": "hilfe", "help": "hilfe", "new": "neu", "current": "aktuell", "list": "liste",
           "get": "folge", "cancel": "abbrechen"}


class Messenger(Protocol):
    async def send_text(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> int: ...

    async def edit_text(self, chat_id: int, message_id: int, text: str, keyboard: Keyboard | None = None) -> None: ...

    async def send_audio(
        self, chat_id: int, path: Path | None, *, file_id: str | None, title: str, performer: str,
        duration: int, caption: str,
    ) -> str: ...

    async def send_document(self, chat_id: int, path: Path | None, *, file_id: str | None, filename: str,
                            caption: str) -> str: ...

    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...


@dataclass
class Draft:
    topic: str
    length: str = Length.mittel.value
    depth: str = ResearchDepth.medium.value
    handout: bool = False
    language: str = Language.de.value
    clarify: bool = True


@dataclass
class AnswerSession:
    """Clarifying questions of one job, asked one at a time in one message."""

    job_id: str
    questions: list[dict]
    message_id: int = 0
    index: int = 0
    answers: list[str] = field(default_factory=list)


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def fmt_duration(seconds: int | None) -> str:
    return f"{(seconds or 0) // 60}:{(seconds or 0) % 60:02d}"


class TelegramBot:
    def __init__(self, settings: Settings, service: JobService, worker: Worker, messenger: Messenger):
        self.settings = settings
        self.service = service
        self.store = service.store
        self.worker = worker
        self.messenger = messenger
        self.drafts: dict[tuple[int, int], Draft] = {}
        self.sessions: dict[int, AnswerSession] = {}  # chat_id -> questions being answered
        self.max_audio_bytes = MAX_AUDIO_BYTES
        self._last_status_text: dict[tuple[int, int], str] = {}

    # --- access ---------------------------------------------------------------------

    @property
    def allowed(self) -> set[int]:
        return self.settings.telegram_chat_ids

    async def check_access(self, chat_id: int) -> bool:
        if chat_id in self.allowed:
            return True
        if not self.allowed:  # setup mode
            await self.messenger.send_text(
                chat_id,
                f"Dieser Bot ist noch nicht eingerichtet.\nDeine Chat-ID: <code>{chat_id}</code>\n"
                "Trage sie in <code>TELEGRAM_ALLOWED_CHAT_IDS</code> ein und starte den Server neu.",
            )
        else:
            log.warning("Ignoring Telegram chat %s (not in TELEGRAM_ALLOWED_CHAT_IDS)", chat_id)
        return False

    # --- incoming -------------------------------------------------------------------

    async def handle_command(self, chat_id: int, command: str, args: list[str]) -> None:
        if not await self.check_access(chat_id):
            return
        command = ALIASES.get(command.lower(), command.lower())
        handler = {
            "hilfe": lambda: self.messenger.send_text(chat_id, HELP),
            "neu": lambda: self.new_draft(chat_id, " ".join(args)),
            "aktuell": lambda: self.cmd_current(chat_id),
            "liste": lambda: self.cmd_list(chat_id),
            "folge": lambda: self.cmd_get(chat_id, args),
            "status": lambda: self.cmd_status(chat_id),
            "abbrechen": lambda: self.cmd_cancel(chat_id),
        }.get(command)
        if handler is None:
            await self.messenger.send_text(chat_id, "Unbekannter Befehl.\n\n" + HELP)
        else:
            await handler()

    async def handle_text(self, chat_id: int, text: str) -> None:
        if not await self.check_access(chat_id):
            return
        session = self.sessions.get(chat_id)
        if session and not self._still_waiting(session):
            self.sessions.pop(chat_id, None)  # answered elsewhere (another chat, the web UI): normal text again
            session = None
        if session and session.index < len(session.questions):
            if session.questions[session.index].get("allow_free_text", True):
                await self._record_answer(chat_id, session, text.strip())
            else:
                await self.messenger.send_text(chat_id, "Bitte wähle eine der Antworten oben aus.")
            return
        await self.new_draft(chat_id, text)

    async def handle_callback(self, chat_id: int, message_id: int, callback_id: str, data: str) -> None:
        if chat_id not in self.allowed:
            await self.messenger.answer_callback(callback_id)
            return
        kind, _, rest = data.partition(":")
        if kind == "d":
            await self._draft_callback(chat_id, message_id, callback_id, rest)
            return
        if kind == "q":
            await self._question_callback(chat_id, message_id, callback_id, rest)
            return
        job = self.store.get(rest) if rest else None
        try:
            if kind == "a" and job:
                await self.messenger.answer_callback(callback_id)
                await self.start_questions(chat_id, job)
            elif kind == "r" and job:
                self.service.retry(rest)
                await self.messenger.answer_callback(callback_id, "Wird fortgesetzt")
                await self._set_status_message(self.store.get(rest), chat_id, message_id)
            elif kind == "c" and job:  # ask first: a cancelled research run costs time and quota
                text, _ = self.status_text(job)
                await self.messenger.edit_text(chat_id, message_id, text + "\n\n<b>Wirklich abbrechen?</b> "
                                               "Fertige Schritte bleiben erhalten.",
                                               [[("Ja, abbrechen", f"C:{job.id}"), ("Nein, weiterlaufen", f"k:{job.id}")]])
                self._last_status_text.pop((chat_id, message_id), None)
                await self.messenger.answer_callback(callback_id)
            elif kind == "C" and job:
                await self.service.cancel(rest)
                await self.messenger.answer_callback(callback_id, "Wird abgebrochen")
            elif kind == "k" and job:
                self._last_status_text.pop((chat_id, message_id), None)
                await self._edit_status(job, chat_id, message_id)
                await self.messenger.answer_callback(callback_id)
            elif kind == "g" and job:
                await self.messenger.answer_callback(callback_id, "Wird gesendet")
                await self.send_episode(chat_id, job)
            elif kind == "l":
                await self.messenger.answer_callback(callback_id, "Wird gesendet")
                done = self._done_jobs(1)
                if done:
                    await self.send_episode(chat_id, done[0])
            else:
                await self.messenger.answer_callback(callback_id, "Nicht mehr verfügbar")
        except JobError as exc:
            await self.messenger.answer_callback(callback_id, str(exc))

    # --- new episode ------------------------------------------------------------------

    def _draft_view(self, draft: Draft) -> tuple[str, Keyboard]:
        def mark(label: str, on: bool) -> str:
            return f"✓ {label}" if on else label

        text = (
            f"<b>Neue Episode</b>\n{esc(draft.topic)}\n\n"
            f"Länge: {LENGTH_LABELS[draft.length]}\n"
            f"Recherche: {DEPTH_LABELS[draft.depth]} ({DEPTH_HINTS[draft.depth]})\n"
            f"Handout: {'ja (PDF)' if draft.handout else 'nein'}\n"
            f"Sprache: {LANGUAGE_LABELS[draft.language]}\n"
            f"Rückfragen: {'erlaubt, falls das Thema unklar ist' if draft.clarify else 'nein'}"
        )
        keyboard = [
            [(mark(LENGTH_BUTTONS[v], draft.length == v), f"d:len:{v}") for v in LENGTH_LABELS],
            [(mark("🔎 " + DEPTH_LABELS[v], draft.depth == v), f"d:dep:{v}") for v in DEPTH_LABELS],
            [(mark("📄 Handout (PDF)", draft.handout), "d:ho"), (mark("❓ Rückfragen", draft.clarify), "d:cl")],
            []
            + [(mark(LANGUAGE_LABELS[v], draft.language == v), f"d:lang:{v}") for v in LANGUAGE_LABELS],
            [("▶ Starten", "d:go"), ("✖ Verwerfen", "d:x")],
        ]
        return text, keyboard

    async def new_draft(self, chat_id: int, topic: str) -> None:
        topic = topic.strip()
        if not topic:
            await self.messenger.send_text(chat_id, "Worum soll es gehen? Schick mir das Thema, z.B.\n/neu Das Mamba-Paper")
            return
        if len(topic) > 2000:
            await self.messenger.send_text(chat_id, "Das Thema ist zu lang (max. 2000 Zeichen).")
            return
        language = self.settings.default_language if self.settings.default_language in LANGUAGE_LABELS else "de"
        draft = Draft(topic=topic, language=language)
        text, keyboard = self._draft_view(draft)
        message_id = await self.messenger.send_text(chat_id, text, keyboard)
        self.drafts[(chat_id, message_id)] = draft

    async def _draft_callback(self, chat_id: int, message_id: int, callback_id: str, action: str) -> None:
        draft = self.drafts.get((chat_id, message_id))
        if draft is None:
            await self.messenger.answer_callback(callback_id, "Abgelaufen – bitte neu senden")
            return
        field, _, value = action.partition(":")
        if field == "len" and value in LENGTH_LABELS:
            draft.length = value
        elif field == "dep" and value in DEPTH_LABELS:
            draft.depth = value
        elif field == "ho":
            draft.handout = not draft.handout
        elif field == "cl":
            draft.clarify = not draft.clarify
        elif field == "lang" and value in LANGUAGE_LABELS:
            draft.language = value
        elif field == "x":
            del self.drafts[(chat_id, message_id)]
            await self.messenger.edit_text(chat_id, message_id, f"Verworfen: {esc(draft.topic)}")
            await self.messenger.answer_callback(callback_id)
            return
        elif field == "go":
            del self.drafts[(chat_id, message_id)]
            request = EpisodeRequest(
                topic=draft.topic,
                options=EpisodeOptions(length=Length(draft.length), research_depth=ResearchDepth(draft.depth),
                                       handout=draft.handout, language=Language(draft.language),
                                       clarify=draft.clarify),
            )
            job = self.service.submit(request, origin="telegram")
            # Record the status message before the first await, so the worker's
            # 'started' event already knows where to report.
            self._save_meta(job.id, {"messages": [{"chat_id": chat_id, "message_id": message_id}]})
            await self.messenger.answer_callback(callback_id, "Gestartet")
            await self._set_status_message(job, chat_id, message_id)
            return
        text, keyboard = self._draft_view(draft)
        await self.messenger.edit_text(chat_id, message_id, text, keyboard)
        await self.messenger.answer_callback(callback_id)

    # --- job status -------------------------------------------------------------------

    def _meta_path(self, job_id: str) -> Path:
        return self.service.job_dir(job_id) / "telegram.json"

    def _meta(self, job_id: str) -> dict:
        path = self._meta_path(job_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"messages": []}

    def _save_meta(self, job_id: str, meta: dict) -> None:
        self._meta_path(job_id).write_text(json.dumps(meta), encoding="utf-8")

    def _web_link(self, job_id: str) -> str:
        base = self.settings.public_base_url.rstrip("/")
        return f'\n<a href="{esc(base)}/episodes/{esc(job_id)}">Im Browser öffnen</a>' if base else ""

    def status_text(self, job: Job) -> tuple[str, Keyboard | None]:
        lines = [f"{STATUS_ICONS[job.status]} <b>{esc(job.display_title)}</b>"]
        if job.title:
            lines.append(f"<i>{esc(job.topic[:200])}</i>")
        if job.status in ("queued", "running"):
            if job.status == "queued":
                ahead = sum(1 for j in self.store.list(200) if j.status == "queued" and j.created_at < job.created_at)
                running = self.worker.current_job_id()
                position = f" (Position {ahead + 1})" if ahead or running else ""
                lines.append(f"In der Warteschlange{position}")
            else:
                job_dir = self.service.job_dir(job.id)
                steps = []
                for stage in stages_for(self.service.request(job.id).options.handout):
                    if (job_dir / STAGE_ARTIFACTS[stage]).exists() and stage != job.stage:
                        steps.append(f"✓ {STAGE_LABELS[stage]}")
                    elif stage == job.stage:
                        steps.append(f"▶ <b>{STAGE_LABELS[stage]}</b>")
                    else:
                        steps.append(STAGE_LABELS[stage])
                lines.append(" · ".join(steps))
                if job.message:
                    lines.append(f"<i>{esc(job.message)}</i>")
            keyboard = [[("✖ Abbrechen", f"c:{job.id}")]]
        elif job.status == "waiting":
            count = len(clarify.pending_questions(self.service.job_dir(job.id)))
            lines.append(f"Claude hat vor der Recherche {count} Rückfrage(n), damit die Episode in die richtige "
                         "Richtung geht.")
            keyboard = [[("❓ Fragen beantworten", f"a:{job.id}"), ("✖ Abbrechen", f"C:{job.id}")]]
        elif job.status == "failed":
            lines.append(f"Fehler beim Schritt <b>{STAGE_LABELS.get(job.stage, 'Start')}</b>. "
                         "„Fortsetzen“ macht dort weiter, fertige Schritte bleiben erhalten.")
            lines.append(esc(split_error(job.error)[0][:600]))
            keyboard = [[("🔁 Fortsetzen", f"r:{job.id}")]]
        elif job.status == "cancelled":
            lines.append("Abgebrochen.")
            keyboard = [[("🔁 Fortsetzen", f"r:{job.id}")]]
        else:
            lines.append(f"Fertig · {fmt_duration(job.duration_s)} min")
            keyboard = None
        return "\n".join(lines) + self._web_link(job.id), keyboard

    async def _set_status_message(self, job: Job, chat_id: int, message_id: int) -> None:
        """Turn an existing message into the live status message of ``job``."""
        meta = self._meta(job.id)
        if not any(m["chat_id"] == chat_id and m["message_id"] == message_id for m in meta["messages"]):
            meta["messages"].append({"chat_id": chat_id, "message_id": message_id})
            self._save_meta(job.id, meta)
        await self._edit_status(job, chat_id, message_id)

    async def _edit_status(self, job: Job, chat_id: int, message_id: int) -> None:
        text, keyboard = self.status_text(job)
        key = (chat_id, message_id)
        if self._last_status_text.get(key) == text:
            return  # Telegram rejects edits that change nothing
        self._last_status_text[key] = text
        await self.messenger.edit_text(chat_id, message_id, text, keyboard)

    # --- worker events (JobListener) ----------------------------------------------------

    async def job_event(self, job: Job, event: str) -> None:
        if job is None:
            return
        meta = self._meta(job.id)
        if (event == "started" and not meta["messages"] and job.origin != "telegram"
                and self.settings.telegram_notify_all):
            # Started elsewhere (web UI, CLI): announce it in every allowed chat.
            for chat_id in sorted(self.allowed):
                text, keyboard = self.status_text(job)
                message_id = await self.messenger.send_text(chat_id, text, keyboard)
                self._last_status_text[(chat_id, message_id)] = text
                meta["messages"].append({"chat_id": chat_id, "message_id": message_id})
            self._save_meta(job.id, meta)
            return
        for m in meta["messages"]:
            try:
                await self._edit_status(job, m["chat_id"], m["message_id"])
            except Exception:
                log.exception("Could not update Telegram status for %s", job.id)
        if event == "done":
            for chat_id in sorted({m["chat_id"] for m in meta["messages"]}):
                await self.send_episode(chat_id, job)
        elif event == "waiting":
            for chat_id in sorted({m["chat_id"] for m in meta["messages"]}):
                await self.start_questions(chat_id, job)

    # --- clarifying questions ---------------------------------------------------------------

    def _question_view(self, job: Job, session: AnswerSession) -> tuple[str, Keyboard]:
        i, total = session.index, len(session.questions)
        question = session.questions[i]
        text = f"❓ <b>Rückfrage {i + 1}/{total}</b> · {esc(job.display_title)}\n\n{esc(question['question'])}"
        if question.get("allow_free_text", True):
            text += ("\n\n<i>Oder schreib deine eigene Antwort einfach als Nachricht.</i>" if question["options"]
                     else "\n\n<i>Schreib deine Antwort als Nachricht.</i>")
        keyboard = [[(option[:60], f"q:{i}:{k}")] for k, option in enumerate(question["options"])]
        keyboard.append([("⏭ Überspringen", f"q:{i}:s"), ("⏩ Rest überspringen", f"q:{i}:x")])
        return text, keyboard

    async def start_questions(self, chat_id: int, job: Job) -> None:
        questions = clarify.pending_questions(self.service.job_dir(job.id))
        if job.status != "waiting" or not questions:
            await self.messenger.send_text(chat_id, "Für diese Episode sind keine Fragen mehr offen.")
            return
        session = AnswerSession(job_id=job.id, questions=questions)
        text, keyboard = self._question_view(job, session)
        session.message_id = await self.messenger.send_text(chat_id, text, keyboard)
        self.sessions[chat_id] = session

    async def _question_callback(self, chat_id: int, message_id: int, callback_id: str, action: str) -> None:
        session = self.sessions.get(chat_id)
        index, _, choice = action.partition(":")
        if session is None or session.message_id != message_id or index != str(session.index):
            await self.messenger.answer_callback(callback_id, "Abgelaufen – tippe auf „Fragen beantworten“")
            return
        await self.messenger.answer_callback(callback_id)
        question = session.questions[session.index]
        if choice == "x":
            session.answers += [""] * (len(session.questions) - len(session.answers))
            session.index = len(session.questions)
            await self._finish_questions(chat_id, session)
        elif choice == "s":
            await self._record_answer(chat_id, session, "")
        elif choice.isdigit() and int(choice) < len(question["options"]):
            await self._record_answer(chat_id, session, question["options"][int(choice)])

    async def _record_answer(self, chat_id: int, session: AnswerSession, answer: str) -> None:
        session.answers.append(answer)
        session.index += 1
        if session.index >= len(session.questions):
            await self._finish_questions(chat_id, session)
            return
        job = self.store.get(session.job_id)
        text, keyboard = self._question_view(job, session)
        await self.messenger.edit_text(chat_id, session.message_id, text, keyboard)

    def _still_waiting(self, session: AnswerSession) -> bool:
        job = self.store.get(session.job_id)
        return job is not None and job.status == "waiting"

    async def _finish_questions(self, chat_id: int, session: AnswerSession) -> None:
        for other in [c for c, s in self.sessions.items() if s.job_id == session.job_id]:
            self.sessions.pop(other, None)  # every chat asked about this job is done with it
        try:
            self.service.answer(session.job_id, session.answers)
        except (JobError, ValueError) as exc:
            await self.messenger.edit_text(chat_id, session.message_id, f"Antworten nicht gespeichert: {esc(str(exc))}")
            return
        lines = [f"• {esc(q['question'])}\n  → <b>{esc(a) if a else 'Claude entscheidet'}</b>"
                 for q, a in zip(session.questions, session.answers)]
        await self.messenger.edit_text(chat_id, session.message_id,
                                       "✅ <b>Danke!</b> Die Recherche geht weiter.\n\n" + "\n".join(lines))
        job = self.store.get(session.job_id)
        for m in self._meta(session.job_id)["messages"]:
            await self._edit_status(job, m["chat_id"], m["message_id"])

    # --- sending episodes -----------------------------------------------------------------

    async def send_episode(self, chat_id: int, job: Job) -> None:
        job_dir = self.service.job_dir(job.id)
        episode_file = job_dir / "episode.json"
        audio = job_dir / "episode.mp3"
        if not episode_file.exists() or not audio.exists():
            await self.messenger.send_text(chat_id, "Die Audiodatei dieser Episode fehlt.")
            return
        episode = json.loads(episode_file.read_text(encoding="utf-8"))
        caption = f"<b>{esc(episode['title'])}</b>\n\n{esc(episode['summary'])}"
        if len(caption) > 1000:
            caption = caption[:997] + "…"
        if audio.stat().st_size > self.max_audio_bytes:
            await self.messenger.send_text(chat_id, caption + "\n\nDie Datei ist zu groß für Telegram." + self._web_link(job.id))
        else:
            meta = self._meta(job.id)
            file_id = await self.messenger.send_audio(
                chat_id, None if meta.get("audio_file_id") else audio,
                file_id=meta.get("audio_file_id"), title=episode["title"],
                performer=self.settings.podcast_name, duration=episode["duration_seconds"], caption=caption,
            )
            if file_id and file_id != meta.get("audio_file_id"):
                meta["audio_file_id"] = file_id  # re-sends reuse Telegram's copy
                self._save_meta(job.id, meta)
        handout = job_dir / "handout.pdf"
        if handout.exists():
            meta = self._meta(job.id)
            file_id = await self.messenger.send_document(
                chat_id, None if meta.get("handout_file_id") else handout, file_id=meta.get("handout_file_id"),
                filename=f"Handout - {episode['title'][:60]}.pdf", caption="📄 Handout zur Episode",
            )
            if file_id and file_id != meta.get("handout_file_id"):
                meta["handout_file_id"] = file_id
                self._save_meta(job.id, meta)
        chapters = "\n".join(f"{fmt_duration(c['start_ms'] // 1000)} {esc(c['title'])}" for c in episode["chapters"])
        sources = "\n".join(
            "• " + esc(s["title"]) + (f" ({esc(s['year'])})" if s.get("year") else "") for s in episode["sources"][:8]
        )
        text = f"<b>Kapitel</b>\n{chapters}"
        if sources:
            text += f"\n\n<b>Quellen</b>\n{sources}"
        await self.messenger.send_text(chat_id, text[:4000] + self._web_link(job.id))

    # --- commands -------------------------------------------------------------------------

    def _done_jobs(self, limit: int = 10) -> list[Job]:
        return [j for j in self.store.list(200) if j.status == "done"][:limit]

    async def cmd_current(self, chat_id: int) -> None:
        active = [j for j in self.store.list(200) if j.active or j.status == "waiting"]
        done = self._done_jobs(1)
        for job in sorted(active, key=lambda j: j.created_at):
            text, keyboard = self.status_text(job)
            message_id = await self.messenger.send_text(chat_id, text, keyboard)
            await self._set_status_message(job, chat_id, message_id)
        if active and done:  # checking progress should not re-send a whole episode
            await self.messenger.send_text(
                chat_id, f"Neueste fertige Episode: <b>{esc(done[0].display_title)}</b>",
                [[("▶ Senden", "l:")]],
            )
        elif done:
            await self.send_episode(chat_id, done[0])
        elif not active:
            await self.messenger.send_text(chat_id, "Noch keine fertige Episode. Schick mir ein Thema!")

    async def cmd_list(self, chat_id: int) -> None:
        jobs = self._done_jobs()
        failed = [j for j in self.store.list(50) if j.status in ("failed", "cancelled")][:5]
        if not jobs and not failed:
            await self.messenger.send_text(chat_id, "Noch keine fertigen Episoden. Schick mir ein Thema!")
            return
        lines = [
            f"{i}. {esc(j.display_title)} ({fmt_duration(j.duration_s)}, {j.created_at[8:10]}.{j.created_at[5:7]}.)"
            for i, j in enumerate(jobs, start=1)
        ]
        keyboard = [[(f"▶ {i}", f"g:{j.id}") for i, j in enumerate(jobs[k:k + 5], start=k + 1)]
                    for k in range(0, len(jobs), 5)]
        text = "<b>Letzte Episoden</b>\n" + "\n".join(lines) if jobs else ""
        if failed:
            text += "\n\n<b>Nicht fertig geworden</b>\n" + "\n".join(
                f"{STATUS_ICONS[j.status]} {esc(j.display_title)}" for j in failed)
            keyboard += [[(f"🔁 {j.display_title[:28]}", f"r:{j.id}")] for j in failed]
        await self.messenger.send_text(chat_id, text.strip() + "\n\nTippe auf eine Nummer zum Senden.", keyboard)

    async def cmd_get(self, chat_id: int, args: list[str]) -> None:
        jobs = self._done_jobs()
        try:
            job = jobs[int(args[0]) - 1] if args and int(args[0]) >= 1 else None
        except (ValueError, IndexError):
            job = None
        if job is None:
            await self.messenger.send_text(chat_id, "Bitte eine Nummer aus /liste angeben, z.B. /folge 2")
            return
        await self.send_episode(chat_id, job)

    async def cmd_status(self, chat_id: int) -> None:
        active = sorted((j for j in self.store.list(200) if j.active or j.status == "waiting"), key=lambda j: j.created_at)
        if not active:
            await self.messenger.send_text(chat_id, "Nichts in Arbeit. Schick mir ein Thema!")
            return
        lines = [f"{STATUS_ICONS[j.status]} {esc(j.display_title)} – {STATUS_LABELS[j.status]}"
                 + (f" ({STAGE_LABELS.get(j.stage, j.stage)})" if j.status == "running" and j.stage else "")
                 for j in active]
        await self.messenger.send_text(chat_id, "<b>Warteschlange</b>\n" + "\n".join(lines))

    async def cmd_cancel(self, chat_id: int) -> None:
        job_id = self.worker.current_job_id()
        if job_id is None:
            await self.messenger.send_text(chat_id, "Es läuft gerade kein Job.")
            return
        job = self.store.get(job_id)
        await self.messenger.send_text(
            chat_id, f"<b>{esc(job.display_title)}</b> wirklich abbrechen? Fertige Schritte bleiben erhalten.",
            [[("Ja, abbrechen", f"C:{job_id}"), ("Nein", f"k:{job_id}")]],
        )


# --- python-telegram-bot adapter ------------------------------------------------------------


class PTBMessenger:
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _markup(keyboard: Keyboard | None):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if not keyboard:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in keyboard]
        )

    async def send_text(self, chat_id: int, text: str, keyboard: Keyboard | None = None) -> int:
        message = await self.bot.send_message(
            chat_id, text, parse_mode="HTML", reply_markup=self._markup(keyboard), disable_web_page_preview=True
        )
        return message.message_id

    async def edit_text(self, chat_id: int, message_id: int, text: str, keyboard: Keyboard | None = None) -> None:
        from telegram.error import BadRequest

        try:
            await self.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                reply_markup=self._markup(keyboard), disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise

    async def send_audio(self, chat_id, path, *, file_id, title, performer, duration, caption) -> str:
        if file_id:
            message = await self.bot.send_audio(
                chat_id, file_id, caption=caption, parse_mode="HTML", title=title, performer=performer, duration=duration,
            )
        else:
            with open(path, "rb") as audio:
                message = await self.bot.send_audio(
                    chat_id, audio, caption=caption, parse_mode="HTML", title=title, performer=performer,
                    duration=duration, filename=path.name, read_timeout=120, write_timeout=300,
                )
        return message.audio.file_id if message.audio else ""

    async def send_document(self, chat_id, path, *, file_id, filename, caption) -> str:
        if file_id:
            message = await self.bot.send_document(chat_id, file_id, caption=caption)
        else:
            with open(path, "rb") as document:
                message = await self.bot.send_document(
                    chat_id, document, caption=caption, filename=filename, read_timeout=60, write_timeout=120,
                )
        return message.document.file_id if message.document else ""

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.bot.answer_callback_query(callback_id, text=text or None)


class TelegramRunner:
    """Owns the python-telegram-bot Application inside the server's event loop."""

    def __init__(self, settings: Settings, service: JobService, worker: Worker):
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

        builder = Application.builder().token(settings.telegram_bot_token)
        if settings.telegram_api_base_url:
            base = settings.telegram_api_base_url.rstrip("/")
            builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
        self.application = builder.build()
        self.max_audio_bytes = MAX_AUDIO_BYTES if not settings.telegram_api_base_url else 1900 * 1024 * 1024
        self.core = TelegramBot(settings, service, worker, PTBMessenger(self.application.bot))
        self.core.max_audio_bytes = self.max_audio_bytes
        self.worker = worker
        worker.listeners.append(self.core)

        async def on_command(update, context):
            # A generic MessageHandler does not fill context.args, so parse the text here.
            # The rest of the line stays one argument, which keeps a topic's wording intact.
            parts = (update.effective_message.text or "").split(None, 1)
            if not parts:
                return
            command = parts[0].lstrip("/").split("@")[0]
            await self.core.handle_command(update.effective_chat.id, command, parts[1:])

        async def on_text(update, context):
            await self.core.handle_text(update.effective_chat.id, update.effective_message.text or "")

        async def on_callback(update, context):
            query = update.callback_query
            await self.core.handle_callback(
                query.message.chat.id, query.message.message_id, query.id, query.data or ""
            )

        self.application.add_handler(MessageHandler(filters.COMMAND, on_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        self.application.add_handler(CallbackQueryHandler(on_callback))

    async def start(self) -> None:
        from telegram import BotCommand

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        await self.application.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        log.info("Telegram bot @%s started", self.application.bot.username)

    async def stop(self) -> None:
        if self.core in self.worker.listeners:
            self.worker.listeners.remove(self.core)
        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        if self.application._initialized:
            await self.application.shutdown()
