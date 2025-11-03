"""
Streamlit UI composition for the 1C agent controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

import json
import time

import streamlit as st
from pydantic import ValidationError

import os
import requests
from .session import get_owner as get_session_owner, acquire_owner as acquire_session_owner, release_owner as release_session_owner
from .models import CommandMessage, ProjectModel, RunOptionsModel
from .operations import (
    build_dump_config_sequence,
    build_dump_extensions_sequence,
    build_dump_externals_sequence,
    build_load_config_sequence,
    build_load_extensions_sequence,
    build_load_externals_sequence,
)
from .storage import ProjectStorage
from .utils import clean_multiline_input, join_multiline


@dataclass
class OperationResult:
    title: str
    success: bool
    messages: List[str]


class StreamlitApp:
    """
    Main orchestrator for the Streamlit application.
    """

    def __init__(self, storage: Optional[ProjectStorage] = None) -> None:
        self.storage = storage or ProjectStorage()
        self._api_base = os.getenv(
            "ONEC_AGENT_API_BASE", "http://127.0.0.1:8000")

    # ---------------------------------------------------------------- rendering
    def run(self) -> None:
        st.set_page_config(page_title="1C Agent UI", layout="wide")
        projects = list(self.storage.list_projects())
        self._ensure_state(projects)
        self._render_sidebar(projects)

        tabs = st.tabs(
            [
                "Проекты",
                "Конфигурация",
                "Расширения",
                "Внешние файлы",
                "Инструменты",
            ]
        )

        with tabs[0]:
            self._render_project_tab(projects)
        with tabs[1]:
            self._maybe_execute_pending_operation("configuration")
            self._render_configuration_tab()
        with tabs[2]:
            self._maybe_execute_pending_operation("extensions")
            self._render_extensions_tab()
        with tabs[3]:
            self._maybe_execute_pending_operation("externals")
            self._render_externals_tab()
        with tabs[4]:
            self._maybe_execute_pending_operation("tools")
            self._render_tools_tab()

    # ------------------------------------------------------------------ sidebar
    def _render_sidebar(self, projects: List[ProjectModel]) -> None:
        st.sidebar.header("Проекты")
        selected_uid = st.session_state.get("selected_project_uid")

        options: List[Optional[ProjectModel]] = [None] + projects
        if len(options) > 1:
            def _label(option: Optional[ProjectModel]) -> str:
                return option.name if option else "[Новый проект]"

            current_index = 0
            if selected_uid:
                for idx, project in enumerate(options[1:], start=1):
                    if project.uid == selected_uid:
                        current_index = idx
                        break

            choice = st.sidebar.selectbox(
                "Выбранный проект",
                options,
                index=current_index,
                format_func=_label,
            )

            if choice is None:
                if selected_uid is not None:
                    st.session_state.selected_project_uid = None
                    st.session_state.project_form_data = self._blank_form()
                    st.rerun()
            elif choice.uid != selected_uid:
                st.session_state.selected_project_uid = choice.uid
                st.session_state.project_form_data = self._project_to_form(
                    choice)
                # Reset last operation summary and clear cached status markers
                st.session_state.last_operation_result = None
                try:
                    # Clear per-project status to force fresh fetch on rerun
                    st.session_state.pop(self._agent_state_key(choice.uid), None)
                    key_conn, key_since = self._db_state_keys(choice.uid)
                    st.session_state.pop(key_conn, None)
                    st.session_state.pop(key_since, None)
                except Exception:
                    pass
                st.rerun()
        else:
            st.sidebar.info("Проекты не созданы.")

        if st.sidebar.button("Создать новый проект"):
            st.session_state.selected_project_uid = None
            st.session_state.project_form_data = self._blank_form()
            st.rerun()

        if selected_uid:
            if st.sidebar.button("Скопировать проект"):
                self._copy_project_to_form(selected_uid)
            if st.sidebar.button("Удалить проект"):
                project = self.storage.get_project(selected_uid)
                display_name = project.name if project else selected_uid
                self.storage.delete(selected_uid)
                st.session_state.selected_project_uid = None
                st.session_state.project_form_data = self._blank_form()
                st.sidebar.success(f"Проект '{display_name}' удален.")
                st.rerun()

        last_op: Optional[OperationResult] = st.session_state.get(
            "last_operation_result")
        if last_op:
            st.sidebar.markdown("---")
            status = "[OK]" if last_op.success else "[ERR]"
            st.sidebar.write(f"{status} {last_op.title}")
            if last_op.messages:
                st.sidebar.write(last_op.messages[-1])

        # DB session status and controls (when enabled for project)
        selected_uid = st.session_state.get("selected_project_uid")
        if selected_uid:
            project = self.storage.get_project(selected_uid)
            if project and getattr(project, "keep_db_connection", False):
                st.sidebar.markdown("---")
                st.sidebar.subheader("Сессия БД")
                # Pull fresh status from API and update local cache
                self._refresh_db_status_from_api(project)
                agent_connected = bool(st.session_state.get(self._agent_state_key(project.uid)))
                connected, since = self._get_db_session_status(project.uid)
                # Agent line
                if agent_connected:
                    st.sidebar.success("Агент: онлайн")
                else:
                    st.sidebar.error("Агент: оффлайн")
                # DB line
                if connected:
                    ts = time.strftime('%Y-%m-%d %H:%M:%S',
                                       time.localtime(since or time.time()))
                    st.sidebar.success(f"База: подключено с {ts}")
                else:
                    st.sidebar.info("База: не подключено")

                if st.sidebar.button("Подключиться к БД", disabled=self._is_operation_running()):
                    self._connect_db_now(project)
                if st.sidebar.button("Отключиться от БД", disabled=self._is_operation_running()):
                    self._disconnect_db_now(project)
                if st.sidebar.button("Обновить статус", disabled=self._is_operation_running()):
                    self._refresh_db_status_from_api(project, force=True)

    # --------------------------------------------------------------- tabs: CRUD
    def _render_project_tab(self, projects: List[ProjectModel]) -> None:
        data = st.session_state.get("project_form_data", self._blank_form())
        flash = st.session_state.pop("project_flash", None)
        if flash:
            level, message = flash
            if level == "success":
                st.success(message)
            else:
                st.error(message)

        st.subheader("Параметры проекта")
        with st.form("project_form"):
            col1, col2 = st.columns(2)
            with col1:
                name_col, uid_col = st.columns((3, 2))
                with name_col:
                    name = st.text_input("Имя проекта", value=data["name"])
                with uid_col:
                    st.text_input(
                        "UID проекта",
                        value=(data.get("uid") or "—"),
                        disabled=True,
                    )
                host = st.text_input("SSH хост", value=data["host"])
                port = st.number_input(
                    "SSH порт", min_value=1, max_value=65535, value=int(data["port"]))
                username = st.text_input(
                    "SSH пользователь", value=data["username"])
                password = st.text_input(
                    "Пароль", value=data["password"], type="password")
                private_key_path = st.text_input(
                    "Путь к приватному ключу (опционально)", value=data["private_key_path"])
                passphrase = st.text_input(
                    "Пароль к ключу (если требуется)", value=data["passphrase"], type="password")
            with col2:
                keepalive = st.number_input(
                    "Интервал keep-alive (сек.)", min_value=0, max_value=600, value=int(data["keepalive_interval"]))
                timeout = st.number_input(
                    "Таймаут подключения (сек.)", min_value=5, max_value=300, value=int(data["timeout"]))
                description = st.text_area(
                    "Описание", value=data["description"], height=110)
                auto_connect = st.checkbox(
                    "Автоподключение при выборе проекта", value=data["auto_connect"])
                keep_db_connection = st.checkbox(
                    "Сохранять подключение к БД", value=data.get("keep_db_connection", False))
                ssh_idle_timeout = st.number_input(
                    "Таймаут бездействия SSH-канала, сек",
                    min_value=60,
                    max_value=86400,
                    value=int(data.get("ssh_idle_timeout_seconds", 3600)),
                )

            st.markdown("### Каталоги")
            col3, col4 = st.columns(2)
            with col3:
                config_dir = st.text_input(
                    "Конфигурация", value=data["config_dir"])
                externals_dir = st.text_input(
                    "Внешние файлы (EPF/ERF)", value=data["externals_dir"])
            with col4:
                extensions_dir = st.text_input(
                    "Расширения", value=data["extensions_dir"])
                externals_xml_dir = st.text_input(
                    "XML для внешних файлов", value=data["externals_xml_dir"])

            st.markdown("### Опции выполнения")
            col5, col6, col7, col8 = st.columns(4)
            with col5:
                use_server = st.checkbox("--server", value=data["use_server"])
                update_flag = st.checkbox("--update", value=data["update"])
            with col6:
                threads = st.number_input(
                    "--threads", min_value=0, max_value=32, value=int(data["threads"]))
                force_flag = st.checkbox("--force", value=data["force"])
            with col7:
                ignore_refs = st.checkbox(
                    "--ignore-unresolved-refs", value=data["ignore_unresolved_refs"])
                no_check = st.checkbox("--no-check", value=data["no_check"])
                update_dump_info = st.checkbox(
                    "--update-config-dump-info", value=data["update_config_dump_info"])
            with col8:
                command_timeout = st.number_input(
                    "Таймаут команды (с)",
                    min_value=60,
                    max_value=3600,
                    value=int(data["command_timeout_seconds"]),
                )

            st.markdown("### Списки объектов")
            extensions_text = st.text_area(
                "Расширения (по одному в строке, пусто = все)",
                value=data["extensions"],
                height=120,
            )
            externals_text = st.text_area(
                "Внешние отчеты/обработки (имена файлов, *.epf/*.erf)",
                value=data["external_objects"],
                height=120,
            )

            saved = st.form_submit_button("Сохранить проект")

        if saved:
            form_payload = {
                "name": name,
                "description": description,
                "credentials": {
                    "host": host,
                    "port": int(port),
                    "username": username,
                    "password": password,
                    "private_key_path": private_key_path or None,
                    "passphrase": passphrase or None,
                    "keepalive_interval": int(keepalive),
                    "timeout": int(timeout),
                },
                "config_dir": config_dir,
                "extensions_dir": extensions_dir,
                "externals_dir": externals_dir,
                "externals_xml_dir": externals_xml_dir,
                "extensions": clean_multiline_input(extensions_text),
                "external_objects": clean_multiline_input(externals_text),
                "options": {
                    "use_server": use_server,
                    "threads": int(threads) or None,
                    "update": update_flag,
                    "force": force_flag,
                    "ignore_unresolved_refs": ignore_refs,
                    "no_check": no_check,
                    "update_config_dump_info": update_dump_info,
                    "command_timeout_seconds": int(command_timeout),
                },
                "auto_connect": auto_connect,
                "keep_db_connection": keep_db_connection,
                "ssh_idle_timeout_seconds": int(ssh_idle_timeout),
            }
            if data.get("uid"):
                form_payload["uid"] = data["uid"]
            try:
                project = ProjectModel(**form_payload)
            except ValidationError as exc:
                st.error(f"Ошибка валидации: {exc}")
                return

            stored = self.storage.upsert(project, previous_uid=data.get("uid"))
            st.session_state.selected_project_uid = stored.uid
            st.session_state.project_form_data = self._project_to_form(stored)
            st.session_state.project_flash = (
                "success", f"Проект '{stored.name}' сохранен.")
            st.rerun()

    # ------------------------------------------------------------ tabs: config
    def _render_configuration_tab(self) -> None:
        project = self._current_project()
        if not project:
            st.info("Выберите или создайте проект, чтобы работать с конфигурацией.")
            return

        st.subheader(f"Конфигурация: {project.name}")
        st.write(f"Каталог конфигурации: `{project.config_dir}`")

        col_dump, col_load = st.columns(2)
        with col_dump:
            if st.button("Выгрузить конфигурацию", disabled=self._is_operation_running()):
                self._queue_api_operation(
                    project,
                    "Выгрузка конфигурации",
                    "/api/config/dump",
                    scope="configuration",
                )
        with col_load:
            if st.button("Загрузить конфигурацию", disabled=self._is_operation_running()):
                self._queue_api_operation(
                    project,
                    "Загрузка конфигурации",
                    "/api/config/load",
                    scope="configuration",
                )

        self._render_log_output()

    # --------------------------------------------------------- tabs: extensions
    def _render_extensions_tab(self) -> None:
        project = self._current_project()
        if not project:
            st.info("Выберите проект, чтобы управлять расширениями.")
            return

        st.subheader(f"Расширения: {project.name}")
        st.write(f"Каталог расширений: `{project.extensions_dir}`")
        st.write("Список: " + (", ".join(project.extensions)
                 if project.extensions else "все расширения"))

        col_dump, col_load = st.columns(2)
        with col_dump:
            if st.button("Выгрузить расширения", disabled=self._is_operation_running()):
                self._queue_api_operation(
                    project,
                    "Выгрузка расширений",
                    "/api/extensions/dump",
                    scope="extensions",
                )
        with col_load:
            if st.button("Загрузить расширения", disabled=self._is_operation_running()):
                self._queue_api_operation(
                    project,
                    "Загрузка расширений",
                    "/api/extensions/load",
                    scope="extensions",
                )

        self._render_log_output()

    # --------------------------------------------------------- tabs: externals
    def _render_externals_tab(self) -> None:
        project = self._current_project()
        if not project:
            st.info("Выберите проект для работы с внешними обработками.")
            return

        st.subheader(f"Внешние файлы: {project.name}")
        st.write(f"Каталог EPF/ERF: `{project.externals_dir}`")
        st.write(f"Каталог XML: `{project.externals_xml_dir}`")

        col_dump, col_load = st.columns(2)
        with col_dump:
            if st.button("Выгрузить внешние файлы в XML", disabled=self._is_operation_running()):
                if not project.external_objects:
                    st.warning(
                        "Список внешних файлов пуст. Укажите имена файлов в проекте.")
                else:
                    self._queue_api_operation(
                        project,
                        "Выгрузка внешних файлов",
                        "/api/externals/export-xml",
                        scope="externals",
                    )
        with col_load:
            if st.button("Собрать EPF/ERF из XML", disabled=self._is_operation_running()):
                if not project.external_objects:
                    st.warning(
                        "Список внешних файлов пуст. Укажите имена файлов в проекте.")
                else:
                    self._queue_api_operation(
                        project,
                        "Загрузка внешних файлов",
                        "/api/externals/build-from-xml",
                        scope="externals",
                    )

        self._render_log_output()

    # -------------------------------------------------------------- tabs: tools
    def _render_tools_tab(self) -> None:
        project = self._current_project()
        if not project:
            st.info("Выберите проект для запуска инструментов.")
            return

        st.subheader("Диагностика и инструменты")
        if st.button("Проверка подключения (help --version)", disabled=self._is_operation_running()):
            self._queue_api_operation(
                project,
                "Проверка подключения",
                "/api/tools/version",
                scope="tools",
            )

        st.markdown("---")
        dump_path = st.text_input(
            "Путь для dump-ib", value="../backups/dump.dt")
        restore_path = st.text_input(
            "Путь для restore-ib", value="../backups/dump.dt")
        col_dump, col_restore = st.columns(2)
        with col_dump:
            if st.button("dump-ib", disabled=self._is_operation_running()):
                self._queue_api_operation(
                    project,
                    "dump-ib",
                    "/api/infobase/dump",
                    params={"file": dump_path},
                    scope="tools",
                )
        with col_restore:
            if st.button("restore-ib", disabled=self._is_operation_running()):
                self._queue_api_operation(
                    project,
                    "restore-ib",
                    "/api/infobase/restore",
                    params={"file": restore_path},
                    scope="tools",
                )

        st.markdown("---")
        if st.button("Запустить update-db-cfg", disabled=self._is_operation_running()):
            self._queue_api_operation(
                project,
                "update-db-cfg",
                "/api/tools/update-db-cfg",
                scope="tools",
            )

        self._render_log_output()

    # ---------------------------------------------------------------- operations
    def _maybe_execute_pending_operation(self, scope: str | None) -> None:
        pending = st.session_state.get("pending_operation")
        if not pending:
            return

        if scope and pending.get("scope") not in (None, scope):
            return

        project_uid = pending.get("project_uid")
        if not project_uid:
            self._set_operation_running(False)
            st.session_state.pending_operation = None
            return

        project = self.storage.get_project(project_uid)
        if not project:
            st.warning("Ранее выбранный проект не найден. Повторите операцию.")
            self._set_operation_running(False)
            st.session_state.pending_operation = None
            return

        st.session_state.pending_operation = None

        api_path = pending.get("api_path")
        if api_path:
            self._run_api_operation(
                project,
                pending.get("title", "Операция"),
                api_path,
                pending.get("params", {}) or {},
                queued=True,
            )
        else:
            # Legacy no-op
            self._set_operation_running(False)

    def _run_api_operation(
        self,
        project: ProjectModel,
        title: str,
        api_path: str,
        params: dict,
        queued: bool = False,
    ) -> None:
        if self._is_operation_running() and not queued:
            st.warning("Дождитесь завершения текущей операции.")
            return

        self._set_operation_running(True)
        log_placeholder = st.empty()
        log_lines: List[str] = []
        started_at = time.time()

        success = True
        try:
            url = f"{self._api_base}{api_path}"
            effective_params = dict(params or {})
            effective_params["project_uid"] = project.uid
            resp = requests.get(url, params=effective_params,
                                timeout=project.options.command_timeout_seconds + 10)
            try:
                data = resp.json()
            except Exception:
                data = {"status": "error", "logs": [
                    f"[error] HTTP {resp.status_code}"]}
            logs = [str(x) for x in (data.get("logs") or [])]
            log_lines.extend(logs)
            success = (str(data.get("status")) == "success") and (
                resp.status_code == 200)
            log_placeholder.code("\n".join(log_lines))
        except Exception as exc:  # noqa: BLE001
            success = False
            log_lines.append(f"[error] {exc}")
            log_placeholder.code("\n".join(log_lines))
        finally:
            self._set_operation_running(False)

        log_placeholder.empty()

        duration = self._format_duration(time.time() - started_at)
        log_lines.append(f"[info] {title} -> завершено за {duration}")

        st.session_state.last_operation_result = OperationResult(
            title=title,
            success=success,
            messages=list(log_lines),
        )

        if queued:
            st.rerun()
        else:
            if success:
                st.success(f"{title}: успешно")
            else:
                st.error(f"{title}: ошибка, см. лог ниже")
        # After any API operation, refresh DB session status from API
        try:
            self._refresh_db_status_from_api(project, force=True)
        except Exception:
            pass

    def _render_log_output(self, fallback_lines: Optional[List[str]] = None) -> None:
        result: Optional[OperationResult] = st.session_state.get(
            "last_operation_result")
        log = fallback_lines or (
            [line for line in (result.messages if result else []) if line])

        if result and result.title:
            message = f"{result.title}: {'успешно' if result.success else 'ошибка, см. лог ниже'}"
            if result.success:
                st.success(message)
            else:
                st.error(message)

        if log:
            st.markdown("#### Лог операции")
            st.code("\n".join(log))

    def _format_log_entry(self, command: str, message: CommandMessage) -> str:
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
                detail_parts.append(json.dumps(
                    message.data, ensure_ascii=False))
            except TypeError:
                detail_parts.append(str(message.data))
        detail = " | ".join(part for part in detail_parts if part)
        return f"{headline} -> {detail}" if detail else headline

    def _is_operation_running(self) -> bool:
        return bool(st.session_state.get("operation_in_progress"))

    def _set_operation_running(self, value: bool) -> None:
        st.session_state.operation_in_progress = value

    def _queue_operation(
        self,
        project: ProjectModel,
        title: str,
        commands: Iterable[str],
        connect_before: bool = True,
        scope: str | None = None,
        command_timeout: Optional[int] = None,
    ) -> None:
        # Legacy no-op to keep compatibility with old calls
        self._set_operation_running(True)
        st.rerun()

    def _queue_api_operation(
        self,
        project: ProjectModel,
        title: str,
        api_path: str,
        params: Optional[dict] = None,
        scope: str | None = None,
    ) -> None:
        st.session_state.pending_operation = {
            "project_uid": project.uid,
            "title": title,
            "api_path": api_path,
            "params": params or {},
            "scope": scope,
        }
        self._set_operation_running(True)
        st.rerun()

    def _format_duration(self, seconds: float) -> str:
        millis = int((seconds - int(seconds)) * 1000)
        total_seconds = int(seconds)
        minutes, sec = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)

        parts = []
        if hours:
            parts.append(f"{hours} ч")
        if minutes:
            parts.append(f"{minutes} мин")
        if sec or not parts:
            parts.append(f"{sec} с")
        if millis:
            parts.append(f"{millis} мс")

        return " ".join(parts)

    # ------------------------------------------------------------------ helpers
    def _ensure_state(self, projects: List[ProjectModel]) -> None:
        if "selected_project_uid" not in st.session_state:
            st.session_state.selected_project_uid = projects[0].uid if projects else None
        else:
            if st.session_state.selected_project_uid and not any(
                project.uid == st.session_state.selected_project_uid for project in projects
            ):
                st.session_state.selected_project_uid = projects[0].uid if projects else None
        if "project_form_data" not in st.session_state:
            selected_uid = st.session_state.get("selected_project_uid")
            if selected_uid:
                project = self.storage.get_project(selected_uid)
                st.session_state.project_form_data = self._project_to_form(
                    project)
            else:
                st.session_state.project_form_data = self._blank_form()
        if "operation_in_progress" not in st.session_state:
            st.session_state.operation_in_progress = False
        if "pending_operation" not in st.session_state:
            st.session_state.pending_operation = None
        if "project_flash" not in st.session_state:
            st.session_state.project_flash = None

    def _blank_form(self) -> dict:
        return {
            "uid": None,
            "name": "",
            "description": "",
            "host": "",
            "port": 22,
            "username": "",
            "password": "",
            "private_key_path": "",
            "passphrase": "",
            "keepalive_interval": 30,
            "timeout": 30,
            "auto_connect": False,
            "keep_db_connection": False,
            "ssh_idle_timeout_seconds": 3600,
            "config_dir": "../Configuration",
            "extensions_dir": "../Extensions",
            "externals_dir": "../External",
            "externals_xml_dir": "../ExternalXML",
            "use_server": False,
            "threads": 0,
            "update": False,
            "force": False,
            "ignore_unresolved_refs": False,
            "no_check": False,
            "update_config_dump_info": False,
            "command_timeout_seconds": 600,
            "extensions": "",
            "external_objects": "",
        }

    def _project_to_form(self, project: Optional[ProjectModel]) -> dict:
        if not project:
            return self._blank_form()
        return {
            "uid": project.uid,
            "name": project.name,
            "description": project.description or "",
            "host": project.credentials.host,
            "port": project.credentials.port,
            "username": project.credentials.username,
            "password": project.credentials.password or "",
            "private_key_path": project.credentials.private_key_path or "",
            "passphrase": project.credentials.passphrase or "",
            "keepalive_interval": project.credentials.keepalive_interval,
            "timeout": project.credentials.timeout,
            "auto_connect": project.auto_connect,
            "keep_db_connection": getattr(project, "keep_db_connection", False),
            "ssh_idle_timeout_seconds": getattr(project, "ssh_idle_timeout_seconds", 3600),
            "config_dir": project.config_dir,
            "extensions_dir": project.extensions_dir,
            "externals_dir": project.externals_dir,
            "externals_xml_dir": project.externals_xml_dir,
            "use_server": project.options.use_server,
            "threads": project.options.threads or 0,
            "update": project.options.update,
            "force": project.options.force,
            "ignore_unresolved_refs": project.options.ignore_unresolved_refs,
            "no_check": project.options.no_check,
            "update_config_dump_info": project.options.update_config_dump_info,
            "command_timeout_seconds": project.options.command_timeout_seconds,
            "extensions": join_multiline(project.extensions),
            "external_objects": join_multiline(project.external_objects),
        }

    def _copy_project_to_form(self, uid: str) -> None:
        """Скопировать выбранный проект в форму для создания нового проекта."""
        project = self.storage.get_project(uid)
        if not project:
            st.sidebar.error("Проект не найден.")
            return

        # Создаём копию данных формы
        form_data = self._project_to_form(project)

        # Обнуляем uid для создания нового проекта
        form_data["uid"] = None

        # Добавляем суффикс " (копия)" к имени
        form_data["name"] = f"{project.name} (копия)"

        # Переключаемся в режим создания нового проекта
        st.session_state.selected_project_uid = None
        st.session_state.project_form_data = form_data
        st.rerun()

    def _current_project(self) -> Optional[ProjectModel]:
        uid = st.session_state.get("selected_project_uid")
        if not uid:
            return None
        return self.storage.get_project(uid)

    # ------------------------------ DB session helpers (UI-scoped state)
    def _db_state_keys(self, uid: str) -> tuple[str, str]:
        return (f"db_connected__{uid}", f"db_connected_since__{uid}")

    def _agent_state_key(self, uid: str) -> str:
        return f"agent_connected__{uid}"

    def _get_db_session_status(self, uid: str) -> tuple[bool, Optional[float]]:
        key_conn, key_since = self._db_state_keys(uid)
        return bool(st.session_state.get(key_conn)), st.session_state.get(key_since)

    def _set_db_session_status(self, uid: str, connected: bool) -> None:
        key_conn, key_since = self._db_state_keys(uid)
        st.session_state[key_conn] = connected
        st.session_state[key_since] = time.time() if connected else None

    def _set_agent_status(self, uid: str, connected: bool) -> None:
        st.session_state[self._agent_state_key(uid)] = connected

    def _refresh_db_status_from_api(self, project: ProjectModel, force: bool = False) -> None:
        if self._is_operation_running() and not force:
            return
        try:
            url = f"{self._api_base}/api/common/status"
            resp = requests.get(url, params={"project_uid": project.uid}, timeout=5)
            data = resp.json() if resp.ok else {"agent_connected": None, "db_connected": None}
            agent_connected = data.get("agent_connected")
            db_connected = data.get("db_connected")
            if isinstance(agent_connected, bool):
                self._set_agent_status(project.uid, agent_connected)
            if db_connected is True:
                self._set_db_session_status(project.uid, True)
            elif db_connected is False:
                self._set_db_session_status(project.uid, False)
            # if None (unknown) -> leave as-is
        except Exception:
            # Network or parse error: keep current UI value
            pass

    def _connect_db_now(self, project: ProjectModel) -> None:
        self._set_operation_running(True)
        try:
            url = f"{self._api_base}/api/common/connect"
            resp = requests.get(url, params={
                                "project_uid": project.uid}, timeout=project.options.command_timeout_seconds + 10)
            ok = resp.status_code == 200 and (
                resp.json().get("status") == "success")
            if ok:
                self._set_db_session_status(project.uid, True)
                st.sidebar.success("Подключение выполнено")
            else:
                st.sidebar.error("Не удалось подключиться")
        except Exception as exc:  # noqa: BLE001
            st.sidebar.error(f"Ошибка подключения: {exc}")
        finally:
            self._set_operation_running(False)

    def _disconnect_db_now(self, project: ProjectModel) -> None:
        self._set_operation_running(True)
        try:
            url = f"{self._api_base}/api/common/disconnect"
            resp = requests.get(url, params={
                                "project_uid": project.uid}, timeout=project.options.command_timeout_seconds + 10)
            ok = resp.status_code == 200 and (
                resp.json().get("status") == "success")
            if ok:
                self._set_db_session_status(project.uid, False)
                st.sidebar.success("Отключено")
            else:
                st.sidebar.error("Не удалось отключиться")
        except Exception as exc:  # noqa: BLE001
            st.sidebar.error(f"Ошибка отключения: {exc}")
        finally:
            self._set_operation_running(False)


def run_app() -> None:
    StreamlitApp().run()
