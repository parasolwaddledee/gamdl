import sqlite3
from contextlib import closing
from pathlib import Path

from .models import QueueConfig, QueueError, TrackRef, download_url


DATABASE_FILENAME = "downloads.sqlite3"
LEGACY_DATABASE_FILENAMES = {"us": "us.sqlite3", "cn": "cn.sqlite3"}
LEGACY_MIGRATION = "merge-us-cn-databases-v1"


def ensure_download_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS media (
            id TEXT NOT NULL,
            path TEXT NOT NULL,
            source_url TEXT,
            source TEXT NOT NULL CHECK (source IN ('us', 'cn')),
            downloaded_at TEXT,
            PRIMARY KEY (source, id)
        )
        """
    )
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(media)")
    }
    if not {"id", "path", "source"}.issubset(columns):
        raise QueueError("Download database has an incompatible media table")
    if "source_url" not in columns:
        connection.execute("ALTER TABLE media ADD COLUMN source_url TEXT")
    if "downloaded_at" not in columns:
        connection.execute("ALTER TABLE media ADD COLUMN downloaded_at TEXT")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)"
    )


def _legacy_rows(database_path: Path) -> list[tuple[str, str, str | None]]:
    if not database_path.is_file():
        return []
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(media)")
        }
        if not {"id", "path"}.issubset(columns):
            raise QueueError(
                f"Legacy database has an incompatible media table: {database_path}"
            )
        if "source_url" in columns:
            rows = connection.execute(
                "SELECT id, path, source_url FROM media"
            ).fetchall()
        else:
            rows = [
                (row[0], row[1], None)
                for row in connection.execute("SELECT id, path FROM media").fetchall()
            ]
    return [
        (str(media_id), str(path), str(source_url) if source_url else None)
        for media_id, path, source_url in rows
    ]


def _legacy_backup_path(database_path: Path) -> Path:
    candidate = database_path.with_name(f"{database_path.name}.pre-merge.bak")
    counter = 1
    while candidate.exists():
        candidate = database_path.with_name(
            f"{database_path.name}.pre-merge-{counter}.bak"
        )
        counter += 1
    return candidate


def migrate_download_databases(state_dir: Path) -> Path:
    database_path = state_dir / DATABASE_FILENAME
    legacy_paths = {
        source: state_dir / filename
        for source, filename in LEGACY_DATABASE_FILENAMES.items()
    }
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            with connection:
                ensure_download_schema(connection)
                migrated = connection.execute(
                    "SELECT 1 FROM migrations WHERE name = ?",
                    (LEGACY_MIGRATION,),
                ).fetchone()
                if not migrated:
                    for source, legacy_path in legacy_paths.items():
                        connection.executemany(
                            """
                            INSERT INTO media (id, path, source_url, source)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(source, id) DO UPDATE SET
                                path = excluded.path,
                                source_url = COALESCE(
                                    excluded.source_url,
                                    media.source_url
                                )
                            """,
                            [(*row, source) for row in _legacy_rows(legacy_path)],
                        )
                    connection.execute(
                        "INSERT INTO migrations (name) VALUES (?)",
                        (LEGACY_MIGRATION,),
                    )
    except QueueError:
        raise
    except sqlite3.Error as error:
        raise QueueError(f"Could not migrate download database: {database_path}") from error

    for legacy_path in legacy_paths.values():
        if legacy_path.is_file():
            try:
                legacy_path.replace(_legacy_backup_path(legacy_path))
            except OSError:
                # Some host bind mounts allow SQLite writes but deny renames.
                # The migration marker makes an archive failure non-fatal.
                pass
    return database_path


def backfill_source_urls(
    database_path: Path,
    queue: QueueConfig,
    storefront: str,
) -> None:
    if not database_path.is_file():
        return
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            with connection:
                ensure_download_schema(connection)
                rows = connection.execute(
                    "SELECT id FROM media "
                    "WHERE source = ? AND "
                    "(source_url IS NULL OR TRIM(source_url) = '')",
                    (queue.key,),
                ).fetchall()
                connection.executemany(
                    "UPDATE media SET source_url = ? WHERE source = ? AND id = ?",
                    [
                        (
                            download_url(queue, storefront, str(row[0])),
                            queue.key,
                            str(row[0]),
                        )
                        for row in rows
                    ],
                )
    except sqlite3.Error as error:
        raise QueueError(
            f"Could not migrate download database: {database_path}"
        ) from error


def record_source_url(
    database_path: Path,
    queue: QueueConfig,
    track: TrackRef,
    source_url: str,
) -> None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            with connection:
                ensure_download_schema(connection)
                result = connection.execute(
                    "UPDATE media SET source_url = ? "
                    "WHERE source = ? AND id IN (?, ?)",
                    (source_url, queue.key, track.catalog_id, track.library_id),
                )
                if result.rowcount < 1:
                    raise QueueError("Downloaded media URL could not be registered")
    except QueueError:
        raise
    except sqlite3.Error as error:
        raise QueueError(f"Could not update download database: {database_path}") from error


def registered_download(
    database_path: Path,
    queue: QueueConfig,
    track: TrackRef,
) -> Path | None:
    if not database_path.is_file():
        return None
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT path FROM media WHERE source = ? AND id IN (?, ?) "
                "ORDER BY id = ? DESC LIMIT 1",
                (queue.key, track.catalog_id, track.library_id, track.catalog_id),
            ).fetchone()
    except sqlite3.Error as error:
        raise QueueError(f"Could not read download database: {database_path}") from error
    if not row:
        return None
    path = Path(row[0])
    return path if path.is_file() else None


def downloader_registered_download(
    database_path: Path,
    track: TrackRef,
) -> Path | None:
    if not database_path.is_file():
        return None
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            row = connection.execute(
                "SELECT path FROM media WHERE id IN (?, ?) "
                "ORDER BY id = ? DESC LIMIT 1",
                (track.catalog_id, track.library_id, track.catalog_id),
            ).fetchone()
    except sqlite3.Error as error:
        raise QueueError(
            f"Could not read downloader database: {database_path}"
        ) from error
    return Path(row[0]) if row else None


def record_download(
    database_path: Path,
    queue: QueueConfig,
    track: TrackRef,
    path: Path,
    source_url: str,
) -> None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            with connection:
                ensure_download_schema(connection)
                connection.execute(
                    """
                    INSERT INTO media (
                        id,
                        path,
                        source_url,
                        source,
                        downloaded_at
                    )
                    VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    ON CONFLICT(source, id) DO UPDATE SET
                        path = excluded.path,
                        source_url = excluded.source_url,
                        downloaded_at = excluded.downloaded_at
                    """,
                    (track.catalog_id, str(path), source_url, queue.key),
                )
                if track.library_id != track.catalog_id:
                    connection.execute(
                        "DELETE FROM media WHERE source = ? AND id = ?",
                        (queue.key, track.library_id),
                    )
    except sqlite3.Error as error:
        raise QueueError(f"Could not update download database: {database_path}") from error
