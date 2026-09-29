"""Stage 3: text-to-speech → clips/ + clips/manifest.json."""

from __future__ import annotations

import json
import shutil

from app.models import Script

NAME = "tts"
DESCRIPTION = "Sprachausgabe wird erzeugt"


def reset(ctx) -> None:
    shutil.rmtree(ctx.path("clips"), ignore_errors=True)


def is_done(ctx) -> bool:
    return ctx.path("clips/manifest.json").exists()


async def run(ctx) -> None:
    script = Script.model_validate_json(ctx.path("script.json").read_text(encoding="utf-8"))
    clips_dir = ctx.path("clips")
    ctx.path("clips/manifest.json").unlink(missing_ok=True)
    clips = await ctx.tts.synthesize(script, clips_dir)
    manifest = [
        {"file": c.path.name, "chapter": c.chapter, "speaker": c.speaker} for c in clips
    ]
    ctx.log.write("tts", provider=ctx.tts.name, clips=len(clips), note=getattr(ctx.tts, "note", ""))
    (clips_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
