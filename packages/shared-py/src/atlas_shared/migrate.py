"""Tiny SQL migration runner. No Alembic ceremony.

Usage:
    uv run python -m atlas_shared.migrate up
    uv run python -m atlas_shared.migrate status

Migrations live in `packages/shared-py/migrations/NNNN_<name>.sql` and are
applied in lexical order, exactly once. The runner records applied filenames
in a `schema_migrations` table.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from atlas_shared.db import sync_dsn
from atlas_shared.logging import get_logger, setup_logging

setup_logging()
log = get_logger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _ensure_table(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )


def _applied(conn) -> set[str]:
    rows = conn.execute(text("SELECT filename FROM schema_migrations")).all()
    return {r[0] for r in rows}


def _files() -> list[Path]:
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def up() -> int:
    engine = create_engine(sync_dsn(), future=True)
    applied_count = 0
    with engine.begin() as conn:
        _ensure_table(conn)
        applied = _applied(conn)
        for path in _files():
            if path.name in applied:
                log.info("migrate.skip", file=path.name)
                continue
            log.info("migrate.apply", file=path.name)
            sql = path.read_text()
            conn.execute(text(sql))
            conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                {"f": path.name},
            )
            applied_count += 1
    log.info("migrate.done", applied=applied_count)
    return applied_count


def status() -> None:
    engine = create_engine(sync_dsn(), future=True)
    with engine.connect() as conn:
        _ensure_table(conn)
        applied = _applied(conn)
        for path in _files():
            mark = "✓" if path.name in applied else "·"
            print(f" {mark}  {path.name}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "up"
    if cmd == "up":
        up()
    elif cmd == "status":
        status()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
