"""SQLite connection and transactional schema migration helpers."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_DATABASE_PATH = Path("data/gitmem.sqlite3")
MIGRATIONS_PATH = Path(__file__).parent / "migrations"


def connect_database(path: str | Path = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """Open a SQLite database with row access and foreign keys enabled."""
    db_path = str(path)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize_database(
    path: str | Path = DEFAULT_DATABASE_PATH,
    migrations_path: str | Path = MIGRATIONS_PATH,
) -> sqlite3.Connection:
    """Open a database, apply pending migrations, and return the live connection."""
    connection = connect_database(path)
    try:
        apply_migrations(connection, Path(migrations_path))
    except Exception:
        connection.close()
        raise
    return connection


def apply_migrations(
    connection: sqlite3.Connection,
    migrations_path: str | Path = MIGRATIONS_PATH,
) -> list[int]:
    """Apply unapplied, numerically ordered SQL migrations in transactions."""
    directory = Path(migrations_path)
    if not directory.is_dir():
        raise FileNotFoundError(f"Migration directory not found: {directory}")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row["version"])
        for row in connection.execute("SELECT version FROM schema_migrations")
    }

    migrations: list[tuple[int, Path]] = []
    for migration_file in directory.glob("[0-9]*.sql"):
        prefix = migration_file.name.split("_", maxsplit=1)[0]
        try:
            version = int(prefix)
        except ValueError as exc:
            raise ValueError(f"Invalid migration filename: {migration_file.name}") from exc
        migrations.append((version, migration_file))

    versions = [version for version, _ in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("Migration filenames must have unique numeric versions")

    newly_applied: list[int] = []
    for version, migration_file in sorted(migrations):
        if version in applied:
            continue
        sql = migration_file.read_text(encoding="utf-8")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _sql_statements(sql):
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    version,
                    migration_file.name,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        newly_applied.append(version)
    return newly_applied


def _sql_statements(script: str) -> list[str]:
    """Split a migration into complete SQL statements without splitting literals."""
    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines(keepends=True):
        current.append(line)
        if sqlite3.complete_statement("".join(current)):
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current.clear()
    remainder = "".join(current).strip()
    if remainder:
        raise ValueError(f"Incomplete SQL statement in migration: {remainder[:100]}")
    return statements
