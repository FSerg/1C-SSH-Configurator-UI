"""
Cross-process session ownership helpers to coordinate persistent DB connections.

When "keep_db_connection" is enabled, UI or API can become the owner of a
project session. Other processes should avoid persistent mode to prevent
contention and hangs; they can still run operations in non-persistent mode.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

RUNTIME_DIR = Path.home() / ".1c-agent-ui" / ".runtime"
OWNER_FILE_FMT = "owner-{uid}.json"
DEFAULT_TTL_SECONDS = 2 * 60 * 60  # 2 hours


@dataclass
class SessionOwner:
    uid: str
    owner_id: str  # e.g. "ui" or "api"
    pid: int
    since: float

    def to_dict(self) -> dict:
        return {"uid": self.uid, "owner_id": self.owner_id, "pid": self.pid, "since": self.since}


def _owner_path(uid: str) -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR / OWNER_FILE_FMT.format(uid=uid)


def get_owner(uid: str) -> Optional[SessionOwner]:
    path = _owner_path(uid)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return SessionOwner(
            uid=str(raw.get("uid") or uid),
            owner_id=str(raw.get("owner_id") or ""),
            pid=int(raw.get("pid") or 0),
            since=float(raw.get("since") or 0.0),
        )
    except Exception:
        return None


def _is_stale(owner: SessionOwner, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    try:
        if owner.pid and owner.pid > 0:
            # Best-effort liveness check (POSIX). On Windows this always returns False.
            if os.name == "posix":
                os.kill(owner.pid, 0)  # may raise OSError if process is gone
        # Time-based staleness
        return (time.time() - owner.since) > ttl_seconds
    except Exception:
        return True


def acquire_owner(uid: str, owner_id: str, *, force: bool = False) -> bool:
    """Try to acquire ownership for a project session.

    Returns True on success. If another owner exists, returns False unless
    force=True and the existing owner is considered stale.
    """
    path = _owner_path(uid)
    existing = get_owner(uid)
    if existing and not force and not _is_stale(existing):
        # Someone active owns the session
        if existing.owner_id == owner_id:
            # Refresh timestamp
            path.write_text(json.dumps(existing.to_dict()), encoding="utf-8")
            return True
        return False

    record = SessionOwner(uid=uid, owner_id=owner_id, pid=os.getpid(), since=time.time())
    try:
        path.write_text(json.dumps(record.to_dict()), encoding="utf-8")
        return True
    except Exception:
        return False


def release_owner(uid: str, owner_id: str) -> None:
    path = _owner_path(uid)
    existing = get_owner(uid)
    try:
        if existing and existing.owner_id == owner_id and path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass

