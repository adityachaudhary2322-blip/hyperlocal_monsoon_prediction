"""Copy data/monsoon.db into the database named by DATABASE_URL.

Run:  python -m src.db.migrate_to_postgres [--source data/monsoon.db] [--reset]

Why this exists: a Hugging Face Space has no persistent disk, so the SQLite file built
locally cannot travel with the image. This lifts the whole control panel - units,
forecast runs, advisories, subscribers, users, audit log - into Postgres once, and the
Space then reads and writes it there.

Two things it does that a naive `INSERT ... SELECT` loop would get wrong:

* **Tables are copied in dependency order** (`metadata.sorted_tables`), so a message
  never lands before the subscriber it points at. Foreign keys are on in both engines.
* **Sequences are resynchronised afterwards.** Rows keep their original primary keys, so
  Postgres' identity sequences are still sitting at 1 and the first row the app inserts
  would collide with an imported id. Every serial column is advanced past `max(id)`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import func, insert, inspect, select
from sqlalchemy.engine import make_url

from src.db.models import Base
from src.db.session import (DB_PATH, database_url, describe, is_postgres,
                            normalize_url, reset_engine)
from src.report import DataError, summarize

# Batched so a table with ~10k forecasts is one round trip per chunk rather than
# one per row - over a network connection that is the difference between seconds
# and minutes.
CHUNK = 1000


def _serial_columns(table) -> list[str]:
    """Integer primary keys Postgres will have created a sequence for."""
    return [c.name for c in table.primary_key.columns
            if c.autoincrement and c.type.python_type is int]


def copy_table(source_engine, target_engine, table) -> int:
    with source_engine.connect() as source:
        rows = [dict(r._mapping) for r in source.execute(select(table))]
    if not rows:
        return 0

    with target_engine.begin() as target:
        for start in range(0, len(rows), CHUNK):
            target.execute(insert(table), rows[start:start + CHUNK])
    return len(rows)


def resync_sequences(target_engine, tables) -> list[str]:
    """Advance each identity sequence past the largest imported id."""
    advanced = []
    with target_engine.begin() as connection:
        for table in tables:
            for column in _serial_columns(table):
                highest = connection.execute(
                    select(func.max(table.c[column]))).scalar()
                if highest is None:
                    continue
                # setval(..., is_called=true) makes the NEXT value highest + 1.
                connection.exec_driver_sql(
                    "SELECT setval(pg_get_serial_sequence(%(table)s, %(column)s), "
                    "%(value)s, true)",
                    {"table": table.name, "column": column, "value": int(highest)},
                )
                advanced.append(f"{table.name}.{column} -> {highest}")
    return advanced


def scrub(text: str, url: str) -> str:
    """Remove a connection string and its password from text bound for the console."""
    parsed = make_url(url)
    out = text.replace(url, "<target url>")
    for secret in (parsed.password, parsed.username, parsed.host):
        if secret:
            out = out.replace(str(secret), "***")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DB_PATH),
                        help="SQLite file to copy from")
    parser.add_argument("--reset", action="store_true",
                        help="drop and recreate the target tables first")
    parser.add_argument("--target", default=None,
                        help="target URL, overriding DATABASE_URL. Accepts a SQLite "
                             "URL so the copy can be rehearsed without a Postgres "
                             "server; sequence resync is then skipped.")
    parser.add_argument("--target-env", default=None, metavar="NAME",
                        help="name of an environment variable holding the target URL, "
                             "e.g. --target-env DATABASE_URL_CLOUD. Preferred over "
                             "--target for a real database: the connection string "
                             "stays out of the shell history and the process list.")
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.is_file():
        raise DataError(f"missing source database: {source_path}")

    print("--- db.migrate_to_postgres ---")
    if args.target_env:
        from src.runtime import env

        raw = env(args.target_env, "") or ""
        if not raw.strip():
            raise DataError(
                f"{args.target_env} is not set in .env, so there is no target to "
                "migrate to. Add it, or pass --target-env with the right name."
            )
        target_url = normalize_url(raw)
        # The URL carries a password, so only the variable name is ever shown.
        target_name = f"{make_url(target_url).drivername.split('+')[0]} "                       f"(from {args.target_env})"
    elif args.target:
        target_url = normalize_url(args.target)
        target_name = target_url.split("://", 1)[0]
    else:
        if not is_postgres():
            raise DataError(
                "DATABASE_URL is not set to a Postgres database, so there is nothing "
                f"to migrate to (it currently resolves to {describe()}). Set "
                "DATABASE_URL in .env, e.g. "
                "postgresql://user:pass@host:5432/monsoon, or pass --target to "
                "rehearse the copy against another file."
            )
        target_url = database_url()
        target_name = describe()

    postgres_target = target_url.startswith("postgresql")
    if str(source_path.resolve()) in target_url:
        raise DataError("the source and the target are the same database")

    print(f"  from: {source_path}")
    print(f"  to  : {target_name}")

    # Two engines at once, so the module-level cache is bypassed deliberately.
    from sqlalchemy import create_engine as make_engine
    source_engine = make_engine(f"sqlite:///{source_path}", future=True)
    target_engine = make_engine(target_url, future=True, pool_pre_ping=True)

    present = set(inspect(source_engine).get_table_names())
    missing = [t.name for t in Base.metadata.sorted_tables if t.name not in present]
    if missing:
        print(f"  not in the source, skipped: {', '.join(missing)}")

    try:
        if args.reset:
            Base.metadata.drop_all(target_engine)
            print("  dropped every target table")
        Base.metadata.create_all(target_engine)
    except Exception as error:
        raise DataError(
            "could not prepare the target schema: "
            f"{scrub(f'{type(error).__name__}: {error}', target_url)}"
        ) from None

    counts: dict[str, int] = {}
    try:
        for table in Base.metadata.sorted_tables:   # dependency order
            if table.name not in present:
                continue
            counts[table.name] = copy_table(source_engine, target_engine, table)
    except Exception as error:
        # A driver error can quote the DSN, which holds the password.
        raise DataError(
            f"copy failed on {table.name}: "
            f"{scrub(f'{type(error).__name__}: {error}', target_url)}"
        ) from None

    # Only Postgres has sequences to fix; SQLite's AUTOINCREMENT derives the next
    # rowid from the table itself, so an imported id can never be handed out twice.
    advanced = resync_sequences(
        target_engine,
        [t for t in Base.metadata.sorted_tables if t.name in counts],
    ) if postgres_target else []

    source_engine.dispose()
    target_engine.dispose()
    reset_engine()

    total = sum(counts.values())
    if total == 0:
        raise DataError(
            f"copied 0 rows from {source_path} - it is empty. Run "
            "python -m src.db.init first, or point --source at the right file."
        )

    summarize(
        "db.migrate_to_postgres",
        rows=total,
        files=[],
        extra={
            **{f"rows in {name}": n for name, n in counts.items()},
            "sequences advanced": len(advanced),
        },
    )
    for line in advanced:
        print(f"    sequence {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
