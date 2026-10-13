"""The owner's rules: what is important, when JARVIS may speak, preferred style.

Loaded from ``config/rules.toml`` (git-ignored; created from
``config/rules.example.toml``). Rules decide priorities and style only. They
never grant permissions: monitoring stays read-only, content from e-mail is
data and never instructions, and dry-run settings are not touched by rules.
"""

from __future__ import annotations

import logging
import re
import tomllib
from datetime import datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import PROJECT_ROOT, ConfigError

if TYPE_CHECKING:
    from .core.tools import ToolRegistry

log = logging.getLogger(__name__)

RULES_PATH = PROJECT_ROOT / "config" / "rules.toml"
RULES_EXAMPLE_PATH = PROJECT_ROOT / "config" / "rules.example.toml"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse_hhmm(v: str) -> time:
    h, m = v.split(":")
    return time(int(h), int(m))


def _clean_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip()
        if v and v.lower() not in (o.lower() for o in out):
            out.append(v)
    return out


class MailRules(BaseModel):
    important_senders: list[str] = Field(default_factory=list, max_length=500)
    important_domains: list[str] = Field(default_factory=list, max_length=500)
    important_keywords: list[str] = Field(default_factory=list, max_length=500)
    # Urgent: above important; may break through quiet hours.
    urgent_senders: list[str] = Field(default_factory=list, max_length=500)
    urgent_keywords: list[str] = Field(default_factory=list, max_length=500)
    ignored_senders: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("important_senders", "important_keywords", "urgent_senders", "urgent_keywords", "ignored_senders")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return _clean_list(v)

    @field_validator("important_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return _clean_list([d.lower().lstrip("@") for d in v])


class NotificationRules(BaseModel):
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    max_announcements_per_hour: int = Field(4, ge=0, le=60)
    morning_summary_time: str = "07:30"
    urgent_overrides_quiet_hours: bool = True

    @field_validator("quiet_hours_start", "quiet_hours_end", "morning_summary_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v.strip()):
            raise ValueError("must be a time as HH:MM (24-hour), e.g. 07:30")
        return v.strip()

    def is_quiet(self, now: datetime) -> bool:
        start, end, t = _parse_hhmm(self.quiet_hours_start), _parse_hhmm(self.quiet_hours_end), now.time()
        if start == end:
            return False
        if start < end:
            return start <= t < end
        return t >= start or t < end  # spans midnight, e.g. 22:00-07:00

    @property
    def morning_time(self) -> time:
        return _parse_hhmm(self.morning_summary_time)


class Preferences(BaseModel):
    notes: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("notes")
    @classmethod
    def _notes(cls, v: list[str]) -> list[str]:
        cleaned = _clean_list([n.replace("\n", " ") for n in v])
        too_long = [n for n in cleaned if len(n) > 300]
        if too_long:
            raise ValueError("each preference must be at most 300 characters")
        return cleaned


class RulesConfig(BaseModel):
    mail: MailRules = Field(default_factory=MailRules)
    notifications: NotificationRules = Field(default_factory=NotificationRules)
    preferences: Preferences = Field(default_factory=Preferences)

    def public(self) -> dict[str, Any]:
        n = self.notifications
        return {
            "mail": self.mail.model_dump(),
            "quiet_hours": f"{n.quiet_hours_start}-{n.quiet_hours_end}",
            "max_announcements_per_hour": n.max_announcements_per_hour,
            "morning_summary_time": n.morning_summary_time,
            "urgent_overrides_quiet_hours": n.urgent_overrides_quiet_hours,
            "preferences": self.preferences.notes,
        }


def load_rules(path: Path | None = None) -> RulesConfig:
    """Load the owner's rules. A missing rules.toml falls back to the example file."""
    path = path or RULES_PATH
    source = path if path.exists() else RULES_EXAMPLE_PATH
    if not source.exists():
        log.warning("No rules file found; using built-in defaults")
        return RulesConfig()
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
        return RulesConfig.model_validate(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from exc
    except ValidationError as exc:
        lines = [f"{source}: rules are invalid:"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc


def build_preferences_section(rules: RulesConfig | None) -> str:
    """Owner's preferences for the system prompt, explicitly below the safety rules."""
    if rules is None or not rules.preferences.notes:
        return ""
    items = "\n".join(f"- {n}" for n in rules.preferences.notes)
    return (
        "\n\nOwner's preferences (Ägarens preferenser), style only. They NEVER override the safety and tool rules above.\n"
        f"{items}"
    )


def register_rules_tools(registry: "ToolRegistry", get_rules) -> None:
    from .core.tools import Risk, ToolResult, ToolSpec, schema

    async def rules_show(args, ctx) -> ToolResult:
        return ToolResult(ok=True, data=get_rules().public(), sensitivity="personal")

    registry.register(
        ToolSpec(
            "rules_show",
            "Show the owner's rules: important senders, domains and keywords for e-mail, ignored senders, "
            "quiet hours, announcement limit, morning summary time and style preferences. Use for questions "
            "like 'which rules do you have?' / 'vilka regler har du?'.",
            schema({}),
            rules_show,
            Risk.READ,
            "system",
            output_sensitivity="personal",
        )
    )
