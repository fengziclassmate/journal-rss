"""Export Zotero read feed items into the persistent RSS suppression list."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rss_read_filter import hash_tokens, identity_tokens, key_id, load_suppression


@dataclass(frozen=True)
class ExportResult:
    database: Path
    read_items: int
    previous_hashes: int
    new_hashes: int
    total_hashes: int


def _read_records(database: Path) -> list[dict[str, str]]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT fi.itemID, fi.guid, fi.readTime,
                   MAX(CASE WHEN f.fieldName='DOI' THEN v.value END) AS doi,
                   MAX(CASE WHEN f.fieldName='url' THEN v.value END) AS url,
                   MAX(CASE WHEN f.fieldName='title' THEN v.value END) AS title
            FROM feedItems fi
            LEFT JOIN itemData d ON d.itemID=fi.itemID
            LEFT JOIN fields f ON f.fieldID=d.fieldID
            LEFT JOIN itemDataValues v ON v.valueID=d.valueID
            WHERE fi.readTime IS NOT NULL
            GROUP BY fi.itemID, fi.guid, fi.readTime
            """
        ).fetchall()
        return [{key: row[key] or "" for key in ("guid", "doi", "url", "title")} for row in rows]
    finally:
        connection.close()


def _database_is_readable(database: Path, timeout: float = 1) -> None:
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro", uri=True, timeout=timeout
    )
    try:
        connection.execute("SELECT COUNT(*) FROM feedItems").fetchone()
    finally:
        connection.close()


def _file_state(path: Path) -> tuple[int, int] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def create_database_snapshot(
    database: Path, destination_dir: Path, attempts: int = 5
) -> Path:
    """Copy a live SQLite database and WAL into a validated readable snapshot."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    snapshot = destination_dir / database.name
    source_wal = Path(str(database) + "-wal")
    snapshot_wal = Path(str(snapshot) + "-wal")
    errors = []
    for attempt in range(1, attempts + 1):
        for path in (snapshot, snapshot_wal, Path(str(snapshot) + "-shm")):
            path.unlink(missing_ok=True)
        before = (_file_state(database), _file_state(source_wal))
        try:
            shutil.copy2(database, snapshot)
            if source_wal.exists():
                shutil.copy2(source_wal, snapshot_wal)
            after = (_file_state(database), _file_state(source_wal))
            if before != after:
                raise RuntimeError("Zotero database changed during snapshot copy")
            connection = sqlite3.connect(snapshot, timeout=2)
            try:
                check = connection.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise RuntimeError(f"snapshot integrity check failed: {check}")
                connection.execute("SELECT COUNT(*) FROM feedItems").fetchone()
            finally:
                connection.close()
            return snapshot
        except (OSError, sqlite3.Error, RuntimeError) as error:
            errors.append(f"attempt {attempt}: {error}")
            time.sleep(0.2 * attempt)
    raise RuntimeError("Unable to create current Zotero database snapshot: " + "; ".join(errors))


def choose_readable_database(database: Path, snapshot_dir: Path | None = None) -> Path:
    errors = []
    try:
        _database_is_readable(database)
        return database
    except sqlite3.Error as error:
        errors.append(f"{database}: {error}")
    if snapshot_dir is not None:
        try:
            return create_database_snapshot(database, snapshot_dir)
        except (OSError, sqlite3.Error, RuntimeError) as error:
            errors.append(f"current snapshot: {error}")
    backups = sorted(
        database.parent.glob(f"{database.name}*.bak"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in backups:
        try:
            _database_is_readable(candidate)
            return candidate
        except sqlite3.Error as error:
            errors.append(f"{candidate}: {error}")
    raise RuntimeError("No readable Zotero database found: " + "; ".join(errors))


def export_read_state(database: Path, suppression: Path, key: bytes) -> ExportResult:
    records = _read_records(database)
    previous = load_suppression(suppression, key) if suppression.exists() else set()
    discovered: set[str] = set()
    for record in records:
        discovered.update(
            hash_tokens(
                identity_tokens(
                    guid=record["guid"],
                    doi=record["doi"],
                    link=record["url"],
                    title=record["title"],
                ),
                key,
            )
        )
    combined = previous | discovered
    new_hashes = combined - previous
    if new_hashes or not suppression.exists():
        payload = {
            "version": 1,
            "algorithm": "hmac-sha256",
            "key_id": key_id(key),
            "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "source_database": database.name,
            "source_database_modified_at": dt.datetime.fromtimestamp(
                database.stat().st_mtime, dt.timezone.utc
            ).replace(microsecond=0).isoformat(),
            "read_items_seen": len(records),
            "hashes": sorted(combined),
        }
        suppression.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=suppression.parent, delete=False, newline="\n"
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            temporary = Path(stream.name)
        temporary.replace(suppression)
    return ExportResult(
        database=database,
        read_items=len(records),
        previous_hashes=len(previous),
        new_hashes=len(new_hashes),
        total_hashes=len(combined),
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, text=True, capture_output=True
    )


def push_suppression(repo: Path, suppression: Path, new_hashes: int) -> str:
    if new_hashes == 0:
        return "unchanged"
    relative = suppression.resolve().relative_to(repo.resolve())
    _git(repo, "add", "--", str(relative))
    committed = _git(
        repo,
        "commit",
        "--only",
        "-m",
        "Update Zotero read suppression",
        "--",
        str(relative),
        check=False,
    )
    if committed.returncode != 0:
        if "nothing to commit" in (committed.stdout + committed.stderr).lower():
            return "unchanged"
        raise RuntimeError(committed.stdout + committed.stderr)
    _git(repo, "push", "origin", "HEAD:main")
    return "pushed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(r"F:\Zotero\zotero.sqlite"))
    parser.add_argument("--suppression", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    key = args.key_file.read_text(encoding="ascii").strip().encode("ascii")
    if args.push:
        if not args.repo:
            parser.error("--push requires --repo")
        _git(args.repo, "pull", "--ff-only", "origin", "main")
    with tempfile.TemporaryDirectory(prefix="zotero-read-sync-") as directory:
        database = choose_readable_database(args.database, Path(directory))
        result = export_read_state(database, args.suppression, key)
        push_result = "disabled"
        if args.push:
            push_result = push_suppression(args.repo, args.suppression, result.new_hashes)
    print(
        json.dumps(
            {
                "database": str(result.database),
                "read_items": result.read_items,
                "previous_hashes": result.previous_hashes,
                "new_hashes": result.new_hashes,
                "total_hashes": result.total_hashes,
                "git": push_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
