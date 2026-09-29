"""Intro/outro jingles and the cover image, uploaded via the web UI."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
from pathlib import Path

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_JINGLE_SECONDS = 60
COVER_SIZE = 1400
FILES = {"intro": "intro.mp3", "outro": "outro.mp3", "cover": "cover.jpg"}


class AssetError(ValueError):
    pass


def save_jingle(data: bytes, target: Path) -> float:
    """Validate an audio upload with ffprobe and store it as MP3. Returns the duration."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "upload"
        src.write_bytes(data)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "format=duration",
             "-of", "json", str(src)], capture_output=True, text=True,
        )
        try:
            duration = float(json.loads(probe.stdout)["format"]["duration"])
        except (KeyError, ValueError, json.JSONDecodeError):
            raise AssetError("Keine gültige Audiodatei") from None
        if duration > MAX_JINGLE_SECONDS:
            raise AssetError(f"Zu lang ({duration:.0f} s, max. {MAX_JINGLE_SECONDS} s)")
        out = Path(tmp) / "out.mp3"
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "44100", "-b:a", "128k", str(out)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise AssetError("Audiodatei konnte nicht umgewandelt werden")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(out.read_bytes())
    return duration


def save_cover(data: bytes, target: Path) -> None:
    """Center-crop to a square and store as 1400×1400 JPEG (what podcast apps expect)."""
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError):
        raise AssetError("Kein gültiges Bild") from None
    image = image.convert("RGB")
    side = min(image.size)
    left, top = (image.width - side) // 2, (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize((COVER_SIZE, COVER_SIZE), Image.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "JPEG", quality=88)
