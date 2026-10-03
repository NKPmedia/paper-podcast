"""Health checks for the external services and keys, shown on the settings page.

Each check returns a state (``ok``, ``warn``, ``error``, ``off``) and a short German
message. Results are cached and only re-run when the relevant settings change (or on
request), so opening the settings page does not burn Claude quota every time.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from app.claude import AgentSDKRunner, ClaudeCall, ClaudeRunner, claude_auth_configured
from app.config import Settings
from app.errors import PodcastError

CACHE_SECONDS = 6 * 3600
TIMEOUTS = {"claude": 60, "gemini": 20, "telegram": 20, "edge": 30}
TIMEOUT_HINTS = {
    "claude": "Häufigste Ursachen: ein ungültiger oder abgelaufener Token, oder der Server erreicht "
              "api.anthropic.com nicht.",
    "edge": "Der Server erreicht den Edge-Sprachdienst nicht.",
}


@dataclass
class CheckResult:
    state: str  # ok | warn | error | off
    message: str
    checked_at: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


EdgeProbe = Callable[[Settings], Awaitable[None]]


async def default_edge_probe(settings: Settings) -> None:
    import edge_tts

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "probe.mp3"
        await edge_tts.Communicate("Test.", settings.edge_voice_host, proxy=settings.edge_proxy).save(str(out))
        if out.stat().st_size == 0:
            raise RuntimeError("empty audio")


class KeyChecker:
    def __init__(self, settings: Settings, claude: ClaudeRunner | None = None,
                 http: httpx.AsyncClient | None = None, edge_probe: EdgeProbe | None = None):
        self.settings = settings
        self.claude = claude or AgentSDKRunner()
        self.http = http
        self.edge_probe = edge_probe or default_edge_probe
        self.cache: dict[str, tuple[str, CheckResult]] = {}
        self._lock = asyncio.Lock()

    # Which settings each check depends on; a change re-runs the check.
    def fingerprint(self, name: str) -> str:
        s = self.settings
        parts = {
            "claude": (s.claude_code_oauth_token, s.research_scout_model, claude_auth_configured()),
            "gemini": (s.gemini_api_key, s.gemini_tts_model, s.tts_provider),
            "telegram": (s.telegram_bot_token, s.telegram_api_base_url, s.telegram_allowed_chat_ids),
            "edge": (s.edge_voice_host, s.edge_proxy, s.tts_provider, bool(s.gemini_api_key)),
        }[name]
        return hashlib.sha256(repr(parts).encode()).hexdigest()

    def cached(self) -> dict[str, dict]:
        return {name: result.as_dict() for name, (fp, result) in self.cache.items() if fp == self.fingerprint(name)}

    async def run(self, force: bool = False) -> dict[str, dict]:
        async with self._lock:
            names = [n for n in TIMEOUTS if force or self._stale(n)]
            results = await asyncio.gather(*(self._run_one(n) for n in names))
            for name, result in zip(names, results):
                self.cache[name] = (self.fingerprint(name), result)
            return self.cached()

    def _stale(self, name: str) -> bool:
        entry = self.cache.get(name)
        return (entry is None or entry[0] != self.fingerprint(name)
                or time.time() - entry[1].checked_at > CACHE_SECONDS)

    async def _run_one(self, name: str) -> CheckResult:
        check = getattr(self, f"check_{name}")
        try:
            result = await asyncio.wait_for(check(), TIMEOUTS[name])
        except asyncio.TimeoutError:
            result = CheckResult("error", f"Keine Antwort innerhalb von {TIMEOUTS[name]} Sekunden. "
                                          + TIMEOUT_HINTS.get(name, "Netzwerkverbindung prüfen."))
        except Exception as exc:  # a broken check must never break the page
            result = CheckResult("error", f"Prüfung fehlgeschlagen: {type(exc).__name__}: {exc}")
        result.checked_at = time.time()
        return result

    async def _get(self, url: str, **kw) -> httpx.Response:
        if self.http:
            return await self.http.get(url, **kw)
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.get(url, **kw)

    # --- individual checks ---------------------------------------------------------

    async def check_claude(self) -> CheckResult:
        if not claude_auth_configured():
            return CheckResult("error", "Kein Claude-Token eingetragen – Episoden können nicht entstehen.")
        model = self.settings.research_scout_model
        with tempfile.TemporaryDirectory() as tmp:
            try:
                await self.claude.run(ClaudeCall(prompt="Antworte nur mit dem Wort OK.", cwd=Path(tmp), tools=[],
                                                 max_turns=1, model=model))
            except PodcastError as exc:
                return CheckResult("error", exc.message)
        return CheckResult("ok", f"Token gültig – Claude antwortet (getestet mit Modell „{model}“).")

    async def check_gemini(self) -> CheckResult:
        s = self.settings
        if not s.gemini_api_key:
            if s.tts_provider == "gemini":
                return CheckResult("warn", "Sprachausgabe steht auf Gemini, aber es ist kein Key eingetragen – "
                                           "es wird Edge genutzt.")
            return CheckResult("off", "Kein Key eingetragen – die Episoden spricht Edge.")
        try:
            response = await self._get(
                f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_tts_model}",
                headers={"x-goog-api-key": s.gemini_api_key},
            )
        except httpx.HTTPError as exc:
            return CheckResult("error", f"Google ist nicht erreichbar: {type(exc).__name__}.")
        text = response.text[:2000]
        if response.status_code == 200:
            if s.tts_provider == "edge":
                return CheckResult("warn", "Key gültig, wird aber nicht genutzt (Sprachausgabe steht auf Edge).")
            return CheckResult("ok", f"Key gültig, Modell „{s.gemini_tts_model}“ verfügbar.")
        if (response.status_code == 400 and "API_KEY_INVALID" in text) or response.status_code == 401:
            return CheckResult("error", "Der Key ist ungültig. Neuen Key in Google AI Studio erzeugen.")
        if response.status_code == 403:
            return CheckResult("error", "Der Key hat keinen Zugriff auf die Gemini-API (gesperrt oder API nicht "
                                        "aktiviert).")
        if response.status_code == 404:
            return CheckResult("error", f"Das Modell „{s.gemini_tts_model}“ gibt es nicht (mehr). Modellnamen prüfen.")
        if response.status_code == 429:
            return CheckResult("warn", "Key gültig, aber das Kontingent ist gerade erschöpft – bis dahin spricht Edge.")
        return CheckResult("error", f"Unerwartete Antwort von Google (HTTP {response.status_code}).")

    async def check_telegram(self) -> CheckResult:
        s = self.settings
        if not s.telegram_bot_token:
            return CheckResult("off", "Kein Bot-Token eingetragen – Telegram ist aus.")
        base = (s.telegram_api_base_url or "https://api.telegram.org").rstrip("/")
        try:
            response = await self._get(f"{base}/bot{s.telegram_bot_token}/getMe")
        except httpx.HTTPError as exc:
            return CheckResult("error", f"Telegram ist nicht erreichbar: {type(exc).__name__}.")
        if response.status_code == 401 or response.status_code == 404:
            return CheckResult("error", "Der Bot-Token ist ungültig. Bei @BotFather mit /token neu holen.")
        if response.status_code != 200 or not response.json().get("ok"):
            return CheckResult("error", f"Unerwartete Antwort von Telegram (HTTP {response.status_code}).")
        username = response.json()["result"].get("username", "?")
        if not s.telegram_chat_ids:
            return CheckResult("warn", f"Bot @{username} funktioniert. Noch keine Chat-ID eingetragen: Schreib dem "
                                       "Bot, er antwortet mit deiner ID.")
        return CheckResult("ok", f"Bot @{username} funktioniert.")

    async def check_edge(self) -> CheckResult:
        try:
            await self.edge_probe(self.settings)
        except Exception as exc:
            serious = self.settings.tts_provider == "edge" or not self.settings.gemini_api_key
            return CheckResult("error" if serious else "warn",
                               f"Edge-Sprachausgabe nicht erreichbar ({type(exc).__name__})"
                               + ("" if serious else " – nur als Ausweichlösung für Gemini betroffen") + ".")
        return CheckResult("ok", f"Edge-Sprachausgabe erreichbar (Stimme {self.settings.edge_voice_host}).")
