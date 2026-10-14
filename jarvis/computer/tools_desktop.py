"""Volume, media keys, typing, clipboard, file actions, power and advanced mode."""

from __future__ import annotations

import re
from collections.abc import Callable

from ..core.tools import Risk, ToolContext, ToolRegistry, ToolResult, ToolSpec, schema
from .desktop import Desktop
from .shell import MAX_COMMAND_CHARS, PowerShellRunner, ShellError
from .userfiles import FileActions
from .windows import ComputerError

POWER_ACTIONS = ("shutdown", "restart", "sleep", "logout")
POWER_SV = {"shutdown": "Stänga av datorn", "restart": "Starta om datorn", "sleep": "Försätta datorn i viloläge", "logout": "Logga ut"}


def typing_risk(desktop: Desktop, args: dict) -> Risk:
    """Typing into a terminal, a console or the Run dialog is HIGH: the text becomes commands."""
    return Risk.HIGH if desktop.typing_is_dangerous() else Risk.MEDIUM


def keys_risk(desktop: Desktop, args: dict) -> Risk:
    """Enter into such a window runs what is already typed there."""
    keys = str(args.get("keys", "")).lower()
    presses_enter = bool(re.search(r"\b(enter|return)\b", keys))
    return Risk.HIGH if presses_enter and desktop.typing_is_dangerous() else Risk.MEDIUM


def _fail(exc: Exception) -> ToolResult:
    return ToolResult(ok=False, error=str(exc))


