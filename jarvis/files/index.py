"""Metadata-only file index for the watched folders.

Stores name, path, type (extension), size and modified time. File CONTENTS are
never opened or read: scanning uses directory listings and stat() only.

Skipped on purpose:
* secret-looking files (.env, keys, certificates, password databases, VPN
  profiles, SSH keys, anything named like "password"/"lösenord"/"credential"),
* folders named .ssh, .git, node_modules, AppData, and hidden or system entries,
* symbolic links, junctions and other reparse points, so the scan can never
  leave the watched folders.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import sqlite3
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SKIP_DIRS = {".ssh", ".git", "node_modules", "appdata", "$recycle.bin", "system volume information"}
SECRET_PATTERNS = (
    ".env", ".env.*", "*.env", "*.key", "*.pem", "*.pfx", "*.p12", "*.kdbx", "*.ovpn",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*password*", "*passwd*", "*lösenord*", "*credential*",
)
_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# Unfinished downloads and Office lock files: not real new files.
TEMP_PATTERNS = ("*.crdownload", "*.part", "*.partial", "*.download", "*.tmp", "~$*", "desktop.ini", "thumbs.db")

# Windows Known Folders (resolved through the shell, so moved folders work too).
KNOWN_FOLDERS = {
    "Downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Hämtade filer"),
    "Documents": ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Dokument"),
    "Desktop": ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Skrivbord"),
    "Pictures": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Bilder"),
}


def known_folder(name: str) -> Path | None:
    """Path of a Windows Known Folder (e.g. "Downloads"), or ~/<name> elsewhere."""
    guid = KNOWN_FOLDERS.get(name, (None, None))[0]
    if sys.platform == "win32" and guid:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID(guid)
        g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
            try:
                return Path(out.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(out)
        return None
    p = Path.home() / name
    return p if p.is_dir() else None


@dataclass(frozen=True)
class Root:
    path: Path
    label: str  # shown to the user, e.g. "Hämtade filer"


def resolve_roots(entries: list[str]) -> list[Root]:
    """"known:Downloads" -> the Downloads Known Folder; anything else is a path."""
    roots: list[Root] = []
    for e in entries:
        if e.lower().startswith("known:"):
            name = e.split(":", 1)[1]
            p = known_folder(name)
            label = KNOWN_FOLDERS.get(name, (None, name))[1]
        else:
            p, label = Path(os.path.expandvars(e)).expanduser(), Path(e).name or e
        if p and p.is_dir():
            roots.append(Root(p.resolve(), label))
        else:
            log.warning("Watched folder not found: %s", e)
    return roots


def is_secret_name(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_PATTERNS)


def _is_temp(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in TEMP_PATTERNS)


def _is_cloud_placeholder(tag: int) -> bool:
    # OneDrive "files on demand" (IO_REPARSE_TAG_CLOUD_*: 0x9000001A, 0x9000101A ... 0x9000F01A).
    # Not a link: the entry stays where it is. stat() never downloads it.
    return tag & 0xFFFF0FFF == 0x9000001A


def _skip_entry(entry: os.DirEntry, st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    if entry.is_symlink():
        return True  # a link could lead outside the watched folders
    if attrs & _REPARSE:
        # Junctions, mount points and symlinks are reparse points. Only cloud placeholders
        # are allowed; every other (or unknown) reparse point is skipped.
        tag = getattr(st, "st_reparse_tag", 0)
        if not tag:
            try:
                tag = getattr(os.lstat(entry.path), "st_reparse_tag", 0)
            except OSError:
                return True
        if not _is_cloud_placeholder(tag):
            return True
    if entry.name.startswith(".") or attrs & (_HIDDEN | _SYSTEM):
        return True
    return False


@dataclass
class FileRecord:
    path: str
    name: str
    ext: str
    size: int
    mtime: float
    root: str  # label of the watched folder
    rel_dir: str  # folder inside the root ("" = top level)


def scan(roots: list[Root], *, max_depth: int, max_files: int) -> tuple[list[FileRecord], bool]:
    """Walk the roots using listings and stat only. Returns (records, truncated)."""
    out: list[FileRecord] = []
    for root in roots:
        stack: list[tuple[Path, int]] = [(root.path, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                it = os.scandir(folder)
            except OSError:
                continue
            with it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if _skip_entry(entry, st):
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if entry.name.lower() not in SKIP_DIRS and depth + 1 <= max_depth:
                            stack.append((Path(entry.path), depth + 1))
                        continue
                    if not stat.S_ISREG(st.st_mode) or is_secret_name(entry.name) or _is_temp(entry.name):
                        continue
                    rel = os.path.relpath(os.path.dirname(entry.path), root.path)
                    out.append(
                        FileRecord(
                            path=entry.path,
                            name=entry.name,
                            ext=os.path.splitext(entry.name)[1].lower().lstrip("."),
                            size=st.st_size,
                            mtime=st.st_mtime,
                            root=root.label,
                            rel_dir="" if rel == "." else rel,
                        )
                    )
                    if len(out) >= max_files:
                        return out, True
    return out, False


class FileIndex:
    """SQLite store of FileRecords (metadata only). ":memory:" when not persisted."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, name TEXT, name_lc TEXT, ext TEXT, "
                "size INTEGER, mtime REAL, root TEXT, rel_dir TEXT)"
            )
            self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            self._db.execute("CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime)")
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def replace_all(self, records: list[FileRecord]) -> list[FileRecord]:
        """Store a fresh scan. Returns the records whose path was not indexed before."""
        with self._lock:
            known = {r[0] for r in self._db.execute("SELECT path FROM files")}
            new = [r for r in records if r.path not in known]
            self._db.execute("DELETE FROM files")
            self._db.executemany(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)",
                [(r.path, r.name, r.name.lower(), r.ext, r.size, r.mtime, r.root, r.rel_dir) for r in records],
            )
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)", (str(time.time()),))
            self._db.commit()
        return new

    def by_name(self, name: str) -> list[FileRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT path,name,ext,size,mtime,root,rel_dir FROM files WHERE name_lc=? ORDER BY mtime DESC", (name.lower(),)
            ).fetchall()
        return [FileRecord(*r) for r in rows]

    def contains(self, path: str) -> FileRecord | None:
        with self._lock:
            row = self._db.execute("SELECT path,name,ext,size,mtime,root,rel_dir FROM files WHERE path=?", (path,)).fetchone()
        return FileRecord(*row) if row else None

    def search(self, words: list[str], *, exts: list[str] | None = None, since: float | None = None, limit: int = 20) -> list[FileRecord]:
        sql = "SELECT path,name,ext,size,mtime,root,rel_dir FROM files WHERE 1=1"
        args: list = []
        for w in words:
            sql += " AND name_lc LIKE ? ESCAPE '\\'"
            args.append("%" + w.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if exts:
            sql += f" AND ext IN ({','.join('?' * len(exts))})"
            args.extend(e.lower().lstrip(".") for e in exts)
        if since is not None:
            sql += " AND mtime >= ?"
            args.append(since)
        sql += " ORDER BY mtime DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [FileRecord(*row) for row in self._db.execute(sql, args)]

    def close(self) -> None:
        with self._lock:
            self._db.close()
