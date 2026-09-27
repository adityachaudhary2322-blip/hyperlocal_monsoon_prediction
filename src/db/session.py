"""Engine and session factory.

Local development uses SQLite at `data/monsoon.db`. On Hugging Face Spaces the disk is
non-persistent, so the same code points at Postgres through **DATABASE_URL** and the
file path is never touched (README, "Deploying to Hugging Face Spaces").

Three portability details live here rather than being sprinkled through callers:

* `PRAGMA foreign_keys=ON` is a SQLite-only statement. Postgres enforces foreign keys
  natively and errors on the pragma, so the listener is attached per-dialect.
* Postgres connections die quietly behind a proxy or after an idle Space goes to
  sleep. `pool_pre_ping` checks a connection before handing it out, which turns a
  crashed page into a reconnect.
* `psycopg2://`-style URLs from hosting providers usually start `postgres://`, a
  scheme SQLAlchemy 2.x dropped. It is normalised here so a copy-pasted connection
  string works.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from src.common import ROOT
from src.db.models import Base

DB_PATH = ROOT / "data" / "monsoon.db"

_engine: Engine | None = None
_Session: sessionmaker | None = None
_url: str | None = None

# Set by `--db-url`, so a laptop run can write its Chronos-2 forecasts straight into
# the cloud database without exporting DATABASE_URL for the whole process.
_override: str | None = None


def use_url(url: str | None) -> None:
    """Point every later session at `url`. None restores DATABASE_URL."""
    global _override
    normalised = normalize_url(url) if url else None
    if normalised != _override:
        reset_engine()
    _override = normalised


def normalize_url(raw: str) -> str:
    """Make a provider-supplied URL usable by SQLAlchemy 2.x + psycopg2."""
    url = make_url(raw.strip())
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+psycopg2")
    return url.render_as_string(hide_password=False)


def database_url(path: Path | None = None) -> str:
    """Where to connect: an explicit path, then --db-url, then DATABASE_URL."""
    if path is not None:
        return f"sqlite:///{path}"
    if _override:
        return _override

    from src.runtime import env

    configured = env("DATABASE_URL", "") or ""
    if configured.strip():
        return normalize_url(configured)
    return f"sqlite:///{DB_PATH}"


def is_postgres(path: Path | None = None) -> bool:
    return database_url(path).startswith("postgresql")


def describe(path: Path | None = None) -> str:
    """A one-line description safe to show in the UI - never the password."""
    url = make_url(database_url(path))
    if url.drivername.startswith("sqlite"):
        name = Path(url.database or "").name or "in-memory"
        return f"SQLite · {name}"
    return f"Postgres · {url.host or '?'}/{url.database or '?'}"


def engine(path: Path | None = None) -> Engine:
    global _engine, _Session, _url
    target = database_url(path)

    if _engine is not None and target == _url:
        return _engine

    if target.startswith("sqlite"):
        file = Path(make_url(target).database or "")
        file.parent.mkdir(parents=True, exist_ok=True)
        new_engine = create_engine(target, future=True)

        # Foreign keys are off by default in SQLite, which would let a message
        # reference a deleted subscriber. Postgres needs no equivalent.
        @event.listens_for(new_engine, "connect")
        def _fk_on(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    else:
        new_engine = create_engine(
            target,
            future=True,
            pool_pre_ping=True,
            # A Space is one small container; a large pool just exhausts the
            # database's connection limit for no gain.
            pool_size=5,
            max_overflow=5,
            pool_recycle=280,
        )

    _engine = new_engine
    _url = target
    _Session = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Tests and DATABASE_URL changes need this."""
    global _engine, _Session, _url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
    _url = None


def create_all(path: Path | None = None) -> None:
    Base.metadata.create_all(engine(path))


def drop_all(path: Path | None = None) -> None:
    Base.metadata.drop_all(engine(path))


@contextmanager
def session_scope(path: Path | None = None):
    engine(path)
    assert _Session is not None
    session: Session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session(path: Path | None = None) -> Session:
    """A session the caller closes. Streamlit pages use this."""
    engine(path)
    assert _Session is not None
    return _Session()
