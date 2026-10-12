# CLAUDE.md — JARVIS Home

Personal home assistant: Python backend (FastAPI) + vanilla web UI, run locally on the owner's
Windows 11 PC first, later moved to a Raspberry Pi 4 (backend only). The owner writes in Swedish;
answer in Swedish and give exact PowerShell commands.

## Commands (Windows, from the repo root)

```powershell
.\.venv\Scripts\python.exe -m pytest                    # all tests; no network, keys or devices needed
.\.venv\Scripts\python.exe -m jarvis check              # config + what is connected
.\.venv\Scripts\python.exe -m jarvis diagnose           # hardware, mics, cameras, GPU
.\.venv\Scripts\python.exe -m jarvis toolcheck          # does the local model use tools? (dry-run, free)
.\.venv\Scripts\python.exe -m jarvis toolcheck --model llama3.1:8b
.\scripts\start.ps1                                     # UI at http://127.0.0.1:8765 (Ctrl+C stops)
```

Setup from scratch: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` then `.\scripts\setup.ps1 -Dev`.
Dependencies install from hash-pinned `requirements*.lock`. To change one, edit `requirements*.txt`,
regenerate the lock with the command at the top of the lock file (needs `uv`), and commit both.

## Safety rules (do not relax without the owner explicitly asking)

- The server binds to loopback only (`server.host` is validated); never expose it to the LAN or internet,
  never add firewall rules or port forwards.
- Keep dry-run on (`computer.dry_run`, `homeassistant.dry_run`) until the owner decides otherwise.
- Secrets live only in `.env` (git-ignored) or Windows Credential Manager — never in code, config, logs,
  commits or chat. Never print `.env`.
- The model may only act through registered tools (`jarvis/core/tools.py`); never add a shell/exec tool.
  New tools declare risk, category, output sensitivity and dry-run support.
- Do not spend money (Anthropic API calls) without the owner's go-ahead; Ollama is free.
- Don't change Windows system settings, install services, or enable virtualization without asking.

## Architecture (see docs/ARCHITECTURE.md)

- `jarvis/core/` orchestrator (model proposes tool calls → executor validates, checks permission,
  asks for confirmation, runs with timeout/dry-run), conversation, settings, permissions.
- `jarvis/providers/` Anthropic (Claude), Ollama (local), dev (rule-based fallback, labelled);
  router with per-category routes, privacy filter, budget guard.
- `jarvis/integrations/homeassistant/` REST client with service allowlist; Sonos via HA.
- `jarvis/computer/` Windows-only control (allowlisted apps, window focus); registered only on Windows,
  so the backend runs unchanged on Linux/Raspberry Pi.
- `jarvis/web/` UI; shows only real backend state. `tests/fake_ha.py` is a fake Home Assistant.

## Status (2026-10-12)

- Runs on the owner's PC (Python 3.13.15, RTX 4060): all tests pass; Ollama `qwen2.5:7b` runs 100% on GPU
  with `num_ctx` 8192.
- Open issue: `qwen2.5:7b` answered "which programs can you start?" from memory (invented Paint/Word)
  instead of calling `computer_list_apps`. PR #3 added grounding rules, an app enum and temperature 0.2,
  plus `toolcheck`. Next step: run `toolcheck`; if fewer than 7/8 pass, compare other Ollama models and
  pick the best, or add a deterministic intent fast-path before the model.
- Not configured yet: Claude API key (owner must set a spend limit in the Anthropic Console first),
  Home Assistant + Sonos (planned on the Raspberry Pi).

## Git workflow

Work on a branch, open a PR into `main`, and merge only when tests pass. Keep commits focused.
