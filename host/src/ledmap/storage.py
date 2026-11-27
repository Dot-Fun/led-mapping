"""Project persistence: saves/loads multi-round projects and solved 3D maps as JSON."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ledmap.models import ProjectData


def _safe_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    return clean or "default"


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def project_path(self, name: str) -> Path:
        return self.root / _safe_name(name) / "project.json"

    def list_projects(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for p in sorted(self.root.glob("*/project.json")):
            try:
                raw = json.loads(p.read_text())
                items.append(
                    {
                        "name": raw.get("name", p.parent.name),
                        "rounds": len(raw.get("rounds", [])),
                        "total_leds": len(raw.get("led_table", [])),
                        "measured_count": (raw.get("map_3d") or {}).get("measured_count", 0),
                        "created_at": raw.get("created_at", 0),
                    }
                )
            except Exception:
                continue
        return items

    def save(self, project: ProjectData) -> Path:
        path = self.project_path(project.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(project.model_dump_json(indent=2))
        return path

    def load(self, name: str) -> ProjectData | None:
        path = self.project_path(name)
        if not path.exists():
            return None
        return ProjectData.model_validate_json(path.read_text())

    def get_or_create_default(self) -> ProjectData:
        existing = self.load("default")
        if existing:
            return existing
        proj = ProjectData(name="default", created_at=time.time())
        self.save(proj)
        return proj
