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
        self._ssh = SSHConnectionManager(project.credentials)
        self._session = AgentSessionInfo()
        self._parser = _JsonArrayParser()

    # Session management ---------------------------------------------------------
    def ensure_connected(self) -> None:
        channel = self._ssh.ensure_channel()
        if not self._session.banner:
            self._session.banner = self._drain_banner(channel)
        if not self._session.initialized:
            self._initialize_session(channel)

    def disconnect(self) -> None:
        self._ssh.close()
        self._session = AgentSessionInfo()
        self._parser.reset()

    def connect_ib(self, progress_cb: Optional[ProgressCallback] = None) -> CommandResult:
        return self.execute("common connect-ib", timeout=60.0, progress_cb=progress_cb)

    def disconnect_ib(self, progress_cb: Optional[ProgressCallback] = None) -> CommandResult:
        return self.execute("common disconnect-ib", timeout=30.0, progress_cb=progress_cb)

    # Execution ------------------------------------------------------------------
    def execute(self, command: str, timeout: float = 120.0, progress_cb: Optional[ProgressCallback] = None) -> CommandResult:
        """
        Execute a single agent command and return the structured result.
        """
        self.ensure_connected()
        channel = self._get_channel()

        result = CommandResult(command=command, success=False)
        if progress_cb is None:
            progress_cb = lambda cmd, msg: None  # noqa: E731

        self._parser.reset()
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
                            data={k: v for k, v in entry.items() if k not in {"type", "message", "title"}},
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
                chunk = channel.recv_stderr(4096).decode("utf-8", errors="ignore")
                if chunk:
                    message = CommandMessage(type="error", message=chunk.strip())
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
            raise TimeoutError(f"Timeout while waiting for result of '{command}'.")
        self._drain_channel(channel)
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
                result = self.execute(command, timeout_per_command, progress_cb)
                results.append(result)
                if stop_on_error and not result.success:
                    break
            return results
        finally:
            self._parser.reset()

    # Helpers --------------------------------------------------------------------
    def _initialize_session(self, channel: paramiko.Channel) -> None:
        for command in (
            "options set --output-format=json",
            "options set --notify-progress=yes",
            "options set --show-prompt=no",
        ):
            channel.send(command + "\n")
            time.sleep(0.2)
            self._drain_channel(channel)
        self._session.initialized = True

    def _drain_banner(self, channel: paramiko.Channel) -> str:
        banner = []
        start = time.time()
        while time.time() - start < 1.5:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="ignore")
                banner.append(chunk)
            else:
                time.sleep(0.1)
        return "".join(banner)

    def _drain_channel(self, channel: paramiko.Channel) -> None:
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
