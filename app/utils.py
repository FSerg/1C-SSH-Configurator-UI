"""
Utility helpers for the 1C agent Streamlit application.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from typing import Iterable, List

DEFAULT_SECRET_SALT = os.environ.get("ONEC_AGENT_UI_SECRET_SALT", "1c-agent-ui")
_SECRET_LOCK = threading.RLock()


def _derive_key(salt: str) -> bytes:
    digest = hashlib.sha256(salt.encode("utf-8")).digest()
    # Use the whole digest as the repeating XOR key
    return digest


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    key_len = len(key)
    return bytes(b ^ key[i % key_len] for i, b in enumerate(data))


def obfuscate_secret(secret: str, salt: str | None = None) -> str:
    """
    Obfuscate a secret value using XOR + urlsafe base64 encoding.
    """
    if not secret:
        return ""

    with _SECRET_LOCK:
        key = _derive_key(salt or DEFAULT_SECRET_SALT)
        raw = secret.encode("utf-8")
        obfuscated = _xor_bytes(raw, key)
        return base64.urlsafe_b64encode(obfuscated).decode("ascii")


def reveal_secret(obfuscated: str, salt: str | None = None) -> str:
    """
    Reverse obfuscation applied by :func:`obfuscate_secret`.
    """
    if not obfuscated:
        return ""

    with _SECRET_LOCK:
        key = _derive_key(salt or DEFAULT_SECRET_SALT)
        raw = base64.urlsafe_b64decode(obfuscated.encode("ascii"))
        revealed = _xor_bytes(raw, key)
        return revealed.decode("utf-8")


def mask_secret(secret: str, unmasked: int = 2) -> str:
    """
    Produce a masked representation of a secret.
    """
    if not secret:
        return ""
    if len(secret) <= unmasked:
        return "*" * len(secret)
    return secret[:unmasked] + "..." + "*" * (len(secret) - unmasked)


def normalize_relative_agent_path(path: str) -> str:
    """
    Validate and normalize relative paths that will be sent to the agent.
    """
    path = path.strip()
    if not path:
        return ""
    normalized = path.replace("\\", "/")
    if not normalized.startswith("../"):
        raise ValueError("Path must be relative to the agent home via '../'.")
    return normalized


def clean_multiline_input(lines: str) -> List[str]:
    """
    Convert multi-line text input to a list of trimmed, non-empty strings.
    """
    return [line.strip() for line in lines.splitlines() if line.strip()]


def join_multiline(values: Iterable[str]) -> str:
    """
    Join values back into a newline-separated representation.
    """
    return "\n".join(sorted({value.strip() for value in values if value.strip()}))
