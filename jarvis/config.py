"""Configuration loading and validation.

Configuration comes from three places, in increasing priority:

1. Built-in defaults (the pydantic models below).
2. ``config/jarvis.toml`` (or the file named by ``JARVIS_CONFIG``).
3. A small set of environment overrides (``JARVIS_HOST``, ``JARVIS_PORT``, ``HA_URL``).

Secrets are never stored in the TOML file. The config only names the
environment variable that holds each secret (see :mod:`jarvis.secrets`).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "jarvis.toml"

Sensitivity = Literal["normal", "personal", "restricted"]
SENSITIVITY_ORDER: dict[str, int] = {"normal": 0, "personal": 1, "restricted": 2}
Role = Literal["owner", "adult", "child", "guest"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ConfigError(Exception):
    """Raised when the configuration file is missing required data or invalid."""


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)
    open_browser: bool = True

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # The control API can move speakers and open applications. Binding it to
        # the LAN would let anyone on the network do the same, so Phase 1 only
        # allows loopback addresses.
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("server.host must be a loopback address (127.0.0.1, localhost or ::1)")
        return v


class ModelRoute(BaseModel):
    """One entry in a provider fallback chain."""

    # Name of a registered provider ("anthropic", "ollama", "dev", or one you add).
    provider: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    model: str | None = None
    effort: Effort | None = None


def _default_routes() -> dict[str, list[ModelRoute]]:
    cloud_then_local = [
        ModelRoute(provider="anthropic", model="claude-opus-5-5", effort="low"),
        ModelRoute(provider="ollama"),
        ModelRoute(provider="dev"),
    ]
    return {
        "conversation": [r.model_copy() for r in cloud_then_local],
        "smart_home": [r.model_copy() for r in cloud_then_local],
        "computer": [r.model_copy() for r in cloud_then_local],
    }


class ModelPrice(BaseModel):
    """USD per million tokens. Cache prices default to Anthropic's standard
    multipliers (writes 1.25x, reads 0.1x input) when not given."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cache_write_per_mtok: float | None = Field(None, ge=0)  # 5-minute cache writes
    cache_read_per_mtok: float | None = Field(None, ge=0)
    # Some models charge more for long prompts (Claude Haiku 5.5: over 100,000
    # tokens, counting input, cache reads and cache writes).
    long_prompt_threshold_tokens: int | None = Field(None, gt=0)
    long_prompt: "ModelPrice | None" = None


def _default_pricing() -> dict[str, ModelPrice]:
    # Anthropic first-party rates, verified 2026-10-12 against
    # https://platform.claude.com/docs/en/about-claude/pricing
    # Update here if prices change; costs shown in the UI are estimates.
    return {
        "claude-opus-5-5": ModelPrice(
            input_per_mtok=4.00, output_per_mtok=20.00, cache_write_per_mtok=5.00, cache_read_per_mtok=0.20
        ),
        "claude-sonnet-5-5": ModelPrice(
            input_per_mtok=2.00, output_per_mtok=10.00, cache_write_per_mtok=2.50, cache_read_per_mtok=0.10
        ),
        "claude-haiku-5-5": ModelPrice(
            input_per_mtok=0.10,
            output_per_mtok=0.50,
            cache_write_per_mtok=0.125,
            cache_read_per_mtok=0.01,
            long_prompt_threshold_tokens=100_000,
            long_prompt=ModelPrice(
                input_per_mtok=0.50, output_per_mtok=2.50, cache_write_per_mtok=0.625, cache_read_per_mtok=0.05
            ),
        ),
    }


class AnthropicConfig(BaseModel):
    enabled: bool = True
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = Field(16000, ge=256, le=64000)
    max_retries: int = Field(2, ge=0, le=5)
    # Server-side refusal fallback ("fallbacks": "default"). Only sent for
    # models that support it.
    server_side_fallback: bool = True


class OllamaConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    keep_alive: str = "10m"
    # Context window in tokens. Ollama's default (often 4096) is too small for
    # JARVIS's instructions plus tool definitions plus history, and Ollama
    # silently drops the oldest text when it overflows. 8192 fits a 7B model
    # comfortably in 8 GB of VRAM.
    num_ctx: int = Field(8192, ge=2048, le=131072)
    # Lower temperature makes small models choose tools more consistently.
    temperature: float = Field(0.2, ge=0, le=2)


class AIConfig(BaseModel):
    routes: dict[str, list[ModelRoute]] = Field(default_factory=_default_routes)
    # The dev fallback is a rule-based stand-in so the app is testable without
    # any model. It is never silent: responses are labelled as fallback.
    allow_dev_fallback: bool = True
    request_timeout_s: float = Field(60, gt=0, le=600)
    max_tool_rounds: int = Field(6, ge=1, le=20)
    monthly_budget_usd: float = Field(10.0, ge=0)
    pricing: dict[str, ModelPrice] = Field(default_factory=_default_pricing)

    @model_validator(mode="after")
    def _routes_valid(self) -> "AIConfig":
        if "conversation" not in self.routes:
            raise ValueError("ai.routes must define a 'conversation' route")
        for name, chain in self.routes.items():
            if not chain:
                raise ValueError(f"ai.routes.{name} must list at least one provider")
        return self


