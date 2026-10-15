"""Mail watcher: polls the read-only provider, classifies, and notifies with fixed templates.

Only metadata stays in memory (sender, subject, time, priority, one-line
summary) for "have I received any important mail?". On disk the Notifier keeps
just a hash of each message id and a timestamp ("already notified").
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..config import MailConfig
from ..providers.base import AIProvider
from ..rules import MailRules, RulesConfig
from .base import MailAuthError, MailMessage, MailProvider
from .classify import Classification, classify_by_rules, classify_with_model, sender_label

if TYPE_CHECKING:
    from ..core.tools import ToolRegistry
    from ..monitoring.service import MonitorService

log = logging.getLogger(__name__)


def notification_text(msg: MailMessage, c: Classification, rules: MailRules) -> tuple[str, str | None]:
    """Fixed templates. Mail content only fills the sender and one line, never the wording.

    The sender always shows the REAL domain ("Skatteverket (skatteverket.se)"), and a
    display name that imitates a known sender is flagged ("(okänd avsändare: exempel.com)").
    """
    who = sender_label(msg, rules)
    title = f"Viktigt mejl från {who}" if c.priority in ("high", "urgent") else f"Mejl från {who}"
    if c.by == "guard":
        return title, c.summary  # do not repeat the suspicious subject either
    detail = c.summary or msg.subject or None
    return title, detail


class MailWatcher:
    def __init__(
        self,
        cfg: MailConfig,
        provider: MailProvider,
        monitor: "MonitorService",
        get_rules: Callable[[], RulesConfig],
        get_local_model: Callable[[], AIProvider | None],
        *,
        clock: Callable[[], float] = time.time,
        is_gaming: Callable[[], bool] = lambda: False,
    ) -> None:
        self.is_gaming = is_gaming
        # Mail waiting for the local model while game mode is on (in memory only; max 200).
        self.waiting: OrderedDict[str, MailMessage] = OrderedDict()
        self.cfg = cfg
        self.provider = provider
        self.monitor = monitor
        self.get_rules = get_rules
        self.get_local_model = get_local_model
        self.clock = clock
        self.started_at = clock()
        self.last_poll: float | None = None
        self.last_error: str | None = None
        self.recent: deque[dict[str, Any]] = deque(maxlen=cfg.recent_limit)
        self._auth_warned = False

    def state(self) -> tuple[str, str]:
        if not self.provider.signed_in():
            return "needs_login", "Gmail is not signed in. Run: python -m jarvis mail-login"
        if self.last_error:
            return "error", self.last_error
        polled = time.strftime("%H:%M", time.localtime(self.last_poll)) if self.last_poll else "not yet"
        waiting = f", {len(self.waiting)} waiting for game mode to end" if self.waiting else ""
        return "active", f"Gmail (read-only), every {self.cfg.poll_minutes} min, last check {polled}{waiting}"

    def _publish_state(self) -> None:
        self.monitor.set_source("mail", *self.state())

    def important_since(self, since: float | None) -> list[str]:
        """One sentence per important mail received since ``since`` (default: the last 24 h)."""
        cutoff = since if since is not None else self.clock() - 86400
        out = []
        for m in self.recent:
            if m["priority"] in ("high", "urgent") and m["ts"] >= cutoff:
                what = (m["summary"] or m["subject"] or "(inget ämne)").rstrip(" .")
                out.append(f"{'AKUT: ' if m['priority'] == 'urgent' else ''}{m['from']}: {what}.")
        return out

    async def poll(self) -> int:
        """One check. Returns how many new messages were handled."""
        if self.monitor.paused:
            return 0
        now = self.clock()
        lookback = self.cfg.initial_lookback_hours * 3600
        after = (self.last_poll - 3600) if self.last_poll else (self.started_at - lookback)
        try:
            messages = await self.provider.list_recent(after=after, max_results=self.cfg.max_per_poll)
        except MailAuthError as exc:
            self.last_error = str(exc)
            self._publish_state()
            if not self._auth_warned:
                self._auth_warned = True
                await self.monitor.notifier.notify(source="mail", title="Gmail-inloggningen behöver förnyas", detail="Kör: python -m jarvis mail-login", priority="high", kind="mail_auth", dedupe_key=f"mail-auth:{time.strftime('%Y-%m-%d')}")
            return 0
        except Exception as exc:
            self.last_error = f"Could not check mail ({type(exc).__name__})"
            log.warning("Mail check failed: %s", type(exc).__name__)
            self._publish_state()
            return 0
        self.last_error = None
        self._auth_warned = False
        handled = 0
        for msg in sorted(messages, key=lambda m: m.received_at):
            key = f"gmail:{msg.id}"
            if self.monitor.notifier.seen.seen(key):
                continue
            mail_rules = self.get_rules().mail
            c = classify_by_rules(msg, mail_rules)
            if c is None:
                if self.is_gaming():
                    # Game mode: no model call now. Rule matches above still notify at once.
                    self.waiting[msg.id] = msg
                    while len(self.waiting) > 200:
                        self.waiting.popitem(last=False)
                    continue
                c = await classify_with_model(msg, self.get_local_model(), timeout_s=self.cfg.classify_timeout_s)
            handled += 1
            await self._deliver(msg, c)
        self.last_poll = now
        self._publish_state()
        return handled

    async def classify_waiting(self) -> int:
        """Classify the mail that waited during game mode (called when the game closes)."""
        handled = 0
        while self.waiting and not self.is_gaming():
            _, msg = self.waiting.popitem(last=False)
            if self.monitor.notifier.seen.seen(f"gmail:{msg.id}"):
                continue
            c = classify_by_rules(msg, self.get_rules().mail)
            if c is None:
                c = await classify_with_model(msg, self.get_local_model(), timeout_s=self.cfg.classify_timeout_s)
            await self._deliver(msg, c)
            handled += 1
        self._publish_state()
        return handled

    async def _deliver(self, msg: MailMessage, c: Classification) -> None:
        key = f"gmail:{msg.id}"
        self.waiting.pop(msg.id, None)
        if c.priority == "skip":
            self.monitor.notifier.seen.add(key)
            return
        mail_rules = self.get_rules().mail
        title, detail = notification_text(msg, c, mail_rules)
        await self.monitor.notifier.notify(source="mail", title=title, detail=detail, priority=c.priority, kind="mail", dedupe_key=key)  # type: ignore[arg-type]
        self.recent.append(
            {
                "received": time.strftime("%Y-%m-%d %H:%M", time.localtime(msg.received_at)),
                "from": sender_label(msg, mail_rules),
                # mail_list_recent output reaches the chat model, which has tools: a
                # suspicious subject is not passed on.
                "subject": "(dolt: möjligt manipulationsförsök)" if c.by == "guard" else msg.subject,
                "priority": c.priority,
                "summary": c.summary,
                "reason": c.reason,
                "classified_by": c.by,
                "ts": msg.received_at,
            }
        )


def register_mail_tools(registry: "ToolRegistry", watcher: MailWatcher) -> None:
    from ..core.tools import Risk, ToolResult, ToolSpec, schema

    async def mail_list_recent(args, ctx) -> ToolResult:
        state, detail = watcher.state()
        if state == "needs_login":
            return ToolResult(ok=False, error="Gmail is not signed in yet (python -m jarvis mail-login).")
        limit = int(args.get("limit", 10))
        items = [{k: v for k, v in i.items() if k != "ts"} for i in watcher.recent][::-1]
        if args.get("important_only", False):
            items = [i for i in items if i["priority"] in ("high", "urgent")]
        return ToolResult(
            ok=True,
            sensitivity="personal",
            data={"mail_monitoring": state, "detail": detail, "messages": items[:limit], "total_seen_since_start": len(watcher.recent)},
        )

    registry.register(
        ToolSpec(
            "mail_list_recent",
            "List recent e-mails JARVIS has seen while monitoring (sender, subject, priority, one-line summary), "
            "newest first. Read-only. Use for 'have I received any important mail?' / 'har jag fått några viktiga mejl?'.",
            schema(
                {
                    "important_only": {"type": "boolean", "description": "Only important and urgent mail."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                }
            ),
            mail_list_recent,
            Risk.READ,
            "personal",
            output_sensitivity="personal",
            untrusted_output=True,
        )
    )