def register_desktop_tools(
    registry: ToolRegistry,
    *,
    desktop: Desktop | None = None,
    files: FileActions | None = None,
    shell: PowerShellRunner | None = None,
    advanced_mode: Callable[[], bool] = lambda: False,
) -> None:
    desktop = desktop or Desktop()
    files = files or FileActions()
    shell = shell or PowerShellRunner()

    # -- volume --------------------------------------------------------------
    async def volume_status(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.volume_status())
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    async def volume_set(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.set_volume(args["level"], dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    async def volume_adjust(args, ctx: ToolContext) -> ToolResult:
        step = int(args.get("step", 10))
        try:
            return ToolResult(ok=True, data=desktop.adjust_volume(step, dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    async def volume_mute(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.set_mute(args.get("muted"), dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    registry.register(ToolSpec(
        "volume_status", "Datorns ljudvolym just nu (0-100) och om den är tystad.",
        schema({}), volume_status, Risk.READ, "media",
    ))
    registry.register(ToolSpec(
        "volume_set", "Sätt datorns ljudvolym, t.ex. 'sätt volymen på 30'. 0-100. Gäller datorn, inte högtalare i hemmet.",
        schema({"level": {"type": "integer", "minimum": 0, "maximum": 100}}, ["level"]),
        volume_set, Risk.LOW, "media", supports_dry_run=True,
        confirm_summary=lambda a: f"Sätta datorns volym till {a['level']}%",
    ))
    registry.register(ToolSpec(
        "volume_adjust", "Höj eller sänk datorns volym, t.ex. 'höj volymen' (step 10) eller 'sänk volymen' (step -10).",
        schema({"step": {"type": "integer", "minimum": -100, "maximum": 100}}, ["step"]),
        volume_adjust, Risk.LOW, "media", supports_dry_run=True,
        confirm_summary=lambda a: f"Ändra datorns volym med {a['step']}",
    ))
    registry.register(ToolSpec(
        "volume_mute", "Tysta eller slå på datorns ljud. Utan argument växlar den.",
        schema({"muted": {"type": "boolean", "description": "true = tyst, false = ljud på."}}),
        volume_mute, Risk.LOW, "media", supports_dry_run=True,
        confirm_summary=lambda a: "Tysta datorn" if a.get("muted", True) else "Slå på datorns ljud",
    ))

    # -- media keys ----------------------------------------------------------
    async def media_control(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.media(args["action"], dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    registry.register(ToolSpec(
        "media_control",
        "Styr musik på DEN HÄR DATORN (Spotify, webbläsaren) med mediatangenterna: 'pausa musiken', 'nästa låt'. "
        "Nämner ägaren ett rum, till exempel köket, är det högtalarna och sonos_playback som gäller.",
        schema({"action": {"type": "string", "enum": ["play", "pause", "next", "previous", "stop"]}}, ["action"]),
        media_control, Risk.LOW, "media", supports_dry_run=True,
        confirm_summary=lambda a: f"Mediatangent: {a['action']}",
    ))

    # -- typing and clipboard ------------------------------------------------
    async def type_text(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.type_text(args["text"], dry_run=ctx.dry_run), dry_run=ctx.dry_run, sensitivity="personal")
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    async def press_keys(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.press(args["keys"], dry_run=ctx.dry_run), dry_run=ctx.dry_run, sensitivity="personal")
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    async def clipboard_read(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.read_clipboard(), sensitivity="personal")
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    async def clipboard_write(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.write_clipboard(args["text"], dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    registry.register(ToolSpec(
        "input_type_text", "Skriv text i fönstret som är aktivt just nu, t.ex. 'skriv hej i chatten'.",
        schema({"text": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["text"]),
        type_text, Risk.MEDIUM, "computer", supports_dry_run=True, output_sensitivity="personal",
        risk_for_args=lambda a: typing_risk(desktop, a),
        confirm_summary=lambda a: (
            f"Skriva i {desktop.typing_is_dangerous() or 'det aktiva fönstret'}: {a['text'][:120]}"
        ),
    ))
    registry.register(ToolSpec(
        "input_press_keys", "Tryck ett kortkommando i det aktiva fönstret, t.ex. 'ctrl+s' eller 'alt+tab'. Ctrl+Alt+Del går inte.",
        schema({"keys": {"type": "string", "minLength": 1, "maxLength": 60, "description": "T.ex. 'ctrl+s'."}}, ["keys"]),
        press_keys, Risk.MEDIUM, "computer", supports_dry_run=True, output_sensitivity="personal",
        risk_for_args=lambda a: keys_risk(desktop, a),
        confirm_summary=lambda a: f"Trycka {a['keys']} i {desktop.typing_is_dangerous() or 'det aktiva fönstret'}",
    ))
    registry.register(ToolSpec(
        "clipboard_read", "Läs texten som ligger i urklipp ('vad har jag kopierat?').",
        schema({}), clipboard_read, Risk.READ, "computer", output_sensitivity="personal", untrusted_output=True,
    ))
    registry.register(ToolSpec(
        "clipboard_write", "Kopiera text till urklipp ('kopiera det här').",
        schema({"text": {"type": "string", "minLength": 1, "maxLength": 10000}}, ["text"]),
        clipboard_write, Risk.LOW, "computer", supports_dry_run=True,
        confirm_summary=lambda a: f"Kopiera {len(a['text'])} tecken till urklipp",
    ))

    # -- file actions --------------------------------------------------------
    async def create_note(args, ctx: ToolContext) -> ToolResult:
        try:
            data = files.create_note(args["name"], args.get("text", ""), args.get("folder"), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)
        return ToolResult(ok=True, data=data, dry_run=ctx.dry_run, sensitivity="personal")

    async def rename(args, ctx: ToolContext) -> ToolResult:
        try:
            data = files.rename(args["path"], args["new_name"], dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)
        return ToolResult(ok=True, data=data, dry_run=ctx.dry_run, sensitivity="personal")

    async def move(args, ctx: ToolContext) -> ToolResult:
        try:
            data = files.move(args["path"], args["to_folder"], dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)
        return ToolResult(ok=True, data=data, dry_run=ctx.dry_run, sensitivity="personal")

    async def recycle(args, ctx: ToolContext) -> ToolResult:
        try:
            data = files.to_recycle_bin(args["path"], dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)
        return ToolResult(ok=True, data=data, dry_run=ctx.dry_run, sensitivity="personal")

    async def undo(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=files.undo_last(dry_run=ctx.dry_run), dry_run=ctx.dry_run, sensitivity="personal")
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    registry.register(ToolSpec(
        "files_create_note", "Skapa en anteckning (textfil) i Dokument, t.ex. 'skapa en anteckning som heter inköp med mjölk och bröd'.",
        schema(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 100, "description": "Filnamn utan ändelse, t.ex. 'inköp'."},
                "text": {"type": "string", "maxLength": 20000, "description": "Innehållet."},
                "folder": {"type": "string", "maxLength": 200, "description": "Valfri mapp, t.ex. 'skrivbordet'."},
            },
            ["name", "text"],
        ),
        create_note, Risk.LOW, "personal", supports_dry_run=True, dry_run_category="computer", output_sensitivity="personal",
        confirm_summary=lambda a: f"Skapa anteckningen '{a['name']}'",
    ))
    registry.register(ToolSpec(
        "files_rename", "Byt namn på en fil i din användarmapp. Gå via files_search för att hitta sökvägen.",
        schema({"path": {"type": "string", "maxLength": 1000}, "new_name": {"type": "string", "minLength": 1, "maxLength": 100}}, ["path", "new_name"]),
        rename, Risk.MEDIUM, "personal", supports_dry_run=True, dry_run_category="computer", output_sensitivity="personal",
        confirm_summary=lambda a: f"Byta namn till '{a['new_name']}'",
    ))
    registry.register(ToolSpec(
        "files_move", "Flytta en fil till en annan mapp i din användarmapp, t.ex. till 'dokument'.",
        schema({"path": {"type": "string", "maxLength": 1000}, "to_folder": {"type": "string", "minLength": 2, "maxLength": 200}}, ["path", "to_folder"]),
        move, Risk.MEDIUM, "personal", supports_dry_run=True, dry_run_category="computer", output_sensitivity="personal",
        confirm_summary=lambda a: f"Flytta filen till {a['to_folder']}",
    ))
    registry.register(ToolSpec(
        "files_recycle", "Radera en fil genom att flytta den till Papperskorgen. Inget raderas permanent.",
        schema({"path": {"type": "string", "maxLength": 1000}}, ["path"]),
        recycle, Risk.HIGH, "personal", supports_dry_run=True, dry_run_category="computer", output_sensitivity="personal",
        confirm_summary=lambda a: f"Flytta '{a['path']}' till Papperskorgen",
    ))
    registry.register(ToolSpec(
        "files_undo", "Ångra det senaste JARVIS gjorde med en fil (skapade, döpte om eller flyttade). 'ångra'.",
        schema({}), undo, Risk.MEDIUM, "personal", supports_dry_run=True, dry_run_category="computer", output_sensitivity="personal",
        confirm_summary=lambda a: "Ångra den senaste filändringen",
    ))

    # -- power ---------------------------------------------------------------
    async def lock(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.lock(dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    async def power(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=desktop.power(args["action"], dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return _fail(exc)

    registry.register(ToolSpec(
        "power_lock", "Lås datorn (låsskärmen), t.ex. 'lås datorn'.",
        schema({}), lock, Risk.LOW, "computer", supports_dry_run=True,
        confirm_summary=lambda a: "Låsa datorn",
    ))
    registry.register(ToolSpec(
        "power_action", "Stäng av, starta om, försätt i viloläge eller logga ut. Frågar alltid först.",
        schema({"action": {"type": "string", "enum": list(POWER_ACTIONS)}}, ["action"]),
        power, Risk.HIGH, "computer", supports_dry_run=True,
        confirm_summary=lambda a: POWER_SV.get(a.get("action", ""), str(a.get("action"))) + " nu",
    ))

    # -- advanced mode -------------------------------------------------------
    async def run_powershell(args, ctx: ToolContext) -> ToolResult:
        if not advanced_mode():
            return ToolResult(ok=False, error="Avancerat läge är avstängt. Slå på det i Inställningar först.")
        command = args["command"]
        if ctx.dry_run:
            return ToolResult(ok=True, data={"would_run": command}, dry_run=True, sensitivity="personal")
        try:
            data = shell.run(command)
        except ShellError as exc:
            return _fail(exc)
        return ToolResult(ok=data["ok"], data=data, error=None if data["ok"] else f"Kommandot gav felkod {data['exit_code']}.", sensitivity="personal")

    registry.register(ToolSpec(
        "shell_run_powershell",
        "AVANCERAT: kör ett PowerShell-kommando som din vanliga användare. Bara när ägaren uttryckligen ber om det "
        "och avancerat läge är på. Det exakta kommandot visas och måste godkännas varje gång.",
        schema({"command": {"type": "string", "minLength": 1, "maxLength": MAX_COMMAND_CHARS}}, ["command"]),
        run_powershell, Risk.HIGH, "computer", supports_dry_run=True, output_sensitivity="personal",
        blocked_after_untrusted=True, timeout_s=75.0,
        confirm_summary=lambda a: f"Kör i PowerShell: {a['command']}",
    ))
