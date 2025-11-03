"""
Minimal FastAPI surface for executing 1C agent operations via HTTP.
"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass
import os
import logging
from threading import Lock
from typing import Callable, Iterable, List
from uuid import UUID

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .agent_client import FINAL_MESSAGE_TYPES, AgentClient, get_agent_client, release_agent_client
from .session import get_owner as get_session_owner, acquire_owner as acquire_session_owner, release_owner as release_session_owner
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
_API_DEBUG = os.getenv("ONEC_AGENT_API_DEBUG_TRACE", "").lower() in {"1", "true", "yes"}
_logger = logging.getLogger("uvicorn.error")


def _trace_api(event: str, payload: dict | None = None) -> None:
    if not _API_DEBUG:
        return
    try:
        _logger.info("[api] %s | %s", event, payload or {})
    except Exception:
        pass


@app.middleware("http")
async def _log_requests(request, call_next):
    if _API_DEBUG:
        try:
            _logger.info("[api] -> %s %s", request.method, str(request.url))
        except Exception:
            pass
    response = await call_next(request)
    if _API_DEBUG:
        try:
            _logger.info("[api] <- %s %s %s", request.method, str(request.url), response.status_code)
        except Exception:
            pass
    return response

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
        persistent = bool(getattr(project, "keep_db_connection", False))
        if persistent:
            owner = get_session_owner(project.uid)
            # Allow persistent mode only if no owner or API already owns it
            if owner and owner.owner_id != "api":
                persistent = False
        # If another process owns the persistent session, block IB-required operations
        owner = get_session_owner(project.uid)
        if owner and owner.owner_id != "api":
            return _error_response(
                423,
                "[error] Сессия БД занята другим процессом (UI). Выполните операцию из UI или отключите сессию.",
            )
        _trace_api("begin", {"op": spec.title, "persistent": persistent, "project": project.uid})
        client = get_agent_client(project, persistent=persistent)
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
            commands = list(spec.builder(project))
            _trace_api("built_commands", {"count": len(commands)})
            if persistent:
                # Strip explicit connect/disconnect; ensure connection explicitly
                if commands and commands[0] == "common connect-ib":
                    commands = commands[1:]
                if commands and commands[-1] == "common disconnect-ib":
                    commands = commands[:-1]
                try:
                    _trace_api("ensure_connect", None)
                    # Acquire ownership for API to avoid cross-process contention
                    acquired = acquire_session_owner(project.uid, "api", force=False)
                    if not acquired:
                        persistent = False
                    else:
                        client.ensure_db_connected(progress, assume_disconnected=False)
                except Exception as exc:  # noqa: BLE001
                    logs.append(f"[error] ensure-connect failed: {exc}")
                    final_status = "error"
            _trace_api("exec_sequence_start", {"count": len(commands)})
            results = client.execute_sequence(
                commands,
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
            if not persistent:
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            elapsed = time.perf_counter() - started
            _trace_api("end", {"status": final_status, "elapsed": elapsed})
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


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(status_code=200, content={"status": "ok"})


# ----------------------------- direct endpoints for UI integrations

def _json_logs_response(title: str, logs: List[str], status: str, http_status: int = 200) -> JSONResponse:
    limited = _limit_logs(logs)
    return JSONResponse(status_code=http_status, content={"status": status, "logs": limited, "title": title})


def _client_progress_logger(logs: List[str]):
    def _progress(command: str, message: CommandMessage) -> None:
        logs.append(_format_log_entry(command, message))
    return _progress


def _get_project_or_error(project_uid: str | None) -> ProjectModel | JSONResponse:
    if not project_uid:
        return _error_response(400, "[error] Обязательный параметр 'project_uid' не указан.")
    try:
        _storage.reload()
        project = _load_project(project_uid)
    except ValueError:
        return _error_response(400, "[error] Значение 'project_uid' должно быть валидным UUID.")
    if not project:
        return _error_response(404, f"[error] Проект '{project_uid}' не найден.")
    return project


@app.get("/api/common/connect")
def api_connect(project_uid: str | None = Query(default=None)) -> JSONResponse:
    project = _get_project_or_error(project_uid)
    if isinstance(project, JSONResponse):
        return project
    logs: List[str] = []
    client = get_agent_client(project, persistent=True)
    try:
        progress = _client_progress_logger(logs)
        # ensure SSH + JSON session initialized
        client.ensure_connected(progress)
        # ensure DB connected
        client.ensure_db_connected(progress, assume_disconnected=False)
        logs.append("[info] Подключение выполнено")
        return _json_logs_response("connect-ib", logs, "success")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"[error] {exc}")
        return _json_logs_response("connect-ib", logs, "error", 500)


@app.get("/api/common/disconnect")
def api_disconnect(project_uid: str | None = Query(default=None)) -> JSONResponse:
    project = _get_project_or_error(project_uid)
    if isinstance(project, JSONResponse):
        return project
    logs: List[str] = []
    client = get_agent_client(project, persistent=True)
    try:
        progress = _client_progress_logger(logs)
        client.disconnect_ib(progress)
        release_agent_client(project.uid)
        logs.append("[info] Отключение выполнено")
        return _json_logs_response("disconnect-ib", logs, "success")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"[error] {exc}")
        return _json_logs_response("disconnect-ib", logs, "error", 500)


@app.get("/api/tools/version")
def api_version(project_uid: str | None = Query(default=None)) -> JSONResponse:
    project = _get_project_or_error(project_uid)
    if isinstance(project, JSONResponse):
        return project
    logs: List[str] = []
    client = get_agent_client(project, persistent=True)
    try:
        progress = _client_progress_logger(logs)
        client.ensure_connected(progress)
        results = client.execute_sequence(["help --version"], timeout_per_command=project.options.command_timeout_seconds, progress_cb=progress)
        status = _infer_status_from_results(results)
        return _json_logs_response("help --version", logs, status)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"[error] {exc}")
        return _json_logs_response("help --version", logs, "error", 500)


@app.get("/api/infobase/dump")
def api_dump_ib(project_uid: str | None = Query(default=None), file: str | None = Query(default=None)) -> JSONResponse:
    project = _get_project_or_error(project_uid)
    if isinstance(project, JSONResponse):
        return project
    if not file:
        return _error_response(400, "[error] Обязательный параметр 'file' не указан.")
    logs: List[str] = []
    client = get_agent_client(project, persistent=True)
    try:
        progress = _client_progress_logger(logs)
        commands = [
            "common connect-ib",
            f'infobase-tools dump-ib --file="{file}"',
            "common disconnect-ib",
        ]
        results = client.execute_sequence(commands, timeout_per_command=project.options.command_timeout_seconds, progress_cb=progress)
        status = _infer_status_from_results(results)
        return _json_logs_response("dump-ib", logs, status)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"[error] {exc}")
        return _json_logs_response("dump-ib", logs, "error", 500)


@app.get("/api/infobase/restore")
def api_restore_ib(project_uid: str | None = Query(default=None), file: str | None = Query(default=None)) -> JSONResponse:
    project = _get_project_or_error(project_uid)
    if isinstance(project, JSONResponse):
        return project
    if not file:
        return _error_response(400, "[error] Обязательный параметр 'file' не указан.")
    logs: List[str] = []
    client = get_agent_client(project, persistent=True)
    try:
        progress = _client_progress_logger(logs)
        commands = [
            "common connect-ib",
            f'infobase-tools restore-ib --file="{file}"',
            "common disconnect-ib",
        ]
        results = client.execute_sequence(commands, timeout_per_command=project.options.command_timeout_seconds, progress_cb=progress)
        status = _infer_status_from_results(results)
        return _json_logs_response("restore-ib", logs, status)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"[error] {exc}")
        return _json_logs_response("restore-ib", logs, "error", 500)


@app.get("/api/tools/update-db-cfg")
def api_update_db_cfg(project_uid: str | None = Query(default=None)) -> JSONResponse:
    project = _get_project_or_error(project_uid)
    if isinstance(project, JSONResponse):
        return project
    logs: List[str] = []
    client = get_agent_client(project, persistent=True)
    try:
        progress = _client_progress_logger(logs)
        commands = [
            "common connect-ib",
            "config update-db-cfg",
            "common disconnect-ib",
        ]
        results = client.execute_sequence(commands, timeout_per_command=project.options.command_timeout_seconds, progress_cb=progress)
        status = _infer_status_from_results(results)
        return _json_logs_response("update-db-cfg", logs, status)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"[error] {exc}")
        return _json_logs_response("update-db-cfg", logs, "error", 500)
