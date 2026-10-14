"""Gmail, read-only.

Scope: ``https://www.googleapis.com/auth/gmail.readonly`` only. JARVIS asks
Gmail for message *metadata* (From, Subject, Date) and Gmail's short preview
("snippet"); it never downloads full message bodies or attachments, and there
is no code path that sends, deletes, moves or marks mail.

Sign-in uses Google's OAuth flow for installed apps with a loopback redirect
(``python -m jarvis mail-login``): the owner signs in in the browser, and the
resulting token is stored in Windows Credential Manager, never in a file.
"""

from __future__ import annotations

import asyncio
import email.utils
import html
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .base import MailAuthError, MailMessage, MailProvider, clean_field
from .tokens import TokenStore

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class GmailProvider(MailProvider):
    name = "gmail"

    def __init__(self, store: TokenStore | None = None, *, query: str = "in:inbox", transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = 20.0) -> None:
        self.store = store or TokenStore()
        self.query = query
        self._client = httpx.AsyncClient(base_url=API, transport=transport, timeout=timeout_s)
        self._creds: Any = None

    def signed_in(self) -> bool:
        return self.store.get_token() is not None

    # -- credentials ---------------------------------------------------------
    def _load_creds(self) -> Any:
        from google.oauth2.credentials import Credentials

        info = self.store.get_token()
        if not info:
            raise MailAuthError("Gmail is not signed in. Run: python -m jarvis mail-login")
        return Credentials.from_authorized_user_info(info, SCOPES)

    def _refresh(self, creds: Any) -> Any:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request

        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # Typical causes: the 7-day expiry of apps in "Testing", a password change, revoked access.
            raise MailAuthError("The Gmail sign-in has expired or was revoked. Run: python -m jarvis mail-login") from exc
        self.store.set_token(json.loads(creds.to_json()))
        return creds

    async def _access_token(self, force_refresh: bool = False) -> str:
        if self._creds is None:
            self._creds = await asyncio.to_thread(self._load_creds)
        if force_refresh or not self._creds.valid:
            self._creds = await asyncio.to_thread(self._refresh, self._creds)
        return self._creds.token

    async def _get(self, path: str, params: Any) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._access_token(force_refresh=attempt > 0)
            r = await self._client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code in (401, 403):
                raise MailAuthError(f"Gmail refused access (HTTP {r.status_code}). Run: python -m jarvis mail-login")
            r.raise_for_status()
            return r.json()
        raise MailAuthError("Gmail refused access")

    # -- reading ---------------------------------------------------------------
    async def list_recent(self, *, after: float, max_results: int) -> list[MailMessage]:
        q = f"{self.query} after:{int(after)}".strip()
        listing = await self._get("/messages", {"q": q, "maxResults": max_results})
        out: list[MailMessage] = []
        for ref in listing.get("messages", [])[:max_results]:
            meta = await self._get(
                f"/messages/{ref['id']}",
                [("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")],
            )
            out.append(parse_message(meta))
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_message(meta: dict[str, Any]) -> MailMessage:
    headers = {h.get("name", "").lower(): h.get("value", "") for h in (meta.get("payload") or {}).get("headers", [])}
    name, addr = email.utils.parseaddr(headers.get("from", ""))
    return MailMessage(
        id=str(meta.get("id", "")),
        sender_name=clean_field(name, 80),
        sender_email=clean_field(addr, 120).lower(),
        subject=clean_field(headers.get("subject", ""), 200),
        snippet=clean_field(html.unescape(meta.get("snippet", "")), 300),
        received_at=int(meta.get("internalDate", "0")) / 1000.0,
        labels=list(meta.get("labelIds", []))[:20],
    )


# ---------------------------------------------------------------- sign-in ----
def login(client_secret_file: Path | None = None, store: TokenStore | None = None) -> str:  # pragma: no cover - interactive
    """Interactive sign-in in the browser. Returns the signed-in address."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    # The library logs the full redirect URL at INFO, including the one-time
    # authorization code. Keep it out of the console and any log file.
    logging.getLogger("google_auth_oauthlib").setLevel(logging.WARNING)
    store = store or TokenStore()
    if client_secret_file is not None:
        client = json.loads(Path(client_secret_file).read_text(encoding="utf-8"))
        if "installed" not in client:
            raise SystemExit("The client file must be an OAuth client of type 'Desktop app' (it has an 'installed' section).")
        store.set_client(client)
    client = store.get_client()
    if not client:
        raise SystemExit("No OAuth client stored yet. Run: python -m jarvis mail-login --client-secret <path to client_secret.json>")
    flow = InstalledAppFlow.from_client_config(client, SCOPES)
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        open_browser=True,
        authorization_prompt_message="Opening the browser for Google sign-in (read-only Gmail access)...",
        success_message="JARVIS has read-only access to Gmail. You can close this tab.",
    )
    store.set_token(json.loads(creds.to_json()))
    r = httpx.get(f"{API}/profile", headers={"Authorization": f"Bearer {creds.token}"}, timeout=15)
    r.raise_for_status()
    return r.json().get("emailAddress", "?")


def logout(store: TokenStore | None = None, *, forget_client: bool = False) -> bool:  # pragma: no cover - network
    """Revoke the token at Google and remove it from Credential Manager."""
    store = store or TokenStore()
    info = store.get_token()
    if info:
        token = info.get("refresh_token") or info.get("token")
        try:
            httpx.post(REVOKE_URL, data={"token": token}, timeout=15)
        except httpx.HTTPError:
            log.warning("Could not reach Google to revoke the token; removing it locally anyway")
    removed = store.delete_token()
    if forget_client:
        store.delete_client()
    return removed
