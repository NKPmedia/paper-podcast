"""Stage 4: stitch clips with pauses, add optional jingles, normalize loudness,
write MP3 with ID3 tags and chapter markers → episode.mp3 + episode.json.

Clips are decoded to one sample format and joined in Python (``wave``), which
gives exact chapter timestamps; ffmpeg does decoding, loudnorm and encoding.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path

from app.errors import PodcastError
from app.models import Script, Source

NAME = "audio"
DESCRIPTION = "Audio wird gemischt und gemastert"

SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1


def is_done(ctx) -> bool:
    return ctx.path("episode.mp3").exists() and ctx.path("episode.json").exists()


def ffmpeg(*args: str) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PodcastError("Das Audio konnte nicht zusammengesetzt werden (ffmpeg-Fehler).",
                           f"ffmpeg failed: {' '.join(cmd)}\n{proc.stderr[-2000:]}")
    return proc


def to_wav(src: Path, dst: Path) -> None:
    ffmpeg("-i", str(src), "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst))


def read_frames(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def silence(ms: int) -> bytes:
    return b"\x00" * (SAMPLE_RATE * ms // 1000) * SAMPLE_WIDTH * CHANNELS


def frames_to_ms(n_bytes: int) -> int:
    return n_bytes * 1000 // (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)


def _meta_escape(value: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", value)


def build_metadata(title: str, artist: str, album: str, comment: str, chapters: list[tuple[str, int, int]]) -> str:
    lines = [
        ";FFMETADATA1",
        f"title={_meta_escape(title)}",
        f"artist={_meta_escape(artist)}",
        f"album={_meta_escape(album)}",
        f"comment={_meta_escape(comment)}",
        f"date={datetime.now().year}",
        "genre=Podcast",
    ]
    for chapter_title, start, end in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={_meta_escape(chapter_title)}"]
    return "\n".join(lines) + "\n"


def loudnorm_params(wav: Path, target: float) -> dict:
    proc = ffmpeg(
        "-i", str(wav), "-af", f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.DOTALL)
    if not match:
        raise RuntimeError("Could not parse loudnorm output")
    return json.loads(match.group(0))


async def run(ctx) -> None:
    settings = ctx.settings
    script = Script.model_validate_json(ctx.path("script.json").read_text(encoding="utf-8"))
    sources = [Source(**s) for s in json.loads(ctx.path("sources.json").read_text(encoding="utf-8"))]
    manifest = json.loads(ctx.path("clips/manifest.json").read_text(encoding="utf-8"))

    work = ctx.path("work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        pcm = bytearray()
        chapter_starts: dict[int, int] = {}

        intro = settings.assets_dir / "intro.mp3"
        if intro.exists():
            to_wav(intro, work / "intro.wav")
            pcm += read_frames(work / "intro.wav") + silence(300)

        previous_chapter = None
        for i, clip in enumerate(manifest):
            if previous_chapter is not None:
                pause = settings.pause_chapter_ms if clip["chapter"] != previous_chapter else settings.pause_line_ms
                pcm += silence(pause)
            if clip["chapter"] not in chapter_starts:
                chapter_starts[clip["chapter"]] = frames_to_ms(len(pcm))
            wav = work / f"{i:04d}.wav"
            to_wav(ctx.path("clips") / clip["file"], wav)
            pcm += read_frames(wav)
            previous_chapter = clip["chapter"]

        outro = settings.assets_dir / "outro.mp3"
        if outro.exists():
            to_wav(outro, work / "outro.wav")
            pcm += silence(600) + read_frames(work / "outro.wav")

        total_ms = frames_to_ms(len(pcm))
        full = work / "full.wav"
        with wave.open(str(full), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(bytes(pcm))

        ordered = sorted(chapter_starts.items())
        chapters = []
        for n, (ci, start) in enumerate(ordered):
            start = 0 if n == 0 else start  # first chapter includes the intro jingle
            end = ordered[n + 1][1] if n + 1 < len(ordered) else total_ms
            chapters.append((script.chapters[ci].title, start, end))

        comment = script.summary + "\n\nQuellen:\n" + "\n".join(
            f"- {s.title} ({s.year}) {s.url}".strip() for s in sources
        )
        meta = work / "meta.txt"
        meta.write_text(
            build_metadata(script.title, settings.podcast_name, settings.podcast_name, comment, chapters),
            encoding="utf-8",
        )

        ln = loudnorm_params(full, settings.target_lufs)
        loudnorm = (
            f"loudnorm=I={settings.target_lufs}:TP=-1.5:LRA=11"
            f":measured_I={ln['input_i']}:measured_TP={ln['input_tp']}"
            f":measured_LRA={ln['input_lra']}:measured_thresh={ln['input_thresh']}"
            f":offset={ln['target_offset']}:linear=true"
        )
        out_tmp = work / "episode.mp3"
        args = ["-i", str(full), "-i", str(meta)]
        cover = settings.assets_dir / "cover.jpg"
        if cover.exists():
            args += ["-i", str(cover)]
        args += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        if cover.exists():
            args += ["-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
        args += [
            "-af", loudnorm, "-ar", "44100", "-ac", "1",
            "-c:a", "libmp3lame", "-b:a", settings.mp3_bitrate,
            "-id3v2_version", "3", "-write_id3v1", "1",
            str(out_tmp),
        ]
        ffmpeg(*args)
        out_tmp.rename(ctx.path("episode.mp3"))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    episode = {
        "title": script.title,
        "summary": script.summary,
        "topic": ctx.request.topic,
        "language": ctx.request.options.language.value,
        "duration_seconds": round(total_ms / 1000),
        "chapters": [{"title": t, "start_ms": s} for t, s, _ in chapters],
        "sources": [s.model_dump() for s in sources],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tts": ctx.tts.name,
    }
    ctx.path("episode.json").write_text(json.dumps(episode, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.log.write("episode", duration_seconds=episode["duration_seconds"])
