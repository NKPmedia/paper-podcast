"""Which settings the web UI shows, how they are labelled, and how form input is parsed.

The first-run setup dialog shows only the fields marked ``setup=True``; the settings
page shows everything, grouped into sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.config import Settings, SettingsError, save_settings


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"  # text | secret | int | float | bool | select | url | chatids
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()
    setup: bool = False
    required: bool = False
    placeholder: str = ""
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    intro: str
    fields: tuple[Field, ...] = field(default_factory=tuple)


MODELS = "Alias (<code>haiku</code>, <code>sonnet</code>, <code>opus</code>) oder eine vollständige Modell-ID."

SECTIONS: tuple[Section, ...] = (
    Section("podcast", "Podcast", "Name und Sprecher. Persönlichkeit und Sprechweise passt du unter "
            "<a href=\"/prompts/personas\">Prompts → Sprecher</a> an.", (
        Field("podcast_name", "Name des Podcasts", setup=True, required=True),
        Field("host_name", "Host (stellt die Fragen)", setup=True, required=True),
        Field("expert_name", "Experte bzw. Expertin", setup=True, required=True),
        Field("words_per_minute", "Sprechtempo (Wörter pro Minute)", "int", min=80, max=220,
              help="Daraus berechnet sich die Ziel-Wortzahl des Skripts für kurz, mittel und lang."),
        Field("default_language", "Standardsprache neuer Episoden", "select", options=(
            ("de", "Deutsch"), ("en", "Englisch")),
              help="Vorauswahl im Formular und in Telegram; pro Episode änderbar."),
    )),
    Section("claude", "Claude", "Zugang und Modelle für Recherche und Skript.", (
        Field("claude_code_oauth_token", "Claude-Token", "secret", setup=True, required=True, placeholder="sk-ant-oat01-…",
              help="Auf deinem eigenen Rechner einmal <code>claude setup-token</code> ausführen (Pro- oder "
                   "Max-Abo) und den Token hier einfügen. Er ist ein Jahr gültig."),
        Field("research_scout_model", "Modell der Recherche-Scouts", help="Sucht und bewertet Quellen. " + MODELS),
        Field("research_main_model", "Modell für Planung, Auswahl und Lesen",
              help="Plant die Recherche, wählt die Paper und liest sie im Volltext. " + MODELS),
        Field("script_model", "Modell für Skript und Handout", help=MODELS),
        Field("claude_max_turns_script", "Max. Runden pro Skript-Aufruf", "int", min=5, max=200),
    )),
    Section("voices", "Stimmen", "Welche Sprachausgabe die Episoden spricht.", (
        Field("tts_provider", "Sprachausgabe", "select", options=(
            ("auto", "Automatisch – Gemini, wenn ein Key gesetzt ist, sonst Edge"),
            ("gemini", "Gemini (mit Edge als Ausweichlösung)"),
            ("edge", "Edge (kostenlos, ohne Key)"))),
        Field("gemini_api_key", "Gemini-API-Key", "secret", placeholder="AIza…",
              help="Kostenlos bei <a href=\"https://aistudio.google.com\" target=\"_blank\" rel=\"noopener\">Google "
                   "AI Studio</a>. Beide Stimmen sprechen dann natürlicher in einem Zug; ist das Tageskontingent "
                   "aufgebraucht, übernimmt Edge die ganze Episode."),
        Field("gemini_tts_model", "Gemini-Modell"),
        Field("gemini_voice_host", "Gemini-Stimme Host", help="z.B. Kore, Aoede, Leda"),
        Field("gemini_voice_expert", "Gemini-Stimme Experte", help="z.B. Charon, Puck, Orus"),
        Field("edge_voice_host", "Edge-Stimme Host"),
        Field("edge_voice_expert", "Edge-Stimme Experte",
              help="Für deutsche Episoden, z.B. de-DE-SeraphinaMultilingualNeural, de-DE-FlorianMultilingualNeural, "
                   "de-DE-KatjaNeural, de-DE-ConradNeural."),
        Field("edge_voice_host_en", "Edge-Stimme Host (Englisch)"),
        Field("edge_voice_expert_en", "Edge-Stimme Experte (Englisch)",
              help="Für englische Episoden, z.B. en-US-AvaMultilingualNeural, en-US-AndrewMultilingualNeural, "
                   "en-GB-SoniaNeural, en-GB-RyanNeural. Die Gemini-Stimmen sprechen beide Sprachen."),
        Field("edge_concurrency", "Parallele Edge-Anfragen", "int", min=1, max=10),
    )),
    Section("audio", "Audio", "Pausen, Lautheit und Qualität. Intro, Outro und Cover lädst du unter "
            "<a href=\"/connections#klang\">Verbindungen → Klang &amp; Cover</a> hoch.", (
        Field("pause_line_ms", "Pause zwischen Sprecherwechseln (ms)", "int", min=0, max=3000),
        Field("pause_chapter_ms", "Pause zwischen Kapiteln (ms)", "int", min=0, max=10000),
        Field("target_lufs", "Ziel-Lautheit (LUFS)", "float", min=-30, max=-9,
              help="−16 ist der übliche Wert für Podcasts."),
        Field("mp3_bitrate", "MP3-Bitrate", "select", options=(
            ("64k", "64 kbit/s – kleinste Dateien"), ("96k", "96 kbit/s – Standard"),
            ("128k", "128 kbit/s"), ("192k", "192 kbit/s – beste Qualität"))),
    )),
    Section("telegram", "Telegram", "Episoden per Telegram anfragen und empfangen.", (
        Field("telegram_bot_token", "Bot-Token", "secret", setup=True, placeholder="123456789:AA…",
              help="Optional. Bei <a href=\"https://t.me/BotFather\" target=\"_blank\" rel=\"noopener\">@BotFather</a> "
                   "mit <code>/newbot</code> einen Bot anlegen und den Token einfügen."),
        Field("telegram_allowed_chat_ids", "Erlaubte Chat-IDs", "chatids", placeholder="leer lassen – der Bot nennt dir deine ID",
              help="Solange das Feld leer ist, antwortet der Bot jedem mit seiner Chat-ID. Trag deine ein, dann "
                   "reagiert er nur noch auf dich. Mehrere durch Kommas trennen."),
        Field("telegram_notify_all", "Auch Episoden aus der Weboberfläche ankündigen und senden", "bool"),
        Field("telegram_api_base_url", "Eigener Bot-API-Server", "url", placeholder="http://telegram-bot-api:8081",
              help="Nur nötig für Audiodateien über 50 MB."),
    )),
    Section("research", "Recherche", "Grenzen für das Herunterladen der Paper.", (
        Field("download_max_mb", "Max. Dateigröße pro Paper (MB)", "int", min=1, max=200),
        Field("download_timeout_s", "Zeitlimit pro Download (s)", "int", min=5, max=600),
        Field("paper_max_chars", "Max. Länge eines Volltexts (Zeichen)", "int", min=10_000, max=2_000_000,
              help="Längere Texte werden gekürzt, damit mehrere Paper zusammen in Claudes Kontext passen. "
                   "150.000 Zeichen (etwa 35.000 Tokens) reichen für den Hauptteil fast jedes Papers."),
    )),
    Section("access", "Zugang & Netzwerk", "", (
        Field("public_base_url", "Öffentliche Adresse", "url", placeholder="https://podcast.example.org",
              help="Unter dieser Adresse ist die Seite über deinen Reverse-Proxy erreichbar – für Links in Telegram "
                   "und im Podcast-Feed."),
        Field("cookie_secure", "Anmeldung nur über HTTPS erlauben", "bool",
              help="Nur für Tests ohne HTTPS-Proxy ausschalten, z.B. im Heimnetz."),
        Field("timezone", "Zeitzone", placeholder="Europe/Berlin"),
        Field("edge_proxy", "HTTP-Proxy für Edge TTS", "url", placeholder="http://proxy:3128",
              help="Nur nötig, wenn der Server nicht direkt ins Internet kommt."),
    )),
)

FIELDS = {f.key: f for s in SECTIONS for f in s.fields}
SETUP_FIELDS = tuple(f for s in SECTIONS for f in s.fields if f.setup)
CHAT_IDS = re.compile(r"^\s*(-?\d+\s*(,\s*-?\d+\s*)*)?$")


def parse(fields, form) -> tuple[dict, dict[str, str]]:
    """Turn form input for ``fields`` into typed values. Secrets left empty are kept."""
    values, errors = {}, {}
    for f in fields:
        raw = form.get(f.key)
        if f.kind == "bool":
            if form.get(f"{f.key}__present") == "1":
                values[f.key] = form.get(f.key) == "on"
            continue
        if f.kind == "secret":
            if form.get(f"{f.key}__clear") == "on":
                values[f.key] = ""
            elif str(raw or "").strip():
                values[f.key] = str(raw).strip()
            continue
        if raw is None:
            continue
        text = str(raw).strip()
        if f.required and not text:
            errors[f.key] = "Bitte ausfüllen."
            continue
        if f.kind in ("int", "float"):
            try:
                number = int(text) if f.kind == "int" else float(text.replace(",", "."))
            except ValueError:
                errors[f.key] = "Bitte eine Zahl eingeben."
                continue
            if (f.min is not None and number < f.min) or (f.max is not None and number > f.max):
                errors[f.key] = f"Erlaubt sind Werte von {f.min:g} bis {f.max:g}."
                continue
            values[f.key] = number
        elif f.kind == "select":
            if text not in {value for value, _ in f.options}:
                errors[f.key] = "Ungültige Auswahl."
                continue
            values[f.key] = text
        elif f.kind == "url":
            text = text.rstrip("/")
            if text and urlparse(text).scheme not in ("http", "https"):
                errors[f.key] = "Muss mit https:// (oder http://) beginnen."
                continue
            values[f.key] = text or (None if f.key == "edge_proxy" else "")
        elif f.kind == "chatids":
            if not CHAT_IDS.match(text):
                errors[f.key] = "Nur Zahlen, getrennt durch Kommas."
                continue
            values[f.key] = text.replace(" ", "")
        else:
            values[f.key] = text[:200]
    token = values.get("telegram_bot_token")
    if token and not re.match(r"^\d+:[\w-]{20,}$", token):
        errors["telegram_bot_token"] = "Sieht nicht wie ein Bot-Token von @BotFather aus (Zahl:Zeichenkette)."
    if "timezone" in values:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(values["timezone"])
        except Exception:
            errors["timezone"] = "Unbekannte Zeitzone, z.B. Europe/Berlin."
    return values, errors


def apply(settings: Settings, values: dict) -> dict[str, str]:
    """Save values; returns errors per field (empty on success)."""
    try:
        save_settings(settings, values)
    except SettingsError as exc:
        return exc.errors
    return {}
