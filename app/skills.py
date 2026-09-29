"""Claude Code Agent Skills.

Bundled skills live in ``app/skills/<name>/SKILL.md``; user skills in
``<data>/skills/<name>/`` (same name = override). For each job the enabled skills
are copied into ``<job_dir>/.claude/skills/`` so Claude discovers them and the
episode keeps a snapshot of exactly what was used.

Which skills a stage enables is configured in ``<data>/skills.json``::

    {"stages": {"research": ["paper-research", "my-skill"], ...}}

Missing stages fall back to ``DEFAULT_STAGE_SKILLS``.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import APP_DIR

BUNDLED_DIR = APP_DIR / "skills"

DEFAULT_STAGE_SKILLS: dict[str, list[str]] = {
    "research": ["paper-research"],
    "script": ["german-podcast-dialogue", "tts-friendly-text", "fact-check"],
    "handout": ["handout-plots"],
}

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    bundled: bool
    overridden: bool = False


def _parse_front_matter(skill_md: Path) -> dict[str, str]:
    match = _FRONT_MATTER.match(skill_md.read_text(encoding="utf-8"))
    if not match:
        return {}
    meta = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta


class SkillStore:
    def __init__(self, user_dir: Path, config_file: Path):
        self.user_dir = user_dir
        self.config_file = config_file

    def _scan(self, root: Path) -> dict[str, Path]:
        if not root.is_dir():
            return {}
        return {p.parent.name: p.parent for p in sorted(root.glob("*/SKILL.md"))}

    def all(self) -> dict[str, Skill]:
        bundled = self._scan(BUNDLED_DIR)
        user = self._scan(self.user_dir)
        skills = {}
        for name, path in {**bundled, **user}.items():
            meta = _parse_front_matter(path / "SKILL.md")
            skills[name] = Skill(
                name=name,
                description=meta.get("description", ""),
                path=path,
                bundled=name in bundled,
                overridden=name in bundled and name in user,
            )
        return skills

    def _config(self) -> dict:
        if self.config_file.exists():
            return json.loads(self.config_file.read_text(encoding="utf-8"))
        return {}

    def stage_skills(self, stage: str, extra: list[str] | None = None) -> list[str]:
        configured = self._config().get("stages", {}).get(stage)
        names = list(DEFAULT_STAGE_SKILLS.get(stage, []) if configured is None else configured)
        for name in extra or []:
            if name not in names:
                names.append(name)
        available = self.all()
        missing = [n for n in names if n not in available]
        if missing:
            raise KeyError(f"Unknown skill(s): {', '.join(missing)}")
        return names

    def install(self, job_dir: Path, names: list[str]) -> Path:
        """Copy the given skills into the job's ``.claude/skills`` (idempotent)."""
        target_root = job_dir / ".claude" / "skills"
        target_root.mkdir(parents=True, exist_ok=True)
        available = self.all()
        for name in names:
            target = target_root / name
            if target.exists():
                continue
            shutil.copytree(available[name].path, target)
        return target_root
