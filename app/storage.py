"""
Persistent storage for projects and credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Dict, Iterable, Optional
from uuid import uuid4

from .models import ProjectModel
from .utils import obfuscate_secret, reveal_secret

DEFAULT_STORAGE_PATH = Path.home() / ".1c-agent-ui" / "projects.json"


class ProjectStorage:
    """
    Store project definitions in a local JSON file with lightweight
    obfuscation for sensitive fields.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_STORAGE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._projects: Dict[str, ProjectModel] = {}
        self._load()

    # Public API -----------------------------------------------------------------
    def list_projects(self) -> Iterable[ProjectModel]:
        with self._lock:
            return list(sorted(self._projects.values(), key=lambda p: p.name.lower()))

    def get_project(self, uid: str) -> Optional[ProjectModel]:
        with self._lock:
            project = self._projects.get(uid)
            return project.copy(deep=True) if project else None

    def find_by_name(self, name: str) -> Optional[ProjectModel]:
        with self._lock:
            for project in self._projects.values():
                if project.name == name:
                    return project.copy(deep=True)
        return None

    def upsert(self, project: ProjectModel, previous_uid: str | None = None) -> ProjectModel:
        with self._lock:
            materialized = project.touch()
            target_uid = materialized.uid
            if previous_uid and previous_uid != target_uid:
                self._projects.pop(previous_uid, None)
            # Ensure name uniqueness by removing other entries with same name
            for uid, existing in list(self._projects.items()):
                if uid != target_uid and existing.name == materialized.name:
                    self._projects.pop(uid, None)
            self._projects[target_uid] = materialized
            self._save()
            return materialized.copy(deep=True)

    def delete(self, uid: str) -> None:
        with self._lock:
            if uid in self._projects:
                del self._projects[uid]
                self._save()

    def rename(self, uid: str, new_name: str) -> ProjectModel:
        if not new_name.strip():
            raise ValueError("New project name must not be empty.")
        with self._lock:
            project = self._projects.get(uid)
            if not project:
                raise KeyError(f"Project with id '{uid}' was not found.")
            renamed = project.copy(update={"name": new_name}).touch()
            self._projects[uid] = renamed
            self._save()
            return renamed.copy(deep=True)

    # Internal helpers -----------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # If file is corrupted we keep an empty storage but do not overwrite.
            return

        projects = raw if isinstance(raw, list) else raw.get("projects", [])
        for item in projects:
            if not item.get("uid"):
                item["uid"] = uuid4().hex
            credentials = item.get("credentials", {})
            for field in ("password", "passphrase"):
                if credentials.get(field):
                    try:
                        credentials[field] = reveal_secret(credentials[field])
                    except Exception:
                        # If deobfuscation fails we leave the value empty.
                        credentials[field] = None
            try:
                project = ProjectModel(**item)
            except Exception:
                continue
            self._projects[project.uid] = project

    def _save(self) -> None:
        serialized = []
        for project in self._projects.values():
            if hasattr(project, "model_dump"):
                data = project.model_dump(mode="json")
            else:
                data = json.loads(project.json())
            credentials = data.get("credentials", {})
            for field in ("password", "passphrase"):
                if credentials.get(field):
                    credentials[field] = obfuscate_secret(credentials[field])
            serialized.append(data)

        payload = {"projects": serialized}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
