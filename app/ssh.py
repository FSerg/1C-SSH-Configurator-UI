"""
SSH connection management utilities built on top of paramiko.
"""

from __future__ import annotations

import socket
import time
from typing import Optional
from datetime import datetime, timedelta

import paramiko

from .models import CredentialsModel


class SSHConnectionError(RuntimeError):
    """Raised when an SSH connection cannot be established or maintained."""


class SSHConnectionManager:
    """
    Lightweight wrapper around :mod:`paramiko` to manage the SSH lifecycle.
    """

    def __init__(self, credentials: CredentialsModel, *, idle_timeout_seconds: int | None = None) -> None:
        self.credentials = credentials
        self._client: Optional[paramiko.SSHClient] = None
        self._channel: Optional[paramiko.Channel] = None
        self._idle_timeout: Optional[int] = idle_timeout_seconds
        self._last_used: Optional[datetime] = None

    # Public API -----------------------------------------------------------------
    def connect(self) -> None:
        if self._client and self._client.get_transport() and self._client.get_transport().is_active():
            return

        self.close()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.credentials.host,
            "port": self.credentials.port,
            "username": self.credentials.username,
            "password": self.credentials.password,
            "timeout": self.credentials.timeout,
            "allow_agent": self.credentials.allow_agent,
            "look_for_keys": self.credentials.look_for_keys,
        }
        if self.credentials.private_key_path:
            connect_kwargs["pkey"] = self._load_private_key(
                self.credentials.private_key_path,
                self.credentials.passphrase,
            )
            connect_kwargs.pop("password", None)

        try:
            client.connect(**connect_kwargs)
        except (paramiko.SSHException, socket.error) as exc:
            raise SSHConnectionError(str(exc)) from exc

        transport = client.get_transport()
        if not transport:
            client.close()
            raise SSHConnectionError("SSH transport is not available after connecting.")

        if self.credentials.keepalive_interval:
            transport.set_keepalive(self.credentials.keepalive_interval)

        self._client = client

    # Internal helpers -----------------------------------------------------------
    @staticmethod
    def _load_private_key(path: str, password: Optional[str]) -> paramiko.PKey:
        errors = []
        for key_cls in (
            paramiko.RSAKey,
            getattr(paramiko, "ECDSAKey", None),
            getattr(paramiko, "Ed25519Key", None),
            getattr(paramiko, "DSSKey", None),
        ):
            if key_cls is None:
                continue
            try:
                return key_cls.from_private_key_file(path, password=password)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key_cls.__name__}: {exc}")
        raise SSHConnectionError(
            "Unable to load private key. Tried: " + "; ".join(errors)
        )

    def ensure_channel(self) -> paramiko.Channel:
        self.connect()
        assert self._client is not None  # for MyPy
        transport = self._client.get_transport()
        if not transport or not transport.is_active():
            raise SSHConnectionError("SSH transport is not active.")

        # Close the channel if it's been idle for longer than configured timeout
        if (
            self._idle_timeout
            and self._channel
            and not self._channel.closed
            and self._last_used
            and (datetime.now() - self._last_used) > timedelta(seconds=self._idle_timeout)
        ):
            try:
                self._channel.close()
            finally:
                self._channel = None

        if self._channel and not self._channel.closed:
            return self._channel

        try:
            channel = transport.open_session()
            channel.invoke_shell()
        except (paramiko.SSHException, socket.error) as exc:
            raise SSHConnectionError(f"Failed to open agent shell: {exc}") from exc

        # Do not introduce a fixed delay here; banner draining in AgentClient
        # will wait just enough for initial output.
        self._channel = channel
        self._last_used = datetime.now()
        return channel

    def close_channel(self) -> None:
        if self._channel:
            try:
                self._channel.close()
            finally:
                self._channel = None
                self._last_used = None

    def close(self) -> None:
        self.close_channel()
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None
                self._last_used = None

    # Context manager interface --------------------------------------------------
    def __enter__(self) -> "SSHConnectionManager":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # Convenience accessors ------------------------------------------------------
    @property
    def channel(self) -> Optional[paramiko.Channel]:
        return self._channel

    @property
    def client(self) -> Optional[paramiko.SSHClient]:
        return self._client

    def mark_used(self) -> None:
        self._last_used = datetime.now()

    def debug_info(self) -> dict:
        """Return a snapshot of SSH connection and channel state for tracing."""
        transport_active = None
        if self._client and self._client.get_transport():
            transport_active = self._client.get_transport().is_active()
        return {
            "client": bool(self._client),
            "transport_active": bool(transport_active),
            "channel_open": bool(self._channel and not getattr(self._channel, "closed", True)),
            "idle_timeout": self._idle_timeout,
            "last_used": self._last_used.isoformat() if self._last_used else None,
        }
