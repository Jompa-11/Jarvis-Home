# JARVIS architecture (Phase 1)

```
 ┌──────────────── Desktop interface (jarvis/web) ────────────────┐
 │ HUD · conversation · tool activity · confirmations · settings  │
 └──────────────▲──────────────────────────────┬──────────────────┘
        WebSocket events (state, tools)   REST (token-protected)
 ┌──────────────┴──────────────────────────────▼──────────────────┐
 │ Local server (jarvis/server/app.py) · 127.0.0.1 only           │
 └──────────────────────────────┬─────────────────────────────────┘
 ┌──────────────────────────────▼─────────────────────────────────┐
 │ JARVIS Core (jarvis/core)                                      │
 │  Orchestrator ── Conversation (append-only, in memory)         │
 │      │                                                         │
 │      ├─► ProviderRouter ─► Anthropic │ Ollama │ Dev fallback   │  reasoning
 │      │      privacy filter · budget · per-category routes      │
 │      │                                                         │
 │      └─► ToolExecutor: validate ► permission ► confirm ► run   │  execution
 │             ToolRegistry · PermissionPolicy · ConfirmationMgr  │
 └──────┬──────────────────┬──────────────────┬───────────────────┘
        ▼                  ▼                  ▼
  Home Assistant      Windows control     System tools
  (REST, allowlist)   (allowlisted apps,  (time, status)
  └─ Sonos service     window focus)
```

## Layers

**A. Desktop application** — `jarvis/web` (vanilla HTML/CSS/JS, no build step) served by FastAPI.
`python -m jarvis --desktop` wraps it in a native window with pywebview (Edge WebView2). The UI only
displays what the server reports: if no voice engine exists, the microphone shows *Off*.

**B. Core** — `jarvis/core/orchestrator.py` runs one turn: append the user message, ask the router for a
model response, and pass each proposed tool call to the executor. Actual results go back to the model,
up to `ai.max_tool_rounds`. The model never executes anything. It can only name a registered tool with
JSON arguments, and those arguments are validated against that tool's JSON schema (`additionalProperties: false`).

**C. Hybrid AI** — `jarvis/providers`. `AIProvider` has two methods (`health`, `complete`) over neutral
types (`ChatMessage`, `ToolCall`). Implementations:

* `anthropic` — Claude through the official SDK. Default `claude-opus-5-5` at `effort: low` to keep
  costs down, automatic prompt caching, and server-side refusal fallback where supported. Claude's
  assistant turns (including thinking blocks) are stored and replayed unchanged.
* `ollama` — any local model with tool support (default `qwen2.5:3b`).
* `dev` — a rule-based intent matcher (EN/SV), **not** an AI model. It exists so the whole pipeline can
  run and be tested without any model. Every reply it gives is labelled.

Routes are configured per category (`conversation`, `smart_home`, `computer`). The category is chosen
locally by keywords, so routing costs no model call. A provider is skipped when it is unhealthy, when
local-only mode is on (cloud), or when the monthly budget is reached (cloud). A turn stays on the provider
that answered its first round.

**D. Integrations** — `jarvis/integrations/homeassistant`. The REST client refuses any service not on the
allowlist. `SonosService` discovers speakers with `integration_entities('sonos')`, gets room names from
areas, and gates every action on `supported_features`. It then reads state back to set
`verified: true/false`. Announcements and track skips cannot be verified, so they report `verified: null`.

**E. Computer control** — `jarvis/computer/windows.py`. Registered only where it can work
(`computer.enabled`, automatic = Windows only), so a headless backend such as a Raspberry Pi never offers
these tools to the model; the rest of the backend is platform-neutral. It can launch apps from a config allowlist
(the model picks a key; the command line comes from config; `shell=False`), list window titles, and focus
a window. Dry-run is on by default. There is no shell, no file access, no input injection and no elevation.

**F. Monitoring and notifications** — `jarvis/monitoring`. `AsyncScheduler` implements the `Scheduler`
extension point (periodic, daily and one-off jobs; a failing job is logged and retried; `stop()` cancels all
tasks when JARVIS shuts down). `Notifier` decides how each event is delivered from its priority
(`low` / `normal` / `high` / `urgent`) and the owner's rules in `config/rules.toml`: quiet hours, a limit
on interrupting notifications per hour, mute and pause; `urgent` may break through quiet hours. The same
event is never delivered twice; only a hash of the event key and a timestamp are stored
(`data/monitor_state.json`, retention configurable). One-time codes (4–8 digits, also written `483 920`)
are replaced with `••••` before a notification is created, so no channel, the UI or the summary sees them.

