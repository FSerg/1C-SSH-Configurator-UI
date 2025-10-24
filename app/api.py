"""
Minimal FastAPI surface for executing 1C agent operations via HTTP.
"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Iterable, List
from uuid import UUID

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .agent_client import FINAL_MESSAGE_TYPES, AgentClient
from .models import CommandMessage, CommandResult, ProjectModel
from .operations import (
    build_dump_config_sequence,
    build_dump_extensions_sequence,
    build_dump_externals_sequence,
    build_load_config_sequence,
    build_load_extensions_sequence,
    build_load_externals_sequence,
)
from .storage import ProjectStorage

APP_TITLE = "1C Agent API"
MAX_LOG_LINES = 500
TRUNCATION_NOTICE = "[лог усечён до последних 500 строк]"


@dataclass(frozen=True)
class OperationSpec:
    builder: Callable[[ProjectModel], List[str]]
    title: str


app = FastAPI(title=APP_TITLE)
_storage = ProjectStorage()
_global_lock = Lock()

_OPERATIONS = {
    "config_dump": OperationSpec(build_dump_config_sequence, "Выгрузка конфигурации"),
    "config_load": OperationSpec(build_load_config_sequence, "Загрузка конфигурации"),
    "extensions_dump": OperationSpec(build_dump_extensions_sequence, "Выгрузка расширений"),
    "extensions_load": OperationSpec(build_load_extensions_sequence, "Загрузка расширений"),
    "externals_export": OperationSpec(build_dump_externals_sequence, "Экспорт внешних файлов в XML"),
    "externals_build": OperationSpec(build_load_externals_sequence, "Сборка внешних файлов из XML"),
}


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error",
            "logs": [message],
        },
    )


def _format_log_entry(command: str, message: CommandMessage) -> str:
    headline = f"[{message.type}] {command}"
    detail_parts: List[str] = []
    if message.title:
        title_text = message.title.strip()
        if title_text:
            detail_parts.append(title_text)
    if message.message:
        msg_text = message.message.strip()
        if msg_text and msg_text not in detail_parts:
            detail_parts.append(msg_text)
    if message.data:
        try:
            detail_parts.append(json.dumps(message.data, ensure_ascii=False))
        except TypeError:
            detail_parts.append(str(message.data))
    detail = " | ".join(part for part in detail_parts if part)
    return f"{headline} -> {detail}" if detail else headline


def _limit_logs(lines: List[str]) -> List[str]:
    if len(lines) <= MAX_LOG_LINES:
        return lines
    kept = lines[-(MAX_LOG_LINES - 1):]
    return [TRUNCATION_NOTICE, *kept]


def _infer_status_from_results(results: Iterable[CommandResult]) -> str:
    collected = list(results)
    for result in reversed(collected):
        for message in reversed(result.messages):
            if message.type in FINAL_MESSAGE_TYPES:
                return message.type
    if not collected:
        return "success"
    if all(result.success for result in collected):
        return "success"
    return "error"


def _normalize_uid_candidates(value: str) -> List[str]:
    parsed = UUID(value)
    return [parsed.hex, str(parsed)]


def _load_project(project_uid: str) -> ProjectModel | None:
    for candidate in _normalize_uid_candidates(project_uid):
        project = _storage.get_project(candidate)
        if project:
            return project
    return None


def _execute_operation(spec: OperationSpec, project: ProjectModel) -> JSONResponse:
    with _global_lock:
        client = AgentClient(project)
        logs: List[str] = []
        final_status: str | None = None
        http_status = 200
        started = time.perf_counter()

        def progress(command: str, message: CommandMessage) -> None:
            nonlocal final_status
            logs.append(_format_log_entry(command, message))
            if message.type in FINAL_MESSAGE_TYPES:
                final_status = message.type

        try:
            results = client.execute_sequence(
                spec.builder(project),
                timeout_per_command=project.options.command_timeout_seconds,
                progress_cb=progress,
            )
            if final_status is None:
                final_status = _infer_status_from_results(results)
        except Exception as exc:  # noqa: BLE001
            final_status = "error"
            http_status = 500
            logs.append(f"[error] {exc}")
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            elapsed = time.perf_counter() - started
            logs.append(f"[info] {spec.title} завершена за {elapsed:.1f} сек.")

        limited = _limit_logs(logs)
        return JSONResponse(
            status_code=http_status,
            content={
                "status": final_status or "error",
                "logs": limited,
            },
        )


def _handle_request(operation_name: str, project_uid: str | None) -> JSONResponse:
    spec = _OPERATIONS[operation_name]
    if not project_uid:
        return _error_response(400, "[error] Обязательный параметр 'project_uid' не указан.")
    try:
        _storage.reload()
        project = _load_project(project_uid)
    except ValueError:
        return _error_response(400, "[error] Значение 'project_uid' должно быть валидным UUID.")
    if not project:
        return _error_response(404, f"[error] Проект '{project_uid}' не найден.")
    return _execute_operation(spec, project)


@app.get("/api/config/dump")
def dump_config(project_uid: str | None = Query(default=None)) -> JSONResponse:
    return _handle_request("config_dump", project_uid)


@app.get("/api/config/load")
def load_config(project_uid: str | None = Query(default=None)) -> JSONResponse:
    return _handle_request("config_load", project_uid)


@app.get("/api/extensions/dump")
def dump_extensions(project_uid: str | None = Query(default=None)) -> JSONResponse:
    return _handle_request("extensions_dump", project_uid)


@app.get("/api/extensions/load")
def load_extensions(project_uid: str | None = Query(default=None)) -> JSONResponse:
    return _handle_request("extensions_load", project_uid)


@app.get("/api/externals/export-xml")
def export_externals(project_uid: str | None = Query(default=None)) -> JSONResponse:
    return _handle_request("externals_export", project_uid)


@app.get("/api/externals/build-from-xml")
def build_externals(project_uid: str | None = Query(default=None)) -> JSONResponse:
    return _handle_request("externals_build", project_uid)
