# J.A.R.V.I.S. — Home Intelligence

A personal, privacy-first home assistant for Windows 11. Phase 1 has the core
orchestrator, hybrid AI (Claude in the cloud, Ollama locally), Home Assistant /
Sonos control, a restricted Windows control layer, and a holographic HUD
interface. Every indicator in the interface shows real backend state.

![status](https://img.shields.io/badge/phase-1-5fe3ff) ![tests](https://img.shields.io/badge/tests-131%20passing-5fe3ff)

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

With neither secret set, JARVIS still runs: it uses Ollama if installed, or else the dev fallback,
which is clearly labelled in every reply.

Optional local model: install [Ollama](https://ollama.com) and run `ollama pull qwen2.5:3b`.

Home Assistant and Sonos setup: see [docs/HOME_ASSISTANT_SETUP.md](docs/HOME_ASSISTANT_SETUP.md).

## Using it

Try “list speakers”, “set the kitchen to 30 percent”, “pause the living room”, “play Morning Jazz in the kitchen”,
“announce dinner is ready in the kitchen”, “open notepad”, “vad är klockan?”.

* **Tool activity** (right) lists every action with its real result. A “verified” mark means JARVIS re-read
  the device and confirmed the effect.
* **Authorization required** appears for consequential actions: announcements, opening apps, volume above
  60%. Nothing runs until you click Approve; requests expire after 2 minutes.
* **Dry-run** is on by default for both computer control and speakers: actions are described, not performed.
  Turn it off per area in Settings (or `config\jarvis.toml`) once you are satisfied.
* **Settings** (gear): dry-run toggles, local-only mode, what cloud AI may receive, microphone/camera master
  switches, household profile, read-aloud.

## Development

```bash
pip install --require-hashes -r requirements-dev.lock
python -m pytest            # 131 tests; no network, no hardware, no credentials needed
python tests/fake_ha.py     # fake Home Assistant on :8123 (token "demo-token") for UI testing
```

Dependencies: `requirements*.txt` list the direct dependencies with minimum versions; `requirements*.lock`
pin every package with SHA-256 hashes, for Windows and Linux (including Raspberry Pi) from one file.
After changing a `.txt` file, regenerate the lock with the command at the top of the lock file
(needs [uv](https://docs.astral.sh/uv/)), run the tests, and commit both files.

Architecture, security model and extension points: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
