"""Command-line entry point.

    python -m jarvis              start the server and open the interface in the browser
    python -m jarvis --desktop    start in a native window (needs the optional 'pywebview' package)
    python -m jarvis check        validate configuration and show what is configured
    python -m jarvis diagnose     inspect this machine (hardware, devices, services)
    python -m jarvis toolcheck    check that the local AI model uses JARVIS's tools (dry-run)
    python -m jarvis voice-models show which voice models are present (--download fetches missing ones)
    python -m jarvis voice-audition --lang en --voices "piper:en_GB-alan-medium,kokoro:bm_george"
                                  play candidate voices without and with the JARVIS filter
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
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
    v = snap["voice"]
    print(f"  Microphone     : {v['mic_state']} (master switch {v['master_switch']}, push-to-talk)")
    for stage in ("capture", "stt", "tts"):
        info = v[stage]
        extra = {k: val for k, val in info.items() if k not in ("state", "cloud")}
        print(f"    - {stage:<8}: {info['state']}  {extra if extra else ''}")
    print(f"  Computer control: dry-run={'on' if snap['computer']['dry_run'] else 'off'}, platform supported={snap['computer']['platform_supported']}")
    return 0


def _serve(cfg: JarvisConfig, desktop: bool, open_browser: bool) -> int:
    import uvicorn

    from .app_core import JarvisCore
    from .server.app import create_app

    core = JarvisCore(cfg)
    app = create_app(core)
    url = f"http://{cfg.server.host}:{cfg.server.port}/"
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port, log_level="warning", log_config=None))

    if desktop:
        try:
            import webview  # type: ignore[import-not-found]
        except ImportError:
            print("pywebview is not installed; opening in the browser instead (pip install pywebview).")
            desktop = False

    if not desktop:
        if open_browser:
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        print(f"JARVIS running at {url}  (Ctrl+C to stop)")
        server.run()
        return 0

    thread = threading.Thread(target=server.run, name="jarvis-server", daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    webview.create_window("J.A.R.V.I.S.", url, width=1480, height=920, min_size=(980, 640), background_color="#02060d")
    webview.start()
    server.should_exit = True
    thread.join(timeout=10)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS home assistant")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "diagnose", "toolcheck", "voice-models", "voice-audition"])
    parser.add_argument("--lang", choices=["sv", "en"], help="voice-audition: language of the test sentence")
    parser.add_argument("--voices", help="voice-audition: comma-separated voice specs (default: the configured voice)")
    parser.add_argument("--effect", action="append", help="voice-audition: filter override, e.g. --effect pitch_semitones=-2")
    parser.add_argument("--only", choices=["plain", "filtered"], help="voice-audition: play only one variant")
    parser.add_argument("--download", action="store_true", help="voice-models: download missing models into data/models")
    parser.add_argument("--spoken", action="store_true", help="toolcheck: send the prompts as spoken requests")
    parser.add_argument("--config", help="Path to jarvis.toml")
    parser.add_argument("--desktop", action="store_true", help="Open in a native window (pywebview)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    parser.add_argument("--model", help="toolcheck: Ollama model to test (default: the configured one)")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    setup_logging(cfg.logging.level, cfg.data_dir / "logs" if cfg.logging.to_file and args.command == "run" else None)

    if args.command == "check":
        return _check(cfg)
    if args.command == "toolcheck":
        from .toolcheck import format_report, run_toolcheck

        model = args.model or cfg.ollama.model
        print(f"Running tool-use check against Ollama model '{model}' (dry-run; nothing is changed)...")
        results = asyncio.run(run_toolcheck(cfg, model=model, spoken=args.spoken))
        if not results:
            print("No checks ran.")
            return 1
        print(format_report(results, model, args.spoken))
        return 0 if all(r.passed for r in results) else 1
    if args.command == "voice-models":
        from .voice import models

        v = cfg.voice
        voices, kokoro = models.local_voice_names([s for s in (v.tts_voice_sv, v.tts_voice_en, v.tts_fallback_sv, v.tts_fallback_en) if s])
        if args.download:
            models.download(cfg.models_dir, v.stt_model, voices, kokoro)
        rows = models.status(cfg.models_dir, v.stt_model, voices, kokoro)
        for r in rows:
            print(f"  {r['kind']:<8} {r['name']:<22} {'present' if r['present'] else 'MISSING'}  {r['path']}")
        if not all(r["present"] for r in rows):
            print("Missing models can be fetched with: python -m jarvis voice-models --download")
            return 1
        return 0
    if args.command == "voice-audition":
        from .voice.audition import audition

        lang = args.lang or "en"
        voices = [s.strip() for s in (args.voices or "").split(",") if s.strip()]
        if not voices:
            voices = [s for s in ((cfg.voice.tts_voice_en,) if lang == "en" else (cfg.voice.tts_voice_sv,)) if s]
        overrides = {}
        for item in args.effect or []:
            key, _, value = item.partition("=")
            overrides[key.strip()] = float(value)
        effects = cfg.voice.effects.model_copy(update=overrides)
        effects = type(effects).model_validate(effects.model_dump())  # validate the overrides
        asyncio.run(audition(cfg.voice, cfg.models_dir, lang, voices, effects=effects, only=args.only))
        return 0
    if args.command == "diagnose":
        from . import diagnostics

        envs = {"anthropic_api_key": cfg.anthropic.api_key_env, "home_assistant_token": cfg.homeassistant.token_env}
        print(json.dumps(diagnostics.collect(cfg.data_dir, envs), indent=2))
        return 0
    try:
        return _serve(cfg, args.desktop, cfg.server.open_browser and not args.no_browser)
    except KeyboardInterrupt:
        # Ctrl+C is the normal way to stop JARVIS; no traceback needed.
        print("JARVIS stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
