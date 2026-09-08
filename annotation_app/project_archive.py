"""Read-only project sharing. This belongs to project storage, not MS calling."""
from __future__ import annotations

import contextlib
import ctypes
import sys
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import uuid
import zipfile


def _inventory(root: Path):
    files = {}
    directories = []
    excluded = 0
    def fail(error):
        raise error
    for parent, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        for name in list(dirs) + names:
            path = Path(parent) / name
            if name == "__MACOSX" or name == ".DS_Store" or name.startswith("._"):
                excluded += 1
                if name in dirs:
                    dirs.remove(name)
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("项目含链接目录或文件，请先制作不含链接的独立副本。")
            relative = path.relative_to(root).as_posix()
            if name in dirs:
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = (info.st_size, info.st_mtime_ns, info.st_ino)
            else:
                raise ValueError("项目包含无法打包的特殊文件。")
    return files, sorted(directories), excluded


def share_project(root: Path, parent: Path, *, database: Path) -> dict:
    """Publish a new ZIP only after a consistent, complete snapshot succeeds.

    SQLite online backup includes committed WAL data. A read-only monitoring
    connection detects concurrent commits; non-database files are checked again
    before publication. Callers also serialize their own project mutations.
    External raw references are not followed. No audit row is written by sharing.
    """
    root = root.resolve(strict=True)
    parent = parent.expanduser().resolve(strict=True)
    if not root.is_dir() or not parent.is_dir():
        raise ValueError("请选择已有的保存文件夹。")
    if parent == root or root in parent.parents:
        raise ValueError("请选择项目文件夹以外的位置保存 ZIP。")
    required_db = database.resolve(strict=True).relative_to(root).as_posix()
    files, directories, excluded = _inventory(root)
    if required_db not in files:
        raise ValueError("项目的审阅数据库不在可打包文件中。")
    # Retired databases can be immutable, hash-bound project artifacts: preserve
    # their exact bytes. Only the active mutable review DB needs an online backup.
    databases = {required_db}
    with (root / required_db).open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("项目的审阅数据库无效。")
    sidecars = {db + suffix for db in databases for suffix in ("-wal", "-shm", "-journal")}
    payload_files = {name: info for name, info in files.items() if name not in sidecars}
    stable_files = {name: info for name, info in payload_files.items() if name not in databases}
    filename = f"{root.name}-share-{uuid.uuid4().hex[:12]}.zip"
    destination = parent / filename
    with tempfile.TemporaryDirectory(prefix=".studio-share-", dir=parent) as staging:
        staging = Path(staging)
        with contextlib.ExitStack() as stack:
            readers = {}
            snapshots = {}
            for index, name in enumerate(sorted(databases)):
                reader = sqlite3.connect((root / name).as_uri() + "?mode=ro", timeout=10, uri=True)
                stack.callback(reader.close)
                reader.execute("PRAGMA query_only=ON")
                revision = reader.execute("PRAGMA data_version").fetchone()[0]
                readers[name] = (reader, revision)
                snapshot = staging / f"snapshot-{index}.sqlite"
                with contextlib.closing(sqlite3.connect(snapshot)) as target:
                    reader.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise ValueError("项目数据库完整性检查失败，未生成 ZIP。")
                snapshots[name] = snapshot
            temporary = staging / "project.zip"
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True) as archive:
                archive.writestr(root.name + "/", b"")
                for name in directories:
                    archive.writestr(f"{root.name}/{name}/", b"")
                for name in sorted(payload_files):
                    archive.write(snapshots.get(name, root / name), f"{root.name}/{name}")
            after, after_dirs, _ = _inventory(root)
            if set(after).difference(sidecars) != set(payload_files) or after_dirs != directories:
                raise ValueError("打包期间项目文件发生变化，请停止编辑后重试。")
            if any(after[name] != info for name, info in stable_files.items()):
                raise ValueError("打包期间项目内容发生变化，请停止编辑后重试。")
            for name, (reader, revision) in readers.items():
                if reader.execute("PRAGMA data_version").fetchone()[0] != revision:
                    raise ValueError("打包期间审阅结果发生变化，请停止编辑后重试。")
                if after[name][2] != files[name][2]:
                    raise ValueError("打包期间数据库被替换，请重新打开项目。")
            # Atomic publication without overwriting any existing file.
            if os.name == "nt":
                os.rename(temporary, destination)
            elif sys.platform == "darwin":
                # Darwin RENAME_EXCL rejects an existing destination atomically.
                rename = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).renamex_np
                rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
                rename.restype = ctypes.c_int
                if rename(os.fsencode(temporary), os.fsencode(destination), 0x4):
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error), str(destination))
            else:
                os.link(temporary, destination)
    return {"filename": filename, "file_count": len(payload_files), "excluded_count": excluded}
