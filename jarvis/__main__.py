"""Command-line entry point.

    python -m jarvis              start the server and open the interface in the browser
    python -m jarvis --desktop    start as the desktop app (own window, icon by the clock)
    pythonw -m jarvis --background [--startup]   the same without a console (autostart / pinned app)
    python -m jarvis check        validate configuration and show what is configured
    python -m jarvis diagnose     inspect this machine (hardware, devices, services)
    python -m jarvis toolcheck    check that the local AI model uses JARVIS's tools (dry-run)
    python -m jarvis mail-login --client-secret <file>   sign in to Gmail (read-only) in the browser
    python -m jarvis mail-logout  revoke the Gmail token and remove it from Credential Manager
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import webbrowser

from dotenv import load_dotenv

from .config import PROJECT_ROOT, ConfigError, JarvisConfig, load_config
from .logging_utils import setup_logging

log = logging.getLogger("jarvis")


def _check(cfg: JarvisConfig) -> int:
    from .app_core import JarvisCore

    async def run() -> dict:
        core = JarvisCore(cfg, persist=False)
        try:
            await core.refresh_integrations()
            return await core.snapshot()
        finally:
            await core.stop()

    snap = asyncio.run(run())
    print("Configuration: OK")
    print(f"  Data directory : {cfg.data_dir}")
    print(f"  Server         : http://{cfg.server.host}:{cfg.server.port}")
    print("  AI providers   :")
    for p in snap["ai"]["providers"]:
        mark = "available  " if p["available"] else "unavailable"
        print(f"    - {p['name']:<9} [{p['kind']:<5}] {mark} {p['detail']}")
    route = " -> ".join(r.provider + (f" ({r.model})" if r.model else "") for r in cfg.ai.routes["conversation"])
    print(f"  Conversation route : {route}")
    ha = snap["integrations"]["homeassistant"]
    print(f"  Home Assistant : {ha['state']} — {ha['detail']}")
    so = snap["integrations"]["sonos"]
    print(f"  Sonos          : {so['state']} — {so['detail']}")
    print(f"  Microphone     : {snap['voice']['mic_state']} (master switch {snap['voice']['master_switch']})")
    print(f"  Computer control: dry-run={'on' if snap['computer']['dry_run'] else 'off'}, platform supported={snap['computer']['platform_supported']}")
    return 0


def _watch_stdin_for_stop(server) -> None:
    """Child of the desktop app: when the app closes our stdin (Avsluta), stop cleanly."""

    def watch() -> None:
        try:
            while os.read(0, 1024):
                pass
        except OSError:
            return  # no usable stdin: never stop because of it
        log.info("Desktop app asked the server to stop")
        server.should_exit = True

    threading.Thread(target=watch, name="jarvis-stdin-watch", daemon=True).start()


def _serve(cfg: JarvisConfig, open_browser: bool, *, child: bool = False) -> int:
    import uvicorn

    from .app_core import JarvisCore
    from .server.app import create_app

    core = JarvisCore(cfg)
    # The desktop app passes the session token so its tray menu can call the API. It is
    # removed from the environment at once, so no process started from here inherits it.
    token = os.environ.pop("JARVIS_API_TOKEN", None) if child else None
    app = create_app(core, token=token)
    url = f"http://{cfg.server.host}:{cfg.server.port}/"
    server = uvicorn.Server(uvicorn.Config(
            app, host=cfg.server.host, port=cfg.server.port, log_level="warning", log_config=None,
            # Open UI websockets would otherwise keep a stop waiting until they disconnect.
            timeout_graceful_shutdown=3,
        ))

    if child:
        _watch_stdin_for_stop(server)
        server.run()
        return 0

    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"JARVIS running at {url}  (Ctrl+C to stop)")
    server.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS home assistant")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "diagnose", "toolcheck", "mail-login", "mail-logout"])
    parser.add_argument("--client-secret", help="mail-login: path to the OAuth client JSON (Desktop app) from Google Cloud")
    parser.add_argument("--forget-client", action="store_true", help="mail-logout: also remove the stored OAuth client")
    parser.add_argument("--config", help="Path to jarvis.toml")
    parser.add_argument("--desktop", action="store_true", help="Open in a native window (pywebview)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    parser.add_argument("--background", action="store_true", help="Desktop app without a console (use pythonw.exe); X hides the window")
    parser.add_argument("--startup", action="store_true", help="Started at login: wait ~10 s before showing the window")
    parser.add_argument("--serve-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--model", help="toolcheck: Ollama model to test (default: the configured one)")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    desktop_app = args.command == "run" and (args.desktop or args.background) and not args.serve_child
    setup_logging(
        cfg.logging.level,
        cfg.data_dir / "logs" if cfg.logging.to_file and args.command == "run" else None,
        filename="desktop.log" if desktop_app else "jarvis.log",
    )

    if args.command == "check":
        return _check(cfg)
    if args.command == "mail-login":
        from pathlib import Path

        from .mail.gmail import login

        print("Signing in to Gmail with READ-ONLY access (gmail.readonly). The token is stored in Windows Credential Manager.")
        address = login(Path(args.client_secret) if args.client_secret else None)
        print(f"Signed in as {address}. Restart JARVIS to start watching mail.")
        return 0
    if args.command == "mail-logout":
        from .mail.gmail import logout

        removed = logout(forget_client=args.forget_client)
        print("Gmail token revoked and removed from Credential Manager." if removed else "No Gmail token was stored.")
        return 0
    if args.command == "toolcheck":
        from .toolcheck import format_report, run_toolcheck

        from .monitoring.gaming import GamingDetector

        # Same check as game mode: never load the GPU while a game runs (results would be timeouts anyway).
        busy = GamingDetector(cfg.gaming).check_now()
        if busy:
            print(f"Game mode is on ({busy}): not running the tool-use check now. Try again when the game is closed.")
            return 3
        model = args.model or cfg.ollama.model
        print(f"Running tool-use check against Ollama model '{model}' (dry-run; nothing is changed)...")
        results = asyncio.run(run_toolcheck(cfg, model=model))
        if not results:
            print("No checks ran.")
            return 1
        print(format_report(results, model))
        return 0 if all(r.passed for r in results) else 1
    if args.command == "diagnose":
        from . import diagnostics

        envs = {"anthropic_api_key": cfg.anthropic.api_key_env, "home_assistant_token": cfg.homeassistant.token_env}
        print(json.dumps(diagnostics.collect(cfg.data_dir, envs), indent=2))
        return 0
    try:
        if args.serve_child:
            return _serve(cfg, False, child=True)
        if desktop_app:
            from .desktop.app import run_desktop

            return run_desktop(cfg, startup=args.startup)
        return _serve(cfg, cfg.server.open_browser and not args.no_browser)
    except KeyboardInterrupt:
        # Ctrl+C is the normal way to stop JARVIS; no traceback needed.
        print("JARVIS stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
