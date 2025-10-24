"""
SSH connection management utilities built on top of paramiko.
"""

from __future__ import annotations

import socket
import time
from typing import Optional

import paramiko

from .models import CredentialsModel


class SSHConnectionError(RuntimeError):
    """Raised when an SSH connection cannot be established or maintained."""


class SSHConnectionManager:
    """
    Lightweight wrapper around :mod:`paramiko` to manage the SSH lifecycle.
    """

    def __init__(self, credentials: CredentialsModel) -> None:
        self.credentials = credentials
        self._client: Optional[paramiko.SSHClient] = None
        self._channel: Optional[paramiko.Channel] = None

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
        return channel

    def close_channel(self) -> None:
        if self._channel:
            try:
                self._channel.close()
            finally:
                self._channel = None

    def close(self) -> None:
        self.close_channel()
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None

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
