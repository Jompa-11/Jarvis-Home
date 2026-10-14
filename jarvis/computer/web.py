"""Open web pages and searches in the default browser. Only http and https."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote_plus, urlsplit

from .windows import ComputerError

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.google.com/search?q={q}"
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I)

SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
    "maps": "https://www.google.com/maps", "kartor": "https://www.google.com/maps",
    "github": "https://github.com", "reddit": "https://www.reddit.com", "wikipedia": "https://sv.wikipedia.org",
    "netflix": "https://www.netflix.com", "spotify": "https://open.spotify.com", "twitch": "https://www.twitch.tv",
    "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com", "tiktok": "https://www.tiktok.com",
    "x": "https://x.com", "twitter": "https://x.com", "linkedin": "https://www.linkedin.com",
    "svt": "https://www.svt.se", "svt play": "https://www.svtplay.se", "svtplay": "https://www.svtplay.se",
    "aftonbladet": "https://www.aftonbladet.se", "expressen": "https://www.expressen.se", "dn": "https://www.dn.se",
    "blocket": "https://www.blocket.se", "tradera": "https://www.tradera.com", "swish": "https://www.swish.nu",
    "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
}


def resolve_target(target: str) -> str:
    """A site name, a domain or a full URL -> an http(s) URL. Anything else is refused."""
    text = str(target).strip().strip('"').rstrip("/")
    if not text:
        raise ComputerError("Ange en webbplats eller adress.")
    known = SITES.get(text.lower())
    if known:
        return known
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme.lower() not in ("http", "https"):
            raise ComputerError(f"JARVIS öppnar bara http- och https-adresser, inte '{parts.scheme}:'.")
        if not parts.netloc:
            raise ComputerError(f"'{target}' är ingen giltig adress.")
        return text
    head = text.split("/", 1)[0]
    if _DOMAIN.match(head):
        return "https://" + text
    raise ComputerError(f"'{target}' är varken en känd webbplats eller en adress. Använd sökning i stället.")


def search_url(query: str) -> str:
    q = str(query).strip()
    if not q:
        raise ComputerError("Ange vad du vill söka på.")
    if len(q) > 300:
        raise ComputerError("Söktexten är för lång.")
    return SEARCH_URL.format(q=quote_plus(q))


class WebOpener:
    def __init__(self, open_url: Callable[[str], None] | None = None) -> None:
        self._open = open_url or _start_url

    def open_site(self, target: str, *, dry_run: bool) -> dict[str, Any]:
        url = resolve_target(target)
        if dry_run:
            return {"would_open": url}
        self._open(url)
        log.info("Opened %s in the browser", urlsplit(url).netloc)
        return {"opened": url}

    def search(self, query: str, *, dry_run: bool) -> dict[str, Any]:
        url = search_url(query)
        if dry_run:
            return {"would_search": query, "url": url}
        self._open(url)
        log.info("Opened a web search in the browser")
        return {"searched": query, "url": url}


def _start_url(url: str) -> None:  # pragma: no cover - opens a real browser
    # The scheme is checked above, so this can only ever be http(s).
    os.startfile(url)  # noqa: S606
