"""Consistent copies of the data folder: backups and the bundle used to set up a server.

The database is copied with SQLite's backup API, so it is safe even while the
server is running. The bundle may contain .env (API keys), so it is written to
dist/, which git ignores.
"""
from __future__ import annotations

import io
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

DB_NAME = "healthlingo.db"
CODE_FILES = ["run.py", "app.js", "healthlingo_v1.html", "manifest.webmanifest", "sw.js", "requirements.txt", "README.md", "NUTRITION.md", ".env.example"]
CODE_DIRS = ["backend", "deploy", "icons"]
BUNDLE_PREFIX = "healthlingo/"


def _clean(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _add_data(tar: tarfile.TarFile, data_dir: Path, prefix: str) -> dict:
    """Add data/<db snapshot>, secret.key and uploads. Returns counts."""
    counts = {"database": False, "files": 0}
    db = data_dir / DB_NAME
    if db.is_file():
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / DB_NAME
            src = sqlite3.connect(db)
            dst = sqlite3.connect(copy)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            tar.add(copy, f"{prefix}data/{DB_NAME}", filter=_clean)
        counts["database"] = True
    if data_dir.is_dir():
        for path in sorted(data_dir.rglob("*")):
            rel = path.relative_to(data_dir)
            # The live database (and its -wal/-shm files) is replaced by the snapshot above.
            if not path.is_file() or path.name.startswith(DB_NAME):
                continue
            tar.add(path, f"{prefix}data/{rel.as_posix()}", filter=_clean, recursive=False)
            counts["files"] += 1
    return counts


def make_backup(data_dir: Path, out_path: Path) -> dict:
    """data/ snapshot as a .tar.gz (entries start with data/)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        return _add_data(tar, data_dir, "")


def make_server_bundle(project_root: Path, data_dir: Path, out_path: Path,
                       include_data: bool = True, include_env: bool = True) -> dict:
    """Code (+ optionally .env and a data snapshot) under healthlingo/ in a .tar.gz."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {"code_files": 0, "env": False, "database": False, "files": 0}
    with tarfile.open(out_path, "w:gz") as tar:
        paths = [project_root / name for name in CODE_FILES]
        for folder in CODE_DIRS:
            base = project_root / folder
            if base.is_dir():
                paths += sorted(p for p in base.rglob("*") if p.is_file())
        for path in paths:
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = BUNDLE_PREFIX + path.relative_to(project_root).as_posix()
            if path.suffix == ".sh":
                # Shell scripts must have Unix line endings to run on the server.
                _add_bytes(tar, name, path.read_bytes().replace(b"\r\n", b"\n"), mode=0o755)
            else:
                tar.add(path, name, filter=_clean)
            summary["code_files"] += 1
        env = project_root / ".env"
        if include_env and env.is_file():
            _add_bytes(tar, BUNDLE_PREFIX + ".env", env.read_bytes(), mode=0o600)
            summary["env"] = True
        if include_data:
            summary.update(_add_data(tar, data_dir, BUNDLE_PREFIX))
    return summary
