"""File actions under the owner's user folder: notes, rename, move, Recycle Bin, undo.

Limits that always apply:

* Only paths inside the user's own folder (``C:\\Users\\<you>``).
* Never a secret-looking file (.env, keys, certificates, password databases) and
  never inside ``.ssh``, ``.git``, ``node_modules`` or ``AppData``.
* Never follows a link or junction out of the user folder.
* "Delete" moves to the Recycle Bin. Nothing is ever deleted permanently.
* Creating, renaming and moving can be undone with the last-action undo.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..files.index import SKIP_DIRS, is_secret_name, known_folder
from .windows import ComputerError
from .windows import is_windows as _is_windows

log = logging.getLogger(__name__)

MAX_NOTE_CHARS = 20000
MAX_UNDO = 20
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# Swedish and English names for the usual folders, so "hämtade filer" works.
FOLDER_WORDS = {
    "downloads": "Downloads", "hämtade filer": "Downloads", "hamtade filer": "Downloads", "hämtade": "Downloads",
    "documents": "Documents", "dokument": "Documents",
    "desktop": "Desktop", "skrivbord": "Desktop", "skrivbordet": "Desktop",
    "pictures": "Pictures", "bilder": "Pictures",
}


@dataclass
class UndoStep:
    label: str
    undo: Callable[[], str]


@dataclass
class FileActions:
    home: Path = field(default_factory=lambda: Path.home())
    notes_folder: Path | None = None
    recycle: Callable[[Path], None] | None = None
    is_windows: Callable[[], bool] | None = None
    undo_stack: list[UndoStep] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.home = Path(self.home).resolve()
        if self.notes_folder is None:
            self.notes_folder = known_folder("Documents") or (self.home / "Documents")
        self.recycle = self.recycle or recycle_to_bin
        self.is_windows = self.is_windows or _is_windows

    # -- paths ---------------------------------------------------------------
    def folder(self, name: str) -> Path:
        """A named folder (Hämtade filer, Dokument, Skrivbord, Bilder) or a path under the user folder."""
        key = name.strip().lower()
        known = FOLDER_WORDS.get(key)
        if known:
            return self.check(self._known_under_home(known), must_exist=True, must_be_dir=True)
        return self.check(self._expand(name), must_exist=True, must_be_dir=True)

    def _known_under_home(self, name: str) -> Path:
        """The Windows Known Folder when it lies inside the user folder (also with OneDrive),
        otherwise the plain <home>/<name>."""
        known = known_folder(name)
        if known is not None:
            resolved = known.resolve()
            if resolved == self.home or self.home in resolved.parents:
                return resolved
        return self.home / name

    def _expand(self, raw: str) -> Path:
        text = os.path.expandvars(str(raw).strip().strip('"'))
        path = Path(text).expanduser()
        if not path.is_absolute():
            known = FOLDER_WORDS.get(path.parts[0].lower()) if path.parts else None
            if known:
                path = self._known_under_home(known).joinpath(*path.parts[1:])
            else:
                path = self.home / path
        return path

    def check(self, path: Path | str, *, must_exist: bool, must_be_dir: bool = False) -> Path:
        path = Path(path) if isinstance(path, Path) else self._expand(path)
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ComputerError(f"Ogiltig sökväg: {path}") from exc
        if resolved != self.home and self.home not in resolved.parents:
            raise ComputerError(f"'{path}' ligger utanför din användarmapp. JARVIS rör bara filer där.")
        # Only the part below the user folder matters; the folder itself may sit anywhere.
        for part in resolved.relative_to(self.home).parts:
            if part.lower() in SKIP_DIRS:
                raise ComputerError(f"Mappen '{part}' är skyddad och rörs inte.")
        if is_secret_name(resolved.name):
            raise ComputerError(f"'{resolved.name}' ser ut att innehålla hemligheter och rörs aldrig.")
        if resolved.is_symlink():
            raise ComputerError("Länkar följs inte.")
        if must_exist:
            if not resolved.exists():
                raise ComputerError(f"Hittar inte '{path}'.")
            attrs = getattr(resolved.lstat(), "st_file_attributes", 0)
            if attrs & _REPARSE:
                raise ComputerError("Länkar och junctions följs inte.")
            if must_be_dir and not resolved.is_dir():
                raise ComputerError(f"'{path}' är inte en mapp.")
            if not must_be_dir and resolved.is_dir():
                raise ComputerError(f"'{path}' är en mapp, inte en fil.")
        return resolved

    # -- undo ----------------------------------------------------------------
    def _remember(self, label: str, undo: Callable[[], str]) -> None:
        self.undo_stack.append(UndoStep(label, undo))
        del self.undo_stack[:-MAX_UNDO]

    def undo_last(self, *, dry_run: bool) -> dict[str, Any]:
        if not self.undo_stack:
            raise ComputerError("Det finns inget att ångra.")
        step = self.undo_stack[-1]
        if dry_run:
            return {"would_undo": step.label}
        self.undo_stack.pop()
        return {"undone": step.label, "result": step.undo()}

    # -- actions -------------------------------------------------------------
    def create_note(self, name: str, text: str, folder: str | None = None, *, dry_run: bool) -> dict[str, Any]:
        if len(text) > MAX_NOTE_CHARS:
            raise ComputerError(f"Anteckningen får vara högst {MAX_NOTE_CHARS} tecken.")
        safe = _safe_filename(name)
        base = self.folder(folder) if folder else self.check(self.notes_folder, must_exist=True, must_be_dir=True)
        target = self.check(base / (safe if safe.lower().endswith((".txt", ".md")) else f"{safe}.txt"), must_exist=False)
        if target.exists():
            target = _unique(target)
        if dry_run:
            return {"would_create": str(target), "characters": len(text)}
        target.write_text(text, encoding="utf-8")
        self._remember(f"skapade {target.name}", lambda p=target: _undo_create(p, self.recycle))
        log.info("Created note %s", target.name)
        return {"created": str(target), "name": target.name, "folder": str(target.parent), "characters": len(text)}

    def rename(self, path: str, new_name: str, *, dry_run: bool) -> dict[str, Any]:
        source = self.check(path, must_exist=True)
        safe = _safe_filename(new_name)
        if not Path(safe).suffix and source.suffix:
            safe += source.suffix
        target = self.check(source.parent / safe, must_exist=False)
        if target.exists():
            raise ComputerError(f"'{target.name}' finns redan.")
        if dry_run:
            return {"would_rename": source.name, "to": target.name, "folder": str(source.parent)}
        source.rename(target)
        self._remember(f"döpte om {source.name} till {target.name}", lambda a=target, b=source: _undo_move(a, b))
        return {"renamed": source.name, "to": target.name, "folder": str(target.parent)}

    def move(self, path: str, to_folder: str, *, dry_run: bool) -> dict[str, Any]:
        source = self.check(path, must_exist=True)
        base = self.folder(to_folder)
        target = self.check(base / source.name, must_exist=False)
        if target.exists():
            raise ComputerError(f"'{source.name}' finns redan i {base}.")
        if source.parent == base:
            raise ComputerError(f"'{source.name}' ligger redan i {base}.")
        if dry_run:
            return {"would_move": source.name, "to": str(base)}
        shutil.move(str(source), str(target))
        self._remember(f"flyttade {source.name} till {base.name}", lambda a=target, b=source: _undo_move(a, b))
        return {"moved": source.name, "from": str(source.parent), "to": str(base)}

    def to_recycle_bin(self, path: str, *, dry_run: bool) -> dict[str, Any]:
        target = self.check(path, must_exist=True)
        if dry_run:
            return {"would_move_to_recycle_bin": str(target)}
        if not self.is_windows():
            raise ComputerError("Papperskorgen finns bara på Windows.")
        self.recycle(target)
        log.info("Moved %s to the Recycle Bin", target.name)
        return {
            "moved_to_recycle_bin": target.name,
            "from": str(target.parent),
            "note": "Filen ligger i Papperskorgen och kan återställas därifrån.",
        }


def _safe_filename(name: str) -> str:
    clean = str(name).strip().strip(". ")
    if not clean or clean in (".", ".."):
        raise ComputerError("Ange ett filnamn.")
    if any(c in clean for c in '\\/:*?"<>|') or "\x00" in clean:
        raise ComputerError("Filnamnet innehåller tecken som Windows inte tillåter.")
    if len(clean) > 100:
        raise ComputerError("Filnamnet får vara högst 100 tecken.")
    return clean


def _unique(path: Path) -> Path:
    for i in range(2, 100):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise ComputerError("Kunde inte hitta ett ledigt filnamn.")


def _undo_create(path: Path, recycle: Callable[[Path], None]) -> str:
    if not path.exists():
        return f"{path.name} fanns inte längre."
    recycle(path)
    return f"{path.name} flyttades till Papperskorgen."


def _undo_move(current: Path, original: Path) -> str:
    if not current.exists():
        return f"{current.name} fanns inte längre."
    if original.exists():
        raise ComputerError(f"Kan inte ångra: '{original.name}' finns redan.")
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(current), str(original))
    return f"{current.name} är tillbaka som {original.name}."


def recycle_to_bin(path: Path) -> None:  # pragma: no cover - real Recycle Bin
    """SHFileOperationW with FOF_ALLOWUNDO: the file goes to the Recycle Bin, not away."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x0004, 0x0010, 0x0040, 0x0400
    op = SHFILEOPSTRUCTW(
        None, FO_DELETE, str(path) + "\0\0", None,
        FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None,
    )
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise ComputerError(f"Kunde inte flytta '{path.name}' till Papperskorgen (kod {rc}).")


def open_in_explorer(path: Path) -> None:  # pragma: no cover - opens a real window
    os.startfile(str(path))  # noqa: S606 - a folder inside the user's own home, checked above
