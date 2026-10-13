"""OAuth client config and tokens in Windows Credential Manager (via keyring).

Never written to files, config, logs or git. Every value read is registered
with the log redactor.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..logging_utils import register_secret

log = logging.getLogger(__name__)

SERVICE = "jarvis"
TOKEN_KEY = "gmail_oauth_token"
CLIENT_KEY = "gmail_oauth_client"


class TokenStore:
    def __init__(self, backend: Any = None) -> None:
        self._backend = backend  # injectable for tests; default: the keyring module

    @property
    def kr(self) -> Any:
        if self._backend is None:
            import keyring

            self._backend = keyring
        return self._backend

    def _get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self.kr.get_password(SERVICE, key)
        except Exception as exc:
            log.warning("Credential Manager read failed (%s)", type(exc).__name__)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("Stored %s is not valid JSON", key)
            return None
        for v in _secret_values(data):
            register_secret(v)
        return data

    def _set(self, key: str, data: dict[str, Any]) -> None:
        for v in _secret_values(data):
            register_secret(v)
        self.kr.set_password(SERVICE, key, json.dumps(data, separators=(",", ":")))

    def _delete(self, key: str) -> bool:
        try:
            self.kr.delete_password(SERVICE, key)
            return True
        except Exception:
            return False

    def get_token(self) -> dict[str, Any] | None:
        return self._get(TOKEN_KEY)

    def set_token(self, data: dict[str, Any]) -> None:
        self._set(TOKEN_KEY, data)

    def delete_token(self) -> bool:
        return self._delete(TOKEN_KEY)

    def get_client(self) -> dict[str, Any] | None:
        return self._get(CLIENT_KEY)

    def set_client(self, data: dict[str, Any]) -> None:
        self._set(CLIENT_KEY, data)

    def delete_client(self) -> bool:
        return self._delete(CLIENT_KEY)


def _secret_values(data: Any) -> list[str]:
    out: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and any(s in k.lower() for s in ("token", "secret")) and len(v) >= 8:
                out.append(v)
            else:
                out.extend(_secret_values(v))
    elif isinstance(data, list):
        for v in data:
            out.extend(_secret_values(v))
    return out
