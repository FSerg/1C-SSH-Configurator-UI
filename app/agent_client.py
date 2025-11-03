"""
Agent client abstraction that communicates with the 1C configurator agent.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

import paramiko

from .models import CommandMessage, CommandResult, ProjectModel
from .ssh import SSHConnectionError, SSHConnectionManager

FINAL_MESSAGE_TYPES = {"success", "error", "cancel"}
ProgressCallback = Callable[[str, CommandMessage], None]


@dataclass
class AgentSessionInfo:
    banner: str = ""
    initialized: bool = False


class _JsonArrayParser:
    """
    Streaming parser that extracts top-level JSON arrays from chunks coming
    from the agent.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._depth = 0
        self._in_string = False
        self._escape = False

    def feed(self, chunk: str) -> Iterable[str]:
        if not chunk:
            return []
        results = []
        for ch in chunk:
            if ch in ("\r", "\n") and not self._buffer:
                continue

            self._buffer += ch

            if self._in_string:
                if self._escape:
                    self._escape = False
                    continue
                if ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self._in_string = False
                continue

            if ch == '"':
                self._in_string = True
                continue
            if ch == "[":
                self._depth += 1
                continue
            if ch == "]":
                self._depth -= 1
                if self._depth == 0:
                    results.append(self._buffer.strip())
                    self._buffer = ""
                continue

        return results

    def reset(self) -> None:
        self._buffer = ""
        self._depth = 0
        self._in_string = False
        self._escape = False


