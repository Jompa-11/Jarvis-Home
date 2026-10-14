# J.A.R.V.I.S. — Home Intelligence

A personal, privacy-first home assistant for Windows 11. Phase 1 has the core
orchestrator, hybrid AI (Claude in the cloud, Ollama locally), Home Assistant /
Sonos control, a restricted Windows control layer, and a holographic HUD
interface. Every indicator in the interface shows real backend state.

![status](https://img.shields.io/badge/phase-1-5fe3ff) ![tests](https://img.shields.io/badge/tests-376%20passing-5fe3ff)

## What works today

| Area | Status |
|---|---|
| Text conversation with context | ✅ Claude (with API key), Ollama (if running), or a labelled rule-based dev fallback |
| Tool calls: validation → permission → confirmation → execution → verified result | ✅ |
| Sonos via Home Assistant: discover, rooms, state/volume, play/pause/stop/next, volume, favorites, announcements | ✅ Tested against a fake Home Assistant. Not yet tested against your real speakers. Dry-run by default |
| Windows: open allowlisted apps, list/focus windows | ✅ Dry-run by default. Offered only on Windows, so the backend runs unchanged on Linux / Raspberry Pi |
| Privacy: local-only mode, sensitive data kept from cloud, no stored conversations, redacted logs | ✅ |
| Monthly cloud budget guard and cost estimates | ✅ |
| Voice (wake word, speech-to-text, mic capture) | ⏳ Interfaces only (Phase 2). The mic shows **Off** because it *is* off |
| Read replies aloud | ✅ Optional, using local Windows voices through the browser |
| Monitoring and notifications: priorities, quiet hours, rate limit, mute/pause, Windows pop-ups | ✅ Rules in `config\rules.toml`; one-time codes shown as `••••` |
| Gmail, read-only: important mail by your rules first, other mail classified by the **local** model only | ✅ After `python -m jarvis mail-login`; token in Windows Credential Manager |
| File search and folder watching (Hämtade filer, Dokument): metadata only, never file contents | ✅ "hitta min senaste faktura", "öppna mappen där den ligger" (dry-run by default) |
| Summary: important mail, new files and what waited, every morning or on demand ("Sammanfatta nu") | ✅ Shown in the notification panel until acknowledged |
| Game mode: no background AI calls on the GPU while you play (e.g. `cs2.exe`, or GPU > 85 % for 2 min) | ✅ Mail that needs the model waits; rule matches still notify. "Spelläge: på" in the panel |
| Phone notifications via Discord (outgoing only, important + urgent) | ✅ After `DISCORD_WEBHOOK_URL` in `.env`; see below |
| Camera / gestures / identity / calendars / memory | ⏳ Interfaces only |

## Quick start (Windows 11)

Python 3.11 or newer is required; 3.13 is recommended (`winget install --id Python.Python.3.13 -e`).
Clone into a folder that OneDrive does **not** sync (your `.env` holds secrets).

```powershell
git clone https://github.com/Jompa-11/Jarvis-Home.git C:\Users\<you>\dev\Jarvis-Home
cd C:\Users\<you>\dev\Jarvis-Home
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # allow the scripts in this window only
.\scripts\setup.ps1 -Dev       # creates .venv, installs pinned dependencies, creates config\jarvis.toml and .env
.\scripts\start.ps1            # opens http://127.0.0.1:8765 in your browser
```

After setup you can also double-click `start-jarvis.bat`.

Other commands:

```powershell
.\scripts\setup.ps1 -Desktop        # also install pywebview, then:
.\scripts\start.ps1 -Desktop        # native window (Edge WebView2) instead of a browser tab
.\.venv\Scripts\python -m jarvis check      # validate config, show what's connected
.\.venv\Scripts\python -m jarvis diagnose   # hardware, mics, cameras, tools, credentials present (never values)
.\scripts\inspect_environment.ps1          # same idea, no Python needed
.\.venv\Scripts\python -m jarvis toolcheck  # does the local AI model use JARVIS's tools? (dry-run, free)
.\.venv\Scripts\python -m jarvis toolcheck --model llama3.1:8b   # compare another Ollama model
```

## Configuration

* `config\jarvis.toml` holds settings (copied from `config\jarvis.example.toml`). It is git-ignored.
  Settings you change in the app's Settings panel are saved in `data\settings.json` and take precedence;
  switching one back to its configured value hands control back to `jarvis.toml`.
* `.env` holds secrets (copied from `.env.example`). It is git-ignored. Instead of `.env`, you can use
  real environment variables or the Windows Credential Manager (`pip install keyring`, service `jarvis`).

| Secret | Needed for | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Cloud AI (Claude) | https://console.anthropic.com → API keys |
| `HA_TOKEN` | Home Assistant / Sonos | Home Assistant → your profile → Security → Long-lived access tokens |
| `DISCORD_WEBHOOK_URL` | Phone notifications | Your private Discord server → Integrations → Webhooks (see below) |

With neither secret set, JARVIS still runs: it uses Ollama if installed, or else the dev fallback,
which is clearly labelled in every reply.

Optional local model: install [Ollama](https://ollama.com) and run `ollama pull qwen2.5:3b`.

Home Assistant and Sonos setup: see [docs/HOME_ASSISTANT_SETUP.md](docs/HOME_ASSISTANT_SETUP.md).

* `config\rules.toml` (git-ignored, copied from `config\rules.example.toml`) holds what is important to
  you: senders, domains and words for mail, words in new file names (`[files] important_keywords`),
  quiet hours, the morning summary time and style preferences. Rules set priorities only, never permissions.
* `[files]` in `config\jarvis.toml` chooses the watched folders. The default is your Downloads and Documents
  folders (found through Windows Known Folders). JARVIS indexes only name, path, type, size and modified
  time into `data\files.db` every 15 minutes and never opens a file. Secret-looking files (`.env`, keys,
  certificates, password databases, VPN profiles), `.ssh`/`.git`/`node_modules`/`AppData`, hidden and
  system files, and links/junctions are skipped.
* `[gaming]` in `config\jarvis.toml`: `process_names` (default `["cs2.exe"]`, add your games) and
  `gpu_threshold_percent` / `gpu_high_minutes`. While game mode is on, the local model is unloaded from the
  GPU, mail classification waits until the game closes, and `toolcheck` refuses to run.
* Mail, files, notifications, the summary and your rules are **owner only**: other profiles are neither
  offered those tools nor shown notifications.

## Phone notifications via Discord

JARVIS can post **important and urgent** notifications to a private Discord server, so they reach your phone.
It is outgoing only: JARVIS posts through a webhook and never reads anything from Discord. The AI model has
no tool that sends; only the notification code does, with fixed texts.

1. **Create a private server.** In the Discord app: **+** (Add a Server) → **Create My Own** → **For me and my
   friends** → name it e.g. `JARVIS`. Do not invite anyone.
2. **Create the webhook.** Right-click the server (or tap its name) → **Server Settings** → **Integrations** →
   **Webhooks** → **New Webhook** → name `JARVIS`, channel `#general` → **Copy Webhook URL**.
3. **Put it in `.env`** (never in config, chat or git). Open `.env` in Notepad and add one line:
   `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...` (the URL you copied). Save. The URL is a password:
   anyone with it can post in that channel. If it leaks, delete the webhook in Discord and make a new one.
4. **Phone:** install Discord, sign in, open the server → long-press it → **Notification Settings** →
   **All Messages**. Allow notifications for Discord in the phone's settings.
5. **Test:** `.\.venv\Scripts\python.exe -m jarvis discord-test` (sends a fixed test message), or
   **Skicka testnotis** in the notification panel.

What is sent (`[discord]` in `config\jarvis.toml`):

* Only `high` and `urgent`; at most `max_per_hour` (5) per hour. Quiet hours, mute, pause and **Pausa Discord**
  apply; urgent may break through quiet hours (`urgent_overrides_quiet_hours`). Game mode does not stop Discord.
* `phone_detail = "minimal"` (default): type and sender/domain or file name, e.g.
  "Viktigt mejl från Skatteverket (skatteverket.se)". `"summary"` adds one sentence for mail.
  Mail text and file paths are never sent; one-time codes are shown as `••••`; mentions are disabled.
* Urgent starts with "🚨 AKUT" in a red box; important is blue. The morning summary sends only its counts.

## Using it

Try “list speakers”, “set the kitchen to 30 percent”, “pause the living room”, “play Morning Jazz in the kitchen”,
“announce dinner is ready in the kitchen”, “open notepad”, “vad är klockan?”, “hitta min senaste faktura”,
“öppna mappen där den ligger”, “har jag fått några viktiga mejl?”, “sammanfatta dagen”.

* **Tool activity** (right) lists every action with its real result. A “verified” mark means JARVIS re-read
  the device and confirmed the effect.
* **Authorization required** appears for consequential actions: announcements, opening apps, volume above
  60%. Nothing runs until you click Approve; requests expire after 2 minutes.
* **Dry-run** is on by default for both computer control and speakers: actions are described, not performed.
  Turn it off per area in Settings (or `config\jarvis.toml`) once you are satisfied.
* **Notiser** (notification panel): pause, mute for an hour, acknowledge, and **Sammanfatta nu** for a summary
  of important mail, new files and what waited. The latest summary stays at the top until you acknowledge it.
* **Settings** (gear): dry-run toggles, local-only mode, what cloud AI may receive, microphone/camera master
  switches, household profile, read-aloud.

## Development

```bash
pip install --require-hashes -r requirements-dev.lock
python -m pytest            # 376 tests; no network, no hardware, no credentials needed
python tests/fake_ha.py     # fake Home Assistant on :8123 (token "demo-token") for UI testing
```

Dependencies: `requirements*.txt` list the direct dependencies with minimum versions; `requirements*.lock`
pin every package with SHA-256 hashes, for Windows and Linux (including Raspberry Pi) from one file.
After changing a `.txt` file, regenerate the lock with the command at the top of the lock file
(needs [uv](https://docs.astral.sh/uv/)), run the tests, and commit both files.

Architecture, security model and extension points: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