class HomeAssistantConfig(BaseModel):
    enabled: bool = True
    # Dry-run is on by default: speaker actions are described, not performed.
    # Turn off here (or in Settings) once you have checked what JARVIS would do.
    dry_run: bool = True
    url: str | None = None  # e.g. "http://homeassistant.local:8123"
    token_env: str = "HA_TOKEN"
    verify_ssl: bool = True
    timeout_s: float = Field(10, gt=0, le=60)
    # TTS entity used for Sonos announcements, e.g. "tts.piper". Auto-detected when unset.
    tts_entity: str | None = None
    # Volume changes above this level (0-100) require confirmation.
    max_volume_without_confirmation: int = Field(60, ge=0, le=100)
    # The only Home Assistant services JARVIS may ever call.
    allowed_services: list[str] = Field(
        default_factory=lambda: [
            "media_player.media_play",
            "media_player.media_pause",
            "media_player.media_stop",
            "media_player.media_next_track",
            "media_player.media_previous_track",
            "media_player.volume_set",
            "media_player.play_media",
        ]
    )

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str | None) -> str | None:
        if v is None or v.strip() == "":
            return None
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("homeassistant.url must start with http:// or https://")
        return v


class AppEntry(BaseModel):
    command: list[str] = Field(min_length=1)
    description: str = ""


def _default_apps() -> dict[str, AppEntry]:
    return {
        "notepad": AppEntry(command=["notepad.exe"], description="Notepad"),
        "calculator": AppEntry(command=["calc.exe"], description="Calculator"),
        "explorer": AppEntry(command=["explorer.exe"], description="File Explorer"),
    }


class ComputerConfig(BaseModel):
    # Whether the computer-control tools are offered to the AI at all.
    # None = automatic: on for Windows, off elsewhere (for example on a
    # Raspberry Pi backend, where there is no desktop to control).
    enabled: bool | None = None
    # Dry-run is on by default: actions are described, not performed.
    dry_run: bool = True
    allowed_apps: dict[str, AppEntry] = Field(default_factory=_default_apps)


class PrivacyConfig(BaseModel):
    # Never use cloud providers, even if configured.
    local_only: bool = False
    # Highest sensitivity of tool output that may be sent to a cloud model.
    cloud_max_sensitivity: Sensitivity = "normal"
    # Master switches. With the microphone switch off, push-to-talk cannot open
    # the microphone. The camera switch gates a future module.
    microphone_enabled: bool = False
    camera_enabled: bool = False


class VoiceEffectsConfig(BaseModel):
    """The "JARVIS filter" applied after speech synthesis (see jarvis/voice/effects.py)."""

    enabled: bool = False
    pitch_semitones: float = Field(-1.5, ge=-6, le=3)  # lower = deeper; tempo is unchanged
    highpass_hz: float = Field(80, ge=20, le=300)
    warmth_db: float = Field(1.0, ge=-6, le=6)  # around 180 Hz
    presence_db: float = Field(3.0, ge=-6, le=9)  # around 3 kHz: clarity
    air_db: float = Field(2.0, ge=-6, le=9)  # above 8 kHz
    sheen_mix: float = Field(0.12, ge=0, le=0.6)  # subtle chorus: "digital sheen"
    reverb_wet: float = Field(0.08, ge=0, le=0.5)
    reverb_room_size: float = Field(0.15, ge=0, le=1)
    output_gain: float = Field(1.0, ge=0.1, le=2)


class AzureSpeechConfig(BaseModel):
    """Microsoft Azure neural voices (cloud). Used only for voices named "azure:...".

    The key and region are secrets read from the environment / .env. Text sent
    for synthesis leaves this machine; audio from the microphone never does.
    """

    key_env: str = "AZURE_SPEECH_KEY"
    region_env: str = "AZURE_SPEECH_REGION"
    # Stop using Azure for the rest of the month after this many characters
    # (the free F0 tier allows 500,000 neural characters a month). Piper takes over.
    monthly_char_limit: int = Field(450_000, ge=0)
    timeout_s: float = Field(8, gt=0, le=30)


class PersonaConfig(BaseModel):
    """Speaking style, added after the system prompt. It never changes the safety
    or tool rules, which always take precedence."""

    enabled: bool = True
    name: str = "JARVIS"
    style: str = (
        "You are a refined, calm AI butler. Be polite, composed and brief. "
        "Address the user as \"sir\" now and then (in Swedish too: \"sir\"), not in every sentence. "
        "A dry, understated wit is welcome when it fits, but never at the expense of clarity, "
        "and never when something failed or the user is in a hurry. No exclamation marks, no gushing."
    )


