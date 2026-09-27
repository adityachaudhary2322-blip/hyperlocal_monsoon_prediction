"""scripts/manage_users.py - the account tool for the cloud database.

The properties worth testing here are all safety properties, because that is what the
script is for: a password must never reach argv or the console, a hash must never be
displayed, a connection string must never be printed even inside an error, and it must
be impossible to deactivate the last admin and lock everyone out of the site.

Everything runs against a temporary SQLite file. `use_url` normalises any URL, so the
same code path is exercised without needing a Postgres server.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import bcrypt
import pytest

from src.common import ROOT
from src.db.models import AuditLog, User
from src.db.session import create_all, reset_engine, session_scope, use_url

SCRIPT = ROOT / "scripts" / "manage_users.py"
URL_ENV = "MONSOON_TEST_DB_URL"
PASSWORD_ENV = "MONSOON_TEST_PASSWORD"


@pytest.fixture
def manage():
    """Import the script by path - scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("manage_users", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["manage_users"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("manage_users", None)
    use_url(None)
    reset_engine()


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A seeded SQLite database, addressed the way the script addresses one."""
    path = tmp_path / "users.db"
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setenv(URL_ENV, f"sqlite:///{path}")

    use_url(f"sqlite:///{path}")
    create_all()
    with session_scope() as session:
        session.add_all([
            User(username="boss", name="Boss", password_hash=_hash("original-pw"),
                 role="admin", assigned_states="", active=True),
            User(username="officer_bihar", name="Bihar Officer",
                 password_hash=_hash("officer-pw"), role="officer",
                 assigned_states="Bihar", active=True),
        ])
    use_url(None)
    reset_engine()
    yield path
    use_url(None)
    reset_engine()


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _run(manage, database, *arguments) -> int:
    return manage.main(["--db-url-env", URL_ENV, "--actor", "pytest", *arguments])


def _users(database) -> dict[str, User]:
    use_url(f"sqlite:///{database}")
    try:
        with session_scope() as session:
            return {u.username: u for u in session.query(User).all()}
    finally:
        use_url(None)
        reset_engine()


# ==========================================================================
# Nothing secret is ever printed
# ==========================================================================
def test_list_shows_accounts_but_never_a_hash(manage, database, capsys):
    assert _run(manage, database, "list") == 0
    printed = capsys.readouterr().out

    assert "boss" in printed
    assert "officer_bihar" in printed
    assert "Bihar" in printed

    stored = _users(database)
    for user in stored.values():
        assert user.password_hash not in printed
        # bcrypt hashes start $2b$; catch any partial leak too.
        assert user.password_hash[:20] not in printed
    assert "$2b$" not in printed
    assert "original-pw" not in printed


def test_the_database_url_is_never_printed(manage, database, capsys):
    """Only the variable name it came from may appear."""
    _run(manage, database, "list")
    printed = capsys.readouterr().out

    assert str(database) not in printed
    assert "sqlite:///" not in printed
    assert URL_ENV in printed


def test_errors_are_scrubbed_of_credentials(manage):
    url = "postgresql+psycopg2://alice:sup3rs3cret@db.example:5432/monsoon"
    cleaned = manage.scrub(f"OperationalError: could not connect to {url}", url)
    for secret in ("sup3rs3cret", "alice", "db.example"):
        assert secret not in cleaned


def test_there_is_no_password_command_line_flag():
    """argv is world-readable in the process list and lands in shell history."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"--password"' not in source
    assert "'--password'" not in source
    # The only non-interactive route is an environment variable.
    assert "--password-env" in source
    assert "getpass" in source


def test_a_password_is_never_written_to_the_audit_log(manage, database,
                                                      monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "a-brand-new-secret")
    _run(manage, database, "reset-password", "boss", "--password-env", PASSWORD_ENV)

    use_url(f"sqlite:///{database}")
    try:
        with session_scope() as session:
            entries = session.query(AuditLog).all()
            assert entries
            for entry in entries:
                assert "a-brand-new-secret" not in (entry.detail or "")
    finally:
        use_url(None)
        reset_engine()


# ==========================================================================
# Reading a password
# ==========================================================================
def test_getpass_is_used_and_the_entry_is_confirmed(manage, monkeypatch):
    asked = []

    def fake_getpass(prompt=""):
        asked.append(prompt)
        return "typed-in-password"

    monkeypatch.setattr(manage.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(manage.sys.stdin, "isatty", lambda: True, raising=False)

    assert manage.read_new_password("boss", None) == "typed-in-password"
    assert len(asked) == 2, "the password must be confirmed, not taken on one entry"


def test_mismatched_confirmation_changes_nothing(manage, monkeypatch):
    from src.report import DataError

    answers = iter(["first-entry", "second-entry"])
    monkeypatch.setattr(manage.getpass, "getpass", lambda _p="": next(answers))
    monkeypatch.setattr(manage.sys.stdin, "isatty", lambda: True, raising=False)

    with pytest.raises(DataError, match="did not match"):
        manage.read_new_password("boss", None)


def test_no_terminal_and_no_env_var_is_a_clear_refusal(manage, monkeypatch):
    """Better than silently reading a truncated line from a pipe."""
    from src.report import DataError

    monkeypatch.setattr(manage.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(DataError, match="--password-env"):
        manage.read_new_password("boss", None)


# ==========================================================================
# create
# ==========================================================================
def test_create_adds_an_officer_with_a_working_password(manage, database,
                                                        monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "officer-secret")
    assert _run(manage, database, "create", "officer_latur", "--role", "officer",
                "--states", "Maharashtra", "--password-env", PASSWORD_ENV) == 0

    created = _users(database)["officer_latur"]
    assert created.role == "officer"
    assert created.states() == ["Maharashtra"]
    assert created.active is True
    assert bcrypt.checkpw(b"officer-secret", created.password_hash.encode())


def test_create_refuses_an_existing_username(manage, database, monkeypatch):
    from src.report import DataError

    monkeypatch.setenv(PASSWORD_ENV, "whatever-secret")
    with pytest.raises(DataError, match="already exists"):
        _run(manage, database, "create", "boss", "--role", "admin",
             "--password-env", PASSWORD_ENV)

    # The original password must survive a refused create.
    assert bcrypt.checkpw(b"original-pw", _users(database)["boss"].password_hash.encode())


def test_an_officer_needs_states_and_an_unknown_state_is_refused(manage):
    from src.report import DataError

    with pytest.raises(DataError, match="needs --states"):
        manage.parse_states(None, "officer")
    with pytest.raises(DataError, match="unknown state"):
        manage.parse_states("Bihar,Kerala", "officer")

    # GADM spelling, pipe-joined the way User.states() splits it.
    assert manage.parse_states("Bihar,Madhya Pradesh", "officer") == \
        "Bihar|Madhya Pradesh"


def test_an_admin_is_scoped_to_every_state(manage):
    assert manage.parse_states("Bihar", "admin") == ""
    assert manage.parse_states(None, "admin") == ""


# ==========================================================================
# reset-password
# ==========================================================================
def test_reset_password_replaces_the_hash(manage, database, monkeypatch):
    before = _users(database)["boss"].password_hash

    monkeypatch.setenv(PASSWORD_ENV, "replacement-secret")
    assert _run(manage, database, "reset-password", "boss",
                "--password-env", PASSWORD_ENV) == 0

    after = _users(database)["boss"]
    assert after.password_hash != before
    assert bcrypt.checkpw(b"replacement-secret", after.password_hash.encode())
    assert not bcrypt.checkpw(b"original-pw", after.password_hash.encode())


def test_reset_password_on_an_unknown_user_is_an_error(manage, database,
                                                       monkeypatch):
    from src.report import DataError

    monkeypatch.setenv(PASSWORD_ENV, "irrelevant-secret")
    with pytest.raises(DataError, match="no such user"):
        _run(manage, database, "reset-password", "nobody",
             "--password-env", PASSWORD_ENV)


# ==========================================================================
# The lockout guard
# ==========================================================================
def test_the_last_active_admin_cannot_be_deactivated(manage, database):
    from src.report import DataError

    with pytest.raises(DataError, match="only active admin"):
        _run(manage, database, "deactivate", "boss")

    assert _users(database)["boss"].active is True


def test_an_admin_can_be_retired_once_another_one_exists(manage, database,
                                                         monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "successor-secret")
    _run(manage, database, "create", "newboss", "--role", "admin",
         "--password-env", PASSWORD_ENV)
    assert _run(manage, database, "deactivate", "boss") == 0

    stored = _users(database)
    assert stored["boss"].active is False
    assert stored["newboss"].active is True
    # Deactivation keeps the row, so the audit trail still resolves the username.
    assert "boss" in stored


def test_deactivating_an_officer_needs_no_second_officer(manage, database):
    assert _run(manage, database, "deactivate", "officer_bihar") == 0
    assert _users(database)["officer_bihar"].active is False


def test_every_change_is_recorded_in_the_audit_log(manage, database, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "audited-secret")
    _run(manage, database, "create", "officer_gaya", "--role", "officer",
         "--states", "Bihar", "--password-env", PASSWORD_ENV)
    _run(manage, database, "reset-password", "boss", "--password-env", PASSWORD_ENV)
    _run(manage, database, "deactivate", "officer_bihar")

    use_url(f"sqlite:///{database}")
    try:
        with session_scope() as session:
            actions = [e.action for e in session.query(AuditLog)
                       .order_by(AuditLog.id).all()]
    finally:
        use_url(None)
        reset_engine()

    assert actions == ["user_create", "user_reset_password", "user_deactivate"]


def test_listing_does_not_write_anything(manage, database):
    before = {name: (u.password_hash, u.active, u.role)
              for name, u in _users(database).items()}
    _run(manage, database, "list")
    after = {name: (u.password_hash, u.active, u.role)
             for name, u in _users(database).items()}
    assert before == after
