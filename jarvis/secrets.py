"""Secret lookup.

Secrets are read from environment variables (optionally loaded from a local,
git-ignored ``.env`` file) or, if the optional ``keyring`` package is installed,
from the Windows Credential Manager under the service name ``jarvis``.

Every secret that is read is registered with the log redactor so that it can
never appear in log output, even by accident.
"""

from __future__ import annotations

import os

from .logging_utils import register_secret

KEYRING_SERVICE = "jarvis"


def get_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value:
        value = _from_keyring(name) or ""
    if not value:
        return None
    register_secret(value)
    return value


def has_secret(name: str) -> bool:
    return get_secret(name) is not None


def _from_keyring(name: str) -> str | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:  # keyring backends raise a variety of errors
        return None