class AgentClient:
    """
    High-level API to execute commands against the 1C configurator agent.
    """

    def __init__(self, project: ProjectModel) -> None:
        self.project = project
        # Reuse SSH channel with optional idle timeout from project settings
        self._ssh = SSHConnectionManager(
            project.credentials,
            idle_timeout_seconds=getattr(
                project, "ssh_idle_timeout_seconds", None),
        )
        self._session = AgentSessionInfo()
        self._parser = _JsonArrayParser()

    # Session management ---------------------------------------------------------
    def ensure_connected(self, progress_cb: Optional[ProgressCallback] = None) -> None:
        t0 = time.perf_counter()
        prev = self._ssh.channel
        channel = self._ssh.ensure_channel()
        t1 = time.perf_counter()
        elapsed_ms = int((t1 - t0) * 1000)
        # Only trace when channel was created/refreshed or took noticeable time
        if prev is None or getattr(prev, "closed", True) or (prev is not channel) or elapsed_ms > 5:
            self._trace(progress_cb, "SSH channel ready",
                        {"elapsed_ms": elapsed_ms, "ssh": self._ssh.debug_info()})
        # If channel was recreated, reset session flags so that we reinitialize
        if prev is not channel:
            self._session.banner = ""
            self._session.initialized = False

        if not self._session.banner:
            t2 = time.perf_counter()
            self._session.banner = self._drain_banner(channel)
            t3 = time.perf_counter()
            self._trace(progress_cb, "Drained banner", {
                "elapsed_ms": int((t3 - t2) * 1000),
                "size": len(self._session.banner or ""),
            })
        if not self._session.initialized:
            t4 = time.perf_counter()
            self._initialize_session(channel, progress_cb)
            t5 = time.perf_counter()
            self._trace(progress_cb, "Initialized agent session",
                        {"elapsed_ms": int((t5 - t4) * 1000), "ssh": self._ssh.debug_info()})

    def disconnect(self) -> None:
        self._ssh.close()
        self._session = AgentSessionInfo()
        self._parser.reset()

    def is_usable(self) -> bool:
        try:
            client = self._ssh.client
            if not client:
                return False
            transport = client.get_transport()
            return bool(transport and transport.is_active())
        except Exception:
            return False

    def connect_ib(self, progress_cb: Optional[ProgressCallback] = None) -> CommandResult:
        self._trace(progress_cb, "Connect IB requested", {"ssh": self._ssh.debug_info()})
        return self.execute("common connect-ib", timeout=60.0, progress_cb=progress_cb)

    def disconnect_ib(self, progress_cb: Optional[ProgressCallback] = None) -> CommandResult:
        self._trace(progress_cb, "Disconnect IB requested", {"ssh": self._ssh.debug_info()})
        return self.execute("common disconnect-ib", timeout=30.0, progress_cb=progress_cb)

    def check_agent_connected(self) -> Optional[bool]:
        """
        Check if the SSH channel + agent session is responsive.
        Does NOT reflect DB (infobase) connectivity.
        """
        try:
            # Ensure channel + JSON session; then a light command
            self.ensure_connected(None)
            result = self.execute("help --version", timeout=10.0, progress_cb=None)
            return any(msg.type == "success" for msg in result.messages)
        except Exception:
            return False

    def ensure_db_connected(
        self,
        progress_cb: Optional[ProgressCallback] = None,
        assume_disconnected: bool = False,
    ) -> None:
        """
        Ensure there is an active infobase connection.

        We cannot reliably probe DB connectivity (help --version is agent-only),
        so the most robust approach is to always attempt a silent connect and
        tolerate the "already connected" condition. This makes persistent flows
        resilient without spamming UI logs.
        """
        result = self.connect_ib(progress_cb=None)
        if not result.success:
            tolerated = False
            for msg in result.messages:
                data = msg.data or {}
                if isinstance(data, dict) and data.get("error-type") == "DesignerAlreadyConnectedToInfoBase":
                    tolerated = True
                    break
            if not tolerated:
                raise RuntimeError("ensure_db_connected: failed to connect to infobase")

    # Execution ------------------------------------------------------------------
    def execute(self, command: str, timeout: float = 120.0, progress_cb: Optional[ProgressCallback] = None) -> CommandResult:
        """
        Execute a single agent command and return the structured result.
        """
        self.ensure_connected(progress_cb)
        channel = self._get_channel()

        result = CommandResult(command=command, success=False)
        if progress_cb is None:
            def progress_cb(cmd, msg): return None  # noqa: E731

        self._parser.reset()
        self._trace(progress_cb, "Sending command", {"command": command, "timeout": timeout, "ssh": self._ssh.debug_info()})
        channel.send(command + "\n")
        deadline = time.time() + timeout
        messages: List[CommandMessage] = []
        finished = False

        while time.time() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="ignore")
                for payload in self._parser.feed(chunk):
                    try:
                        raw_messages = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    for entry in raw_messages:
                        message = CommandMessage(
                            type=entry.get("type", "log"),
                            message=entry.get("message"),
                            title=entry.get("title"),
                            data={k: v for k, v in entry.items() if k not in {
                                "type", "message", "title"}},
                        )
                        messages.append(message)
                        progress_cb(command, message)
                        if message.type in FINAL_MESSAGE_TYPES:
                            result.success = message.type == "success"
                            finished = True

                    if finished:
                        break
                if finished:
                    break
            elif channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode(
                    "utf-8", errors="ignore")
                if chunk:
                    message = CommandMessage(
                        type="error", message=chunk.strip())
                    messages.append(message)
                    progress_cb(command, message)
                    finished = True
                    result.success = False
                    break
            else:
                time.sleep(0.1)

        result.messages = messages
        result.mark_finished()
        if not finished:
            raise TimeoutError(
                f"Timeout while waiting for result of '{command}'.")
        self._drain_channel(channel)
        # Update last-used mark for idle timeout handling
        try:
            self._ssh.mark_used()
        except Exception:
            pass
        self._trace(progress_cb, "Command finished", {
            "command": command,
            "success": result.success,
            "messages": len(messages),
            "ssh": self._ssh.debug_info(),
        })
        return result

    def execute_sequence(
        self,
        commands: Iterable[str],
        timeout_per_command: float = 120.0,
        progress_cb: Optional[ProgressCallback] = None,
        stop_on_error: bool = True,
    ) -> List[CommandResult]:
        """
        Execute a sequence of commands returning individual results in order.
        """
        results = []
        try:
            for command in commands:
                result = self.execute(
                    command, timeout_per_command, progress_cb)
                results.append(result)
                if stop_on_error and not result.success:
                    break
            return results
        finally:
            self._parser.reset()

    # Helpers --------------------------------------------------------------------
    def _initialize_session(self, channel: paramiko.Channel, progress_cb: Optional[ProgressCallback] = None) -> None:
        command = (
            "options set "
            "--output-format=json "
            "--notify-progress=yes "
            "--show-prompt=no"
        )
        t0 = time.perf_counter()
        channel.send(command + "\n")
        # Drain output until the channel becomes quiet to avoid fixed sleeps
        drained = self._drain_until_quiet(
            channel, quiet_time=0.1, max_time=0.8)
        t1 = time.perf_counter()
        self._trace(progress_cb, "Init step", {
            "command": command,
            "elapsed_ms": int((t1 - t0) * 1000),
            "size": len(drained),
        })
        self._session.initialized = True

    def _drain_banner(self, channel: paramiko.Channel) -> str:
        banner: list[str] = []
        start = time.perf_counter()
        last_data = start
        max_time = 1.0
        quiet_time = 0.2
        while (time.perf_counter() - start) < max_time:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="ignore")
                if chunk:
                    banner.append(chunk)
                    last_data = time.perf_counter()
            else:
                if (time.perf_counter() - last_data) >= quiet_time:
                    break
                time.sleep(0.05)
        return "".join(banner)

    def _drain_channel(self, channel: paramiko.Channel) -> None:
        # Drain any trailing noise to keep the stream parser clean for next command
        start = time.time()
        while time.time() - start < 0.5:
            if channel.recv_ready():
                channel.recv(4096)
            else:
                break

    def _get_channel(self) -> paramiko.Channel:
        channel = self._ssh.channel
        if not channel or channel.closed:
            raise SSHConnectionError("SSH channel is not available.")
        return channel

    def _drain_until_quiet(self, channel: paramiko.Channel, *, quiet_time: float = 0.1, max_time: float = 0.6) -> str:
        # Read from channel until no data for quiet_time or max_time passes
        start = time.perf_counter()
        last_data = start
        chunks: list[str] = []
        while (time.perf_counter() - start) < max_time:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="ignore")
                if chunk:
                    chunks.append(chunk)
                    last_data = time.perf_counter()
            else:
                if (time.perf_counter() - last_data) >= quiet_time:
                    break
                time.sleep(0.05)
        return "".join(chunks)

    def _trace(self, progress_cb: Optional[ProgressCallback], title: str, data: Optional[dict] = None) -> None:
        import os
        if not progress_cb:
            return
        # Enable via env var to avoid noisy logs by default
        if os.getenv("ONEC_AGENT_UI_DEBUG_TRACE", "").lower() not in {"1", "true", "yes"}:
            return
        try:
            message = CommandMessage(
                type="log", title=f"[client] {title}", data=data)
            progress_cb("[client]", message)
        except Exception:
            # Do not let tracing affect execution
            pass


# ----------------------------------------------------------------------------
# Shared AgentClient pool for persistent sessions per project
_CLIENT_POOL: dict[str, AgentClient] = {}


def get_agent_client(project: ProjectModel, persistent: bool = False) -> AgentClient:
    if not persistent:
        return AgentClient(project)
    existing = _CLIENT_POOL.get(project.uid)
    if existing and existing.is_usable():
        return existing
    client = AgentClient(project)
    _CLIENT_POOL[project.uid] = client
    return client


def release_agent_client(project_uid: str) -> None:
    client = _CLIENT_POOL.pop(project_uid, None)
    if client:
        try:
            client.disconnect()
        except Exception:
            pass
