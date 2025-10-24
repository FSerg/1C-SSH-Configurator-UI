"""
Pydantic models that define configuration and runtime entities.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, validator

from .utils import normalize_relative_agent_path


class CredentialsModel(BaseModel):
    host: str = Field(..., description="SSH host where the 1C agent is running.")
    port: int = Field(22, ge=1, le=65535, description="SSH port of the agent host.")
    username: str = Field(..., min_length=1, description="SSH username.")
    password: Optional[str] = Field(None, description="SSH password (stored obfuscated).")
    private_key_path: Optional[str] = Field(None, description="Path to private key (optional).")
    passphrase: Optional[str] = Field(None, description="Passphrase for private key if required.")
    keepalive_interval: int = Field(30, ge=0, le=600, description="Keep-alive interval in seconds.")
    timeout: int = Field(30, ge=5, le=300, description="SSH connection timeout in seconds.")
    allow_agent: bool = Field(False, description="Allow usage of SSH agent.")
    look_for_keys: bool = Field(False, description="Search for keys in default locations.")

    @validator("host", "username", allow_reuse=True)
    def _strip_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value must not be empty.")
        return value


class RunOptionsModel(BaseModel):
    use_server: bool = Field(False, description="Enable --server flag for config operations.")
    threads: Optional[int] = Field(None, ge=1, le=32, description="Number of threads to pass with --threads.")
    update: bool = Field(False, description="Use --update flag on dumps.")
    force: bool = Field(False, description="Use --force flag on dumps.")
    all_extensions: bool = Field(True, description="Process all extensions when list is empty.")
    ignore_unresolved_refs: bool = Field(False, description="Use --ignore-unresolved-refs flag.")
    no_check: bool = Field(False, description="Use --no-check flag for loads.")
    update_config_dump_info: bool = Field(False, description="Use --update-config-dump-info on loads.")
    command_timeout_seconds: int = Field(
        600,
        ge=60,
        le=3600,
        description="Timeout per command when waiting for agent responses (seconds).",
    )


class ProjectModel(BaseModel):
    uid: str = Field(default_factory=lambda: uuid4().hex, description="Stable project identifier.")
    name: str = Field(..., min_length=1, description="Display name of the project.")
    description: Optional[str] = Field(None, description="Optional notes for the project.")
    credentials: CredentialsModel
    config_dir: str = Field(..., description="Relative directory for the main configuration.")
    extensions_dir: str = Field(..., description="Relative directory for extensions.")
    externals_dir: str = Field(..., description="Relative directory for external reports/processors.")
    externals_xml_dir: str = Field(..., description="Relative directory for XML representations of externals.")
    extensions: List[str] = Field(default_factory=list, description="List of extension names to process.")
    external_objects: List[str] = Field(default_factory=list, description="List of external reports/processors.")
    options: RunOptionsModel = Field(default_factory=RunOptionsModel)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    auto_connect: bool = Field(False, description="Automatically connect on project selection.")

    @validator(
        "config_dir",
        "extensions_dir",
        "externals_dir",
        "externals_xml_dir",
        pre=True,
        allow_reuse=True,
    )
    def _validate_relative_paths(cls, value: str) -> str:
        return normalize_relative_agent_path(value)

    @validator("extensions", "external_objects", pre=True, allow_reuse=True)
    def _strip_lists(cls, values):
        if not values:
            return []
        cleaned = []
        for value in values:
            value = value.strip()
            if value and value not in cleaned:
                cleaned.append(value)
        return cleaned

    @validator("created_at", "updated_at", pre=True, always=True, allow_reuse=True)
    def _ensure_datetime(cls, value):
        return value or datetime.now(UTC)

    def touch(self) -> "ProjectModel":
        """
        Return a copy of the project with an updated timestamp.
        """
        updated = self.copy()
        updated.updated_at = datetime.now(UTC)
        return updated


class CommandMessage(BaseModel):
    type: str
    message: Optional[str] = None
    title: Optional[str] = None
    data: Optional[dict] = None


class CommandResult(BaseModel):
    command: str
    success: bool
    messages: List[CommandMessage] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def mark_finished(self) -> None:
        self.finished_at = datetime.now(UTC)