Channels implement `NotificationChannel`: the UI list (passive) and Windows notifications (interrupting)
today; voice and phone channels plug in without changing the Notifier. **The model has no tool that
sends notifications.** Only code calls `Notifier.notify`, with fixed templates; tools the model can use
here (`rules_show`, `monitor_status`) are read-only and their output is `personal`.

**Windows notifications** are shown by running a *fixed* PowerShell script (`_TOAST_SCRIPT` in
`jarvis/monitoring/channels.py`, passed with `-EncodedCommand`). The notification title and text are
**never part of the command**: they are passed only as environment variables (`JARVIS_TOAST_TITLE`,
`JARVIS_TOAST_BODY`), and the script XML-escapes them with `[Security.SecurityElement]::Escape` before
building the toast XML. PowerShell inserts variable values literally and does not evaluate them, so text
from an e-mail such as `'); Remove-Item ...` or `$(calc)` is displayed as text and cannot run as code or
inject markup. The model cannot reach this path: it has no tool that creates notifications, and the
script, its arguments and the app id are constants. A test asserts that hostile text never appears in
the process arguments.

## Safety model

| Concern | Mechanism |
|---|---|
| Model text becoming commands | Only registered tools; JSON-schema validation; no shell tool exists |
| Over-reach | Roles (owner/adult/child/guest) → allowed categories + max risk; tools a role can't use aren't even offered |
| Consequential actions | Risk ≥ MEDIUM (or above the role's auto-approve level) → explicit UI confirmation; timeout = deny; cancelling a turn denies its pending confirmations |
| Destructive actions | `Risk.HIGH`: always confirmed, owner only (none exist yet) |
| Prompt injection via tool output | System prompt treats tool output as data; approvals come only from the local UI, never from a tool |
| Other websites driving the local API | Loopback bind, Host check (DNS rebinding), per-launch token in page (CSRF), WebSocket Origin check, strict CSP |
| Secrets | Env vars / `.env` / Windows Credential Manager; never in config; every secret read is registered with the log redactor; pattern redaction for tokens/keys |
| Sensitive data to cloud | Tool outputs have a sensitivity (`normal`/`personal`/`restricted`); above `cloud_max_sensitivity` they are replaced with a "withheld" notice for cloud providers only |
| Retention | Conversations in memory only; logs contain no message text by default; usage file stores counts only; monitoring stores hashes of notified event keys and times only |
| Notifications | No model tool can send them; fixed templates; one-time codes masked; Windows notification text passed only as environment variables and XML-escaped by a fixed script |
| Identity | Profiles scope permissions but are *not* authentication. Voice/face identity (future) will be a hint, never authorization |

## Extension points

| Phase | Where | Contract |
|---|---|---|
| 2 Voice | `jarvis/voice/interfaces.py` | `AudioCapture`, `WakeWordDetector`, `SpeechToText`, `TextToSpeech`, `AudioPlayback`; `VoiceController` owns mic state |
| 2/3 Desktop control, gestures | `jarvis/computer`, `extensions.GestureRecognizer` | gestures map to registered tools, so they get the same permission gate |
| 4 Identity | `extensions.IdentityProvider` | consent-based enrollment, `forget()` deletes biometrics |
| 5 Calendars | `extensions.CalendarProvider` | register tools with `output_sensitivity="personal"` |
| 6–7 Smart home | add services to the allowlist + new tools | same executor |
| 8 Memory | `extensions.MemoryStore` | per-profile, user-deletable |
| 9 Reminders/planning | `extensions.Scheduler` | |

A new capability = a module that registers `ToolSpec`s. The core does not change.

## Decisions (reversible)

* **Python + FastAPI + vanilla web UI** instead of Electron/Tauri: one runtime, no build chain, and an easy
  path to local speech libraries (Phase 2). pywebview gives a native window when wanted.
* **Home Assistant REST API** (not WebSocket) for Phase 1: simpler, enough for request/response control.
  Live state push from HA can move to its WebSocket API later.
* **Default cloud model `claude-opus-5-5` at low effort**, with a budget cap. Per-category routes let you
  move simple device commands to `claude-haiku-5-5` or to Ollama.
* **Settings precedence**: `config/jarvis.toml` provides defaults; only values changed in the UI are stored in
  `data/settings.json` and override them.
* **Pinned dependencies**: `requirements*.lock` (universal, hash-checked) for reproducible Windows and Pi installs.
* **No persistence of conversations** until a memory design with retention controls exists (Phase 8).
