"""Tools for controlling the PC: programs, web pages, folders and windows.

Volume, media keys, typing, clipboard, file actions, power and advanced mode are
registered in :mod:`jarvis.computer.tools_desktop`.
"""

from __future__ import annotations

import asyncio

from ..core.tools import Risk, ToolContext, ToolRegistry, ToolResult, ToolSpec, schema
from .apps import AppLauncher
from .desktop import WINDOW_ACTIONS, Desktop
from .userfiles import FileActions, open_in_explorer
from .web import WebOpener
from .windows import ComputerError, WindowsControl


def _fail(exc: Exception) -> ToolResult:
    return ToolResult(ok=False, error=str(exc))


def register_computer_tools(
    registry: ToolRegistry,
    wc: WindowsControl,
    *,
    launcher: AppLauncher | None = None,
    web: WebOpener | None = None,
    desktop: Desktop | None = None,
    files: FileActions | None = None,
) -> None:
    launcher = launcher or AppLauncher(wc.cfg)
    web = web or WebOpener()
    desktop = desktop or Desktop()
    files = files or FileActions()
    aliases = ", ".join(sorted(wc.cfg.allowed_apps)) or "inga"

    # -- programs ------------------------------------------------------------
    async def list_apps(args, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data=await asyncio.to_thread(launcher.list_public))

    async def open_app(args, ctx: ToolContext) -> ToolResult:
        try:
            data = await asyncio.to_thread(launcher.open, args["app"], dry_run=ctx.dry_run)
            return ToolResult(ok=True, data=data, dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    registry.register(ToolSpec(
        "computer_list_apps",
        "Lista program som går att starta på datorn. Använd vid 'vilka program kan du starta?'. Svara aldrig ur minnet.",
        schema({}), list_apps, Risk.READ, "computer",
    ))
    registry.register(ToolSpec(
        "computer_open_app",
        f"Starta ett installerat program på datorn, t.ex. 'öppna spotify', 'starta discord', 'öppna anteckningar'. "
        f"Genvägar: {aliases}. Alla installerade program går att starta med sitt namn.",
        schema({"app": {"type": "string", "minLength": 1, "maxLength": 60, "description": "Programmets namn, t.ex. 'spotify'."}}, ["app"]),
        open_app, Risk.MEDIUM, "computer", supports_dry_run=True,
        confirm_summary=lambda a: f"Starta programmet '{a['app']}'",
    ))

    # -- web -----------------------------------------------------------------
    async def open_web(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=web.open_site(args["site"], dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    async def search_web(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=web.search(args["query"], dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    registry.register(ToolSpec(
        "computer_open_web",
        "Öppna en webbplats i webbläsaren, t.ex. 'öppna youtube' eller 'öppna svt.se'. Bara http och https.",
        schema({"site": {"type": "string", "minLength": 2, "maxLength": 300, "description": "Namn eller adress, t.ex. 'youtube'."}}, ["site"]),
        open_web, Risk.LOW, "computer", supports_dry_run=True,
        confirm_summary=lambda a: f"Öppna {a['site']} i webbläsaren",
    ))
    registry.register(ToolSpec(
        "computer_search_web",
        "Sök på webben i webbläsaren, t.ex. 'sök på väder Stockholm'. Öppnar en söksida; JARVIS läser inte resultatet.",
        schema({"query": {"type": "string", "minLength": 1, "maxLength": 300}}, ["query"]),
        search_web, Risk.LOW, "computer", supports_dry_run=True,
        confirm_summary=lambda a: f"Söka på '{a['query']}'",
    ))

    # -- folders -------------------------------------------------------------
    async def open_folder(args, ctx: ToolContext) -> ToolResult:
        try:
            path = files.folder(args["folder"])
        except ComputerError as exc:
            return _fail(exc)
        if ctx.dry_run:
            return ToolResult(ok=True, data={"would_open": str(path)}, dry_run=True, sensitivity="personal")
        try:
            open_in_explorer(path)
        except OSError as exc:
            return _fail(exc)
        return ToolResult(ok=True, data={"opened": str(path)}, sensitivity="personal")

    registry.register(ToolSpec(
        "computer_open_folder",
        "Öppna en mapp i Utforskaren, t.ex. 'öppna hämtade filer', 'öppna dokument', 'öppna skrivbordet'. "
        "Bara mappar i din användarmapp.",
        schema({"folder": {"type": "string", "minLength": 2, "maxLength": 300, "description": "Mappnamn eller sökväg."}}, ["folder"]),
        open_folder, Risk.LOW, "computer", supports_dry_run=True, output_sensitivity="personal",
        confirm_summary=lambda a: f"Öppna mappen {a['folder']}",
    ))

    # -- windows -------------------------------------------------------------
    async def list_windows(args, ctx: ToolContext) -> ToolResult:
        try:
            wins = desktop.windows()
        except ComputerError as exc:
            return _fail(exc)
        return ToolResult(ok=True, data={"windows": [w.title for w in wins][:50]}, sensitivity="personal")

    async def window(args, ctx: ToolContext) -> ToolResult:
        try:
            data = desktop.window(args["title_contains"], args.get("action", "focus"), dry_run=ctx.dry_run)
        except ComputerError as exc:
            return _fail(exc)
        return ToolResult(ok=True, data=data, dry_run=ctx.dry_run, verified=data.pop("verified", None), sensitivity="personal")

    registry.register(ToolSpec(
        "computer_list_windows",
        "Lista titlarna på öppna fönster. Använd vid 'vilka fönster är öppna?'.",
        schema({}), list_windows, Risk.READ, "computer", output_sensitivity="personal", untrusted_output=True,
    ))
    registry.register(ToolSpec(
        "computer_window",
        "Styr ett öppet fönster: växla till det, minimera, maximera eller stäng det. "
        "T.ex. 'växla till spotify', 'minimera webbläsaren', 'stäng anteckningar'.",
        schema(
            {
                "title_contains": {"type": "string", "minLength": 2, "maxLength": 100, "description": "Del av fönstrets titel."},
                "action": {"type": "string", "enum": list(WINDOW_ACTIONS), "description": "focus, minimize, maximize, restore eller close."},
            },
            ["title_contains", "action"],
        ),
        window, Risk.MEDIUM, "computer", supports_dry_run=True, output_sensitivity="personal",
        confirm_summary=lambda a: f"{a.get('action', 'focus')} fönstret med '{a['title_contains']}' i titeln",
    ))
