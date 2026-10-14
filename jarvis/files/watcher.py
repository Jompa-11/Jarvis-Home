"""File watcher: refreshes the metadata index, notifies about new files, and the file tools.

The index is rebuilt in a worker thread every ``refresh_minutes`` so the event
loop (and the chat) never waits for a scan. The first scan is a baseline: files
that already exist are indexed silently; only files that appear later notify.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..config import FilesConfig
from ..rules import RulesConfig
from .index import FileIndex, FileRecord, Root, resolve_roots, scan

if TYPE_CHECKING:
    from pathlib import Path

    from ..core.tools import ToolRegistry
    from ..monitoring.service import MonitorService

log = logging.getLogger(__name__)

MAX_RESULTS = 20
MAX_IMPORTANT_NOTICES = 10  # per scan; more than that are grouped into one notification
MAX_SINGLE_NOTICES = 20  # other new files per scan before they are grouped
PERIODS = {"today": None, "week": 7, "month": 31, "year": 365, "any": None}
TYPE_GROUPS = {
    "image": ["jpg", "jpeg", "png", "gif", "heic", "webp", "bmp"],
    "bild": ["jpg", "jpeg", "png", "gif", "heic", "webp", "bmp"],
    "video": ["mp4", "mov", "mkv", "avi", "webm"],
    "document": ["pdf", "doc", "docx", "odt", "txt", "rtf"],
    "dokument": ["pdf", "doc", "docx", "odt", "txt", "rtf"],
    "word": ["doc", "docx"],
    "excel": ["xls", "xlsx", "csv"],
    "powerpoint": ["ppt", "pptx"],
    "zip": ["zip", "7z", "rar"],
}


def _where(r: FileRecord) -> str:
    return r.root + (f"\\{r.rel_dir}" if r.rel_dir else "")


class FileWatcher:
    def __init__(
        self,
        cfg: FilesConfig,
        monitor: "MonitorService",
        get_rules: Callable[[], RulesConfig],
        index_path: "Path | None",
        *,
        roots: list[Root] | None = None,
    ) -> None:
        self.cfg = cfg
        self.monitor = monitor
        self.get_rules = get_rules
        self.index = FileIndex(index_path)
        self._roots = roots
        self.scanning = False
        self.last_scan: float | None = None
        self.last_error: str | None = None
        self.truncated = False
        # New files since the last summary (names and times only), for the morning summary.
        self.new_since_summary: deque[dict[str, Any]] = deque(maxlen=500)
        self._lock = asyncio.Lock()

    @property
    def roots(self) -> list[Root]:
        if self._roots is None:
            self._roots = resolve_roots(self.cfg.watched_folders)
        return self._roots

    def scope_note(self) -> str:
        names = ", ".join(r.label for r in self.roots) or "inga"
        return f"Söker bara i de bevakade mapparna ({names}), inte på hela datorn."

    def state(self) -> tuple[str, str]:
        if not self.roots:
            return "error", "None of the watched folders were found (config [files] watched_folders)."
        names = ", ".join(r.label for r in self.roots)
        if self.last_error:
            return "error", self.last_error
        if self.last_scan is None:
            return "indexing", f"{names}: first indexing in progress (names and metadata only)"
        when = time.strftime("%H:%M", time.localtime(self.last_scan))
        more = f" (stopped at the limit of {self.cfg.max_files} files)" if self.truncated else ""
        return "active", f"{names}: {self.index.count()} files{more}, every {self.cfg.refresh_minutes:g} min, last {when}"

    def _publish_state(self) -> None:
        self.monitor.set_source("folders", *self.state())

    async def refresh(self) -> int:
        """Rescan in a worker thread; notify about new files. Returns how many were new."""
        if self._lock.locked():
            return 0
        async with self._lock:
            self.scanning = True
            self._publish_state()
            try:
                roots = await asyncio.to_thread(lambda: self.roots)
                baseline = self.index.meta("last_scan") is None
                records, truncated = await asyncio.to_thread(scan, roots, max_depth=self.cfg.max_depth, max_files=self.cfg.max_files)
                new = await asyncio.to_thread(self.index.replace_all, records)
                self.truncated = truncated
                self.last_scan = time.time()
                self.last_error = None
            except Exception as exc:
                self.last_error = f"Could not index the watched folders ({type(exc).__name__})"
                log.warning("File index refresh failed: %s", type(exc).__name__)
                return 0
            finally:
                self.scanning = False
                self._publish_state()
            if baseline:
                log.info("File index baseline: %d files", len(records))
                return 0
            return await self._notify_new(new)

    async def _notify_new(self, new: list[FileRecord]) -> int:
        seen = self.monitor.notifier.seen
        new = sorted((r for r in new if not seen.seen(f"file:{r.path}")), key=lambda r: r.mtime)
        if not new:
            return 0
        rules = self.get_rules().files
        important = [(r, kw) for r in new for kw in [rules.match(r.name)] if kw]
        others = [r for r in new if not rules.match(r.name)]
        notify = self.monitor.notifier.notify
        for r, kw in important[:MAX_IMPORTANT_NOTICES]:
            await notify(source="files", title=f"Ny viktig fil: {r.name}", detail=f"I {_where(r)} · matchar regeln \"{kw}\"", priority="high", kind="file", dedupe_key=f"file:{r.path}")
        if len(important) > MAX_IMPORTANT_NOTICES:
            rest = important[MAX_IMPORTANT_NOTICES:]
            await notify(source="files", title=f"{len(rest)} fler viktiga filer", detail=", ".join(r.name for r, _ in rest[:5]), priority="high", kind="file")
        if len(others) <= MAX_SINGLE_NOTICES:
            for r in others:
                await notify(source="files", title=f"Ny fil: {r.name}", detail=f"I {_where(r)}", priority="low", kind="file", dedupe_key=f"file:{r.path}")
        else:
            await notify(source="files", title=f"{len(others)} nya filer", detail=", ".join(r.name for r in others[-5:]), priority="low", kind="file")
        for r in new:
            seen.add(f"file:{r.path}")  # each file is announced at most once
            self.new_since_summary.append({"name": r.name, "where": _where(r), "mtime": r.mtime, "important": bool(rules.match(r.name))})
        return len(new)

    # -- tools -----------------------------------------------------------------
    def search(self, query: str = "", file_type: str | None = None, period: str = "any", now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        words = [w for w in query.replace("*", " ").split() if w]
        exts = None
        if file_type:
            ft = file_type.strip().lower().lstrip(".")
            exts = TYPE_GROUPS.get(ft, [ft])
        since = None
        if period == "today":
            lt = time.localtime(now)
            since = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        elif PERIODS.get(period):
            since = now - PERIODS[period] * 86400
        hits = self.index.search(words, exts=exts, since=since, limit=MAX_RESULTS)
        result: dict[str, Any] = {
            "scope": self.scope_note(),
            "matches": [
                {
                    "name": r.name,
                    "folder": _where(r),
                    "path": r.path,
                    "type": r.ext or "(none)",
                    "size_kb": round(r.size / 1024, 1),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(r.mtime)),
                }
                for r in hits
            ],
            "max_results": MAX_RESULTS,
        }
        if self.last_scan is None and self.index.count() == 0:
            result["note"] = "The first indexing is still running; try again in a minute."
        elif not hits:
            result["note"] = "No matching file names in the watched folders."
        return result

    def resolve_indexed(self, path_or_name: str) -> FileRecord | None:
        """An indexed file by exact path, or by exact file name (newest wins)."""
        rec = self.index.contains(path_or_name)
        if rec is None:
            same = self.index.by_name(os.path.basename(path_or_name))
            rec = same[0] if same else None
        return rec

    def open_folder(self, path_or_name: str, *, dry_run: bool) -> dict[str, Any]:
        rec = self.resolve_indexed(path_or_name)
        if rec is None:
            raise ValueError("That file is not in the index of watched folders. Search for it with files_search first.")
        if not any(_inside(rec.path, r.path) for r in self.roots):
            raise ValueError("That path is outside the watched folders.")
        if not os.path.isfile(rec.path):
            raise ValueError("The file no longer exists at that place.")
        cmd = explorer_select_command(rec.path)
        data = {"folder": _where(rec), "file": rec.name}
        if dry_run:
            return {**data, "would_run": cmd}
        if sys.platform != "win32":
            raise ValueError("Opening folders is only available on Windows.")
        # A ready command string (not a list): CreateProcess gets exactly this text.
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)  # noqa: S603 - fixed program, quoted indexed path
        return {**data, "opened": True}


def explorer_select_command(path: str) -> str:
    """explorer.exe /select,"<path>": opens the containing folder with the file highlighted.

    File names in Downloads can be chosen by others, and explorer.exe splits its
    arguments on commas. The path is therefore always inside one pair of quotes,
    and anything that could end the quotes or the line is refused. The file
    itself is never opened.
    """
    if not os.path.isabs(path):
        raise ValueError("Only absolute paths can be shown in File Explorer.")
    if '"' in path or any(ord(c) < 32 for c in path):
        raise ValueError("That path contains characters that cannot be passed safely to File Explorer.")
    explorer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
    return f'"{explorer}" /select,"{path}"'


def _inside(path: str, root: "Path") -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(str(root))]) == os.path.normcase(str(root))
    except ValueError:
        return False


def register_file_tools(registry: "ToolRegistry", watcher: FileWatcher, *, allow_open: bool) -> None:
    from ..core.tools import Risk, ToolContext, ToolResult, ToolSpec, schema

    async def files_search(args, ctx: ToolContext) -> ToolResult:
        data = await asyncio.to_thread(watcher.search, args.get("query", ""), args.get("file_type"), args.get("period", "any"))
        return ToolResult(ok=True, data=data, sensitivity="personal")

    registry.register(
        ToolSpec(
            "files_search",
            "Find files by name in the watched folders (Downloads, Documents), newest first. "
            "E.g. 'hitta min senaste faktura' -> query 'faktura'.",
            schema(
                {
                    "query": {"type": "string", "maxLength": 100, "description": "Word in the file name."},
                    "file_type": {"type": "string", "maxLength": 20, "description": "e.g. pdf"},
                    "period": {"type": "string", "enum": list(PERIODS)},
                }
            ),
            files_search,
            Risk.READ,
            "system",
            output_sensitivity="personal",
        )
    )
    if not allow_open:
        return

    async def files_open_folder(args, ctx: ToolContext) -> ToolResult:
        try:
            data = watcher.open_folder(args["path"], dry_run=ctx.dry_run)
        except (ValueError, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, data=data, dry_run=ctx.dry_run, sensitivity="personal")

    registry.register(
        ToolSpec(
            "files_open_folder",
            "Open the folder of a files_search match in File Explorer (the file is highlighted, not opened).",
            schema({"path": {"type": "string", "maxLength": 1000, "description": "'path' from files_search."}}, ["path"]),
            files_open_folder,
            Risk.MEDIUM,
            "computer",
            output_sensitivity="personal",
            supports_dry_run=True,
            confirm_summary=lambda a: f"Open the folder containing '{os.path.basename(a['path'])}' in File Explorer",
        )
    )
