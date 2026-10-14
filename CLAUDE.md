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
- Computer dry-run is OFF (`computer.dry_run = false`): the owner decided on 2026-10-14 that actions on the
  PC happen for real. Keep `homeassistant.dry_run` on until the owner says otherwise. Ordinary owner actions
  run without asking; deleting, shutting down and PowerShell are `Risk.HIGH` and always ask.
- Secrets live only in `.env` (git-ignored) or Windows Credential Manager — never in code, config, logs,
  commits or chat. Never print `.env`.
- The model may only act through registered tools (`jarvis/core/tools.py`). New tools declare risk, category,
  output sensitivity and dry-run support.
- ONE shell tool exists, by the owner's explicit decision on 2026-10-14: `shell_run_powershell`
  (`jarvis/computer/shell.py`). Do not loosen any of its guards: off until `advanced_mode` is on in
  Settings, `Risk.HIGH` so the exact command is shown and approved every single time, the owner's normal
  user and never elevated, 60 s timeout, 4000 characters of output, every run logged, a blocklist
  (firewall, network, Defender, ExecutionPolicy, download-and-run, .env, Credential Manager), and
  `blocked_after_untrusted`: refused for the rest of any turn in which JARVIS read e-mail, files, the
  clipboard, window titles or a summary. Never add a second shell/exec tool.
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
- `jarvis/monitoring/` scheduler, notifier (priorities, quiet hours), channels, summary, game mode.
  `jarvis/mail/` Gmail read-only (rules first, local model only). `jarvis/files/` metadata-only file index.
- `jarvis/core/tool_select.py` shows the model only the tool groups a request is about (security unchanged:
  the executor checks every call against the full registry). Personal tools (mail, files, notifications,
  summary, rules) are category `personal`: owner role only.

## Status (2026-10-14)

- Runs on the owner's PC (Python 3.13.15, RTX 4060): all 356 tests pass; Ollama `qwen2.5:7b` runs 100% on
  GPU with `num_ctx` 8192.
- Merged: monitoring + rules (PR #8, Gmail read-only signed in and active); PR #9: file search + folder
  watching (Hämtade filer/Dokument via Known Folders), morning summary + "Sammanfatta nu", per-request tool
  selection, owner-only personal tools, game mode (`cs2.exe` or GPU > 85 % for 2 min: no background
  Ollama calls, model unloaded).
- Draft, not merged: Discord phone notifications (PR #10, `claude/jarvis-discord-notify`).
- Branch `claude/jarvis-autostart` (draft PR): desktop app `pythonw -m jarvis --background` (pywebview window
  maximized on the primary monitor, X hides, pystray icon, single instance, supervised server child with
  max 5 restarts/h), `scripts/autostart.ps1 -Install/-Uninstall/-Status` (Startup + Start-menu shortcuts with
  AppUserModelID `JarvisHome.JARVIS`), icon `assets/jarvis.ico` (`scripts/make_icon.py`). The owner explicitly
  asked for autostart and a pinned app; keep it user level (no admin, service, HKLM, firewall).
- Lock files: `proxy-tools` (a pywebview dependency, sdist only) is the one package allowed from source
  (`--no-binary proxy-tools`, hash-pinned); everything else is wheels only.
- `toolcheck` (qwen2.5:7b, 3 runs each): step 4 alone 6–7/8 (more tools made qwen skip `computer_list_apps`
  and answer the time from context); with tool selection 8/8 ×3; with step 5 (10 cases) 10, 10, 9/10;
  final run 10/10. Small models are sensitive to the number of tools: measure with `toolcheck` after adding
  tools. `toolcheck` refuses to run during game mode (results would be GPU timeouts).
- Voice (PR #6, #7) is paused as drafts; monitoring must not depend on it.
- Not configured yet: Claude API key (owner must set a spend limit in the Anthropic Console first),
  Home Assistant + Sonos (planned on the Raspberry Pi).

## Git workflow

Work on a branch, open a PR into `main`, and merge only when tests pass. Keep commits focused.
