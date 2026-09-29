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

import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config import APP_DIR

BUNDLED_DIR = APP_DIR / "skills"

DEFAULT_STAGE_SKILLS: dict[str, list[str]] = {
    "research": ["paper-research"],
    "script": ["german-podcast-dialogue", "tts-friendly-text", "fact-check"],
    "handout": ["latex-handout", "handout-plots"],
}

STAGES = tuple(DEFAULT_STAGE_SKILLS)
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_ZIP_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024


class SkillError(ValueError):
    pass


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

    # --- management (web UI) -------------------------------------------------------

    def stage_config(self) -> dict[str, list[str]]:
        """Enabled skills per stage, ignoring names that no longer exist."""
        available = self.all()
        configured = self._config().get("stages", {})
        return {
            stage: [n for n in configured.get(stage, DEFAULT_STAGE_SKILLS[stage]) if n in available]
            for stage in STAGES
        }

    def set_stage_config(self, config: dict[str, list[str]]) -> None:
        available = self.all()
        for stage, names in config.items():
            if stage not in STAGES:
                raise SkillError(f"Unbekannte Stufe: {stage}")
            unknown = [n for n in names if n not in available]
            if unknown:
                raise SkillError(f"Unbekannte Skills: {', '.join(unknown)}")
        data = self._config()
        data["stages"] = {stage: list(config.get(stage, [])) for stage in STAGES}
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _user_path(self, name: str) -> Path:
        if not NAME_PATTERN.match(name):
            raise SkillError("Skill-Namen: Kleinbuchstaben, Ziffern und Bindestriche")
        return self.user_dir / name

    def create(self, name: str, description: str) -> Path:
        path = self._user_path(name)
        if name in self.all():
            raise SkillError(f"Skill {name} existiert bereits")
        path.mkdir(parents=True)
        description = " ".join(description.split()) or "Beschreibe, wann Claude diesen Skill nutzen soll."
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nAnleitung für Claude …\n",
            encoding="utf-8",
        )
        return path

    def override(self, name: str) -> Path:
        """Copy a bundled skill into the user dir so it can be edited."""
        skill = self.all().get(name)
        if skill is None or not skill.bundled:
            raise SkillError(f"Kein mitgelieferter Skill: {name}")
        path = self._user_path(name)
        if not path.exists():
            shutil.copytree(BUNDLED_DIR / name, path)
        return path

    def remove_user_copy(self, name: str) -> None:
        """Delete a user skill, or reset an override to the bundled version."""
        path = self._user_path(name)
        if not path.exists():
            raise SkillError(f"Keine eigene Version von {name}")
        shutil.rmtree(path)
        if name not in self.all():  # was a user-only skill: drop it from stage config
            config = self._config().get("stages")
            if config:
                self.set_stage_config({s: [n for n in names if n != name] for s, names in config.items()})

    def files(self, name: str) -> list[str]:
        skill = self.all()[name]
        return sorted(str(p.relative_to(skill.path)) for p in skill.path.rglob("*") if p.is_file())

    def _file_path(self, root: Path, rel: str) -> Path:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or not rel:
            raise SkillError("Ungültiger Dateipfad")
        return root / pure

    def read_file(self, name: str, rel: str) -> str:
        path = self._file_path(self.all()[name].path, rel)
        return path.read_text(encoding="utf-8")

    def write_file(self, name: str, rel: str, text: str) -> None:
        skill = self.all().get(name)
        if skill is None or skill.path.parent != self.user_dir:
            raise SkillError("Nur eigene Skills oder Überschreibungen sind bearbeitbar")
        if len(text.encode()) > MAX_FILE_BYTES:
            raise SkillError("Datei zu groß")
        if rel == "SKILL.md" and not _FRONT_MATTER.match(text):
            raise SkillError("SKILL.md braucht einen Front-Matter-Block mit name und description")
        path = self._file_path(skill.path, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def import_zip(self, data: bytes) -> str:
        """Install a skill from a zip with SKILL.md at the root or in one top-level folder."""
        if len(data) > MAX_ZIP_BYTES:
            raise SkillError("Zip-Datei zu groß (max. 2 MB)")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SkillError("Keine gültige Zip-Datei") from exc
        members = [m for m in archive.infolist() if not m.is_dir() and not m.filename.startswith("__MACOSX/")]
        skill_md = [m for m in members if PurePosixPath(m.filename).name == "SKILL.md"]
        if not skill_md:
            raise SkillError("Zip enthält keine SKILL.md")
        prefix = str(PurePosixPath(min(skill_md, key=lambda m: len(m.filename)).filename).parent)
        prefix = "" if prefix == "." else prefix + "/"
        if sum(m.file_size for m in members) > 4 * MAX_ZIP_BYTES:
            raise SkillError("Entpackt zu groß")
        meta_text = archive.read(prefix + "SKILL.md").decode("utf-8")
        match = _FRONT_MATTER.match(meta_text)
        meta = {}
        if match:
            for line in match.group(1).splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    meta[key.strip()] = value.strip()
        name = meta.get("name") or PurePosixPath(prefix).name
        path = self._user_path(name)
        if path.exists():
            raise SkillError(f"Eigener Skill {name} existiert bereits; zuerst löschen")
        for member in members:
            if not member.filename.startswith(prefix):
                continue
            target = self._file_path(path, member.filename[len(prefix):])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
        return name
