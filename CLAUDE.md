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

## Status (2026-10-15)

- Runs on the owner's PC (Python 3.13.15, RTX 4060, 8 GB): 546 tests pass.
- Merged: PR #8 (rules + monitoring, Gmail read-only), PR #9 (file search, folder watching, summary,
  per-request tool selection, owner-only personal tools, game mode), PR #11 (desktop app: own window,
  tray icon, single instance, supervised server, autostart shortcuts).
- Draft, not merged: Discord phone notifications (PR #10). PR #12 `claude/jarvis-obey`: PC control
  (any installed program, web, folders, windows, volume, media keys, typing, clipboard, file actions,
  power), advanced PowerShell mode, and the fast path below.
- **Fast path** (`jarvis/core/fastpath.py`): everyday commands are answered by local rules, never the
  model, and marked SNABB. It goes through the executor, so permissions, confirmation, the
  untrusted-data rules and dry-run are unchanged. `ai.fast_path = false` turns it off; `toolcheck`
  turns it off by itself because it measures the model.
- **Injection defence** (PR #12 review): untrusted text in the conversation makes EVERY non-READ tool
  ask for confirmation, not just PowerShell, and the taint lives as long as that text is in the chat.
  Opening a shell (powershell, cmd, wt, regedit, ...) is HIGH on its own; win+r and win+x are refused;
  typing or pressing Enter into a console or the Run dialog is HIGH. See `tests/test_injection_chains.py`.
- Routes: **ollama first** in every category (the owner has no paid API), then anthropic, then dev.
  `ollama.think = false`, so a reasoning model does not think before a tool call.
- Local model comparison (2026-10-15, 18 cases, 3 runs each, measured not guessed):

  | Model | Score | Median reply | GPU memory |
  |---|---|---|---|
  | qwen2.5:7b | 17/18 ×3 | 1.1 s | 4863 MiB |
  | qwen3:8b | 17/18 ×3 | 1.4 s | 6025 MiB |

  Same score; qwen2.5:7b is faster and leaves 1.2 GB more for Whisper later, so it stays. Both miss
  only "Byt till Chrome" (Chrome is not installed, and the fast path handles that phrase anyway).
  Small models are sensitive to the number of tools: measure with `toolcheck` after adding tools.
- Voice (PR #6, #7) is paused as drafts. The plan is Chatterbox Multilingual (MIT, Swedish, PerTh
  watermark) on the CPU, since it needs ~3.5 GB of GPU memory and the model already uses ~4.9 GB of 8 GB.
  Piper (`sv_SE-nst-medium`, CC0 data, KBLab) stays as the fast fallback and is used in game mode.
- Not configured yet: Claude API key, Home Assistant + Sonos (planned on the Raspberry Pi).

## Git workflow

Work on a branch, open a PR into `main`, and merge only when tests pass. Keep commits focused.
