"""Private podcast RSS feed (iTunes + Podcasting 2.0 chapters) for podcast apps.

The feed and its files live under ``/feed/<token>/``. The long random token is the
only credential, because podcast apps cannot log in.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

from app.config import Settings
from app.db import Job


def _rfc2822(value: str | None) -> str:
    moment = datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
    return format_datetime(moment.astimezone(timezone.utc), usegmt=True)


def _duration(seconds: int | None) -> str:
    seconds = seconds or 0
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


TEXT = {
    "de": {"chapters": "Kapitel", "sources": "Quellen", "open": "Im Browser öffnen", "handout": "Handout zur Episode",
           "description": "Private Podcast-Episoden zu Forschungsthemen, erstellt mit Claude."},
    "en": {"chapters": "Chapters", "sources": "Sources", "open": "Open in the browser", "handout": "Handout for the episode",
           "description": "Private podcast episodes on research topics, made with Claude."},
}


def episode_language(episode: dict, job_dir) -> str:
    """The episode's language: stored in episode.json, else in the request (older episodes)."""
    language = episode.get("language")
    if not language:
        request = job_dir / "request.json"
        if request.exists():
            data = json.loads(request.read_text(encoding="utf-8"))
            language = data.get("request", {}).get("options", {}).get("language")
    return language if language in TEXT else "de"


def show_notes(episode: dict, handout_url: str | None, web_url: str | None, language: str = "de") -> str:
    text = TEXT.get(language, TEXT["de"])
    parts = [f"<p>{html.escape(episode['summary'])}</p>"]
    if episode.get("chapters"):
        chapters = "".join(
            f"<li>{c['start_ms'] // 60000}:{c['start_ms'] // 1000 % 60:02d} {html.escape(c['title'])}</li>"
            for c in episode["chapters"]
        )
        parts.append(f"<p><b>{text['chapters']}</b></p><ul>{chapters}</ul>")
    if episode.get("sources"):
        items = []
        for s in episode["sources"]:
            label = html.escape(s["title"] + (f" ({s['year']})" if s.get("year") else ""))
            url = s.get("url", "")
            items.append(f'<li><a href="{html.escape(url)}">{label}</a></li>' if url.startswith("http") else f"<li>{label}</li>")
        parts.append(f"<p><b>{text['sources']}</b></p><ul>{''.join(items)}</ul>")
    if handout_url:
        parts.append(f'<p><a href="{html.escape(handout_url)}">Handout (PDF)</a></p>')
    if web_url:
        parts.append(f'<p><a href="{html.escape(web_url)}">{text["open"]}</a></p>')
    return "".join(parts)


def chapters_json(episode: dict) -> dict:
    return {
        "version": "1.2.0",
        "chapters": [{"startTime": c["start_ms"] / 1000, "title": c["title"]} for c in episode["chapters"]],
    }


def build_feed(settings: Settings, jobs: list[Job], base_url: str, token: str) -> str:
    base = base_url.rstrip("/")
    feed_base = f"{base}/feed/{token}"
    has_cover = (settings.assets_dir / "cover.jpg").exists()
    channel_language = settings.default_language if settings.default_language in TEXT else "de"
    items = []
    for job in jobs:
        job_dir = settings.episodes_dir / job.id
        audio = job_dir / "episode.mp3"
        episode_file = job_dir / "episode.json"
        if job.status != "done" or not audio.exists() or not episode_file.exists():
            continue
        episode = json.loads(episode_file.read_text(encoding="utf-8"))
        handout_url = f"{feed_base}/handout/{job.id}.pdf" if (job_dir / "handout.pdf").exists() else None
        notes = show_notes(episode, handout_url, f"{base}/episodes/{job.id}", episode_language(episode, job_dir))
        items.append(
            "<item>"
            f"<title>{escape(episode['title'])}</title>"
            f"<guid isPermaLink=\"false\">{escape(job.id)}</guid>"
            f"<pubDate>{_rfc2822(job.finished_at or job.created_at)}</pubDate>"
            f"<description>{escape(episode['summary'])}</description>"
            f"<content:encoded>{escape(notes)}</content:encoded>"
            f"<enclosure url=\"{escape(feed_base)}/audio/{escape(job.id)}.mp3\" "
            f"length=\"{audio.stat().st_size}\" type=\"audio/mpeg\"/>"
            f"<itunes:duration>{_duration(episode.get('duration_seconds'))}</itunes:duration>"
            f"<itunes:summary>{escape(episode['summary'])}</itunes:summary>"
            "<itunes:explicit>false</itunes:explicit>"
            f"<podcast:chapters url=\"{escape(feed_base)}/chapters/{escape(job.id)}.json\" "
            "type=\"application/json+chapters\"/>"
            "</item>"
        )
    image = (
        f"<itunes:image href=\"{escape(feed_base)}/cover.jpg\"/>"
        f"<image><url>{escape(feed_base)}/cover.jpg</url><title>{escape(settings.podcast_name)}</title>"
        f"<link>{escape(base)}/</link></image>"
        if has_cover else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:podcast="https://podcastindex.org/namespace/1.0" '
        'xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>"
        f"<title>{escape(settings.podcast_name)}</title>"
        f"<link>{escape(base)}/</link>"
        f"<atom:link href=\"{escape(feed_base)}.xml\" rel=\"self\" type=\"application/rss+xml\"/>"
        f"<description>{TEXT[channel_language]['description']}</description>"
        f"<language>{channel_language}</language>"
        f"<itunes:author>{escape(settings.podcast_name)}</itunes:author>"
        "<itunes:category text=\"Science\"/>"
        "<itunes:explicit>false</itunes:explicit>"
        "<itunes:block>Yes</itunes:block>"
        f"{image}"
        + "".join(items)
        + "</channel></rss>"
    )