class VoiceConfig(BaseModel):
    """Push-to-talk voice input and local speech output (Phase 2)."""

    # None = automatic: on when the voice packages and models are installed.
    enabled: bool | None = None
    # Microphone used unless another is chosen in Settings. None = system default.
    input_device: str | None = "Microphone (Realtek(R) Audio)"
    # The microphone closes after this many seconds even if the button is held.
    max_capture_s: float = Field(30, ge=2, le=120)
    # Whisper model: a name under data/models/whisper, or an absolute path.
    stt_model: str = "large-v3-turbo"
    stt_device: Literal["auto", "cuda", "cpu"] = "auto"
    stt_compute_type: str = "int8_float16"  # used on the GPU; the CPU uses int8
    stt_languages: list[Literal["sv", "en"]] = Field(default_factory=lambda: ["sv", "en"], min_length=1)
    # Voices per reply language. Formats:
    #   "sv_SE-nst-medium" or "piper:<name>[#<speaker>]"  local Piper voice under data/models/piper
    #   "kokoro:<voice>"                                  local Kokoro voice (English only), e.g. kokoro:bm_george
    #   "azure:<voice>"                                   Azure neural voice (cloud), e.g. azure:en-GB-RyanNeural
    tts_voice_sv: str | None = "sv_SE-nst-medium"
    # English replies use this voice when it is available, else the Swedish one.
    tts_voice_en: str | None = "en_GB-alan-medium"
    # Local voices used automatically if a cloud voice fails or its monthly limit is reached.
    tts_fallback_sv: str = "sv_SE-nst-medium"
    tts_fallback_en: str = "en_GB-alan-medium"
    effects: VoiceEffectsConfig = Field(default_factory=VoiceEffectsConfig)
    azure: AzureSpeechConfig = Field(default_factory=AzureSpeechConfig)
    # Where models are stored. Default: <data_dir>/models.
    models_dir: Path | None = None


class RetentionConfig(BaseModel):
    max_conversation_messages: int = Field(60, ge=4, le=1000)
    tool_history_size: int = Field(200, ge=10, le=5000)
    # Phase 1 keeps conversations in memory only. Nothing is written to disk.
    persist_conversations: Literal[False] = False


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    to_file: bool = True
    # Message text is not logged unless explicitly enabled.
    log_conversation_content: bool = False


class ProfileConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    name: str = Field(min_length=1, max_length=64)
    role: Role


class JarvisConfig(BaseModel):
    assistant_name: str = "JARVIS"
    # Preferred reply language: "auto" follows the user, or "sv" / "en".
    language: Literal["auto", "sv", "en"] = "auto"
    data_dir: Path = PROJECT_ROOT / "data"
    server: ServerConfig = Field(default_factory=ServerConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    homeassistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig)
    computer: ComputerConfig = Field(default_factory=ComputerConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    profiles: list[ProfileConfig] = Field(
        default_factory=lambda: [ProfileConfig(id="owner", name="Owner", role="owner")]
    )
    default_profile: str = "owner"

    @model_validator(mode="after")
    def _profiles_valid(self) -> "JarvisConfig":
        ids = [p.id for p in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("profile ids must be unique")
        if self.default_profile not in ids:
            raise ValueError(f"default_profile '{self.default_profile}' is not a defined profile")
        if not any(p.role == "owner" for p in self.profiles):
            raise ValueError("at least one profile must have role 'owner'")
        if not self.data_dir.is_absolute():
            self.data_dir = (PROJECT_ROOT / self.data_dir).resolve()
        return self

    @property
    def models_dir(self) -> Path:
        d = self.voice.models_dir
        if d is None:
            return self.data_dir / "models"
        return d if d.is_absolute() else (PROJECT_ROOT / d).resolve()

    def profile(self, profile_id: str) -> ProfileConfig | None:
        return next((p for p in self.profiles if p.id == profile_id), None)


def load_config(path: Path | str | None = None, *, env: dict[str, str] | None = None) -> JarvisConfig:
    """Load and validate configuration.

    A missing config file is not an error: defaults are used so the app can
    start on a fresh checkout. An invalid file is an error with a readable message.
    """
    env = dict(os.environ) if env is None else env
    if path is None:
        path = env.get("JARVIS_CONFIG") or DEFAULT_CONFIG_PATH
    path = Path(path)

    raw: dict = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    # Environment overrides for values people commonly change per machine.
    if env.get("JARVIS_HOST"):
        raw.setdefault("server", {})["host"] = env["JARVIS_HOST"]
    if env.get("JARVIS_PORT"):
        raw.setdefault("server", {})["port"] = env["JARVIS_PORT"]
    if env.get("HA_URL"):
        raw.setdefault("homeassistant", {})["url"] = env["HA_URL"]

    try:
        return JarvisConfig.model_validate(raw)
    except ValidationError as exc:
        lines = [f"{path if path.exists() else 'defaults'}: configuration is invalid:"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc
