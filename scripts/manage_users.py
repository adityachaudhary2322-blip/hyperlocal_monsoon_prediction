"""Manage control-panel accounts in the cloud database.

    python scripts/manage_users.py list
    python scripts/manage_users.py create  <username> --role officer --states Bihar
    python scripts/manage_users.py reset-password <username>
    python scripts/manage_users.py deactivate <username>

Connects to **DATABASE_URL_CLOUD** from `.env` by default - the deployed site's
database, since that is where the accounts people actually log in with live.
`--db-url-env` names a different variable.

Three rules this script exists to keep:

* **The connection string is never printed.** Only the variable it came from is shown,
  and a driver error is scrubbed of its password, user and host before it reaches the
  console - psycopg2 quotes the DSN in some failures.
* **Passwords are never printed, never logged, and never passed as an argument.** They
  are read with `getpass`, so they do not echo and do not enter the shell history or
  the process list. Only the bcrypt hash is stored, and `list` shows neither.
* **You cannot lock everyone out.** Deactivating or demoting the last active admin is
  refused, because the only other way back in is `python -m src.db.init --reset`, which
  wipes the forecasts along with the accounts.

Every change is written to `audit_log`, with the actor taken from `--actor`.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from src.db.init import hash_password  # noqa: E402
from src.db.models import AuditLog, User  # noqa: E402
from src.db.session import database_url, session_scope, use_url  # noqa: E402
from src.report import DataError  # noqa: E402

ROLES = ("admin", "officer")
MIN_PASSWORD_LENGTH = 8
DEFAULT_URL_ENV = "DATABASE_URL_CLOUD"

# Officers are scoped by state; the strings must match `units.state` exactly, which is
# GADM's spelling (CLAUDE.md §5 - never guess one).
KNOWN_STATES = ("Uttar Pradesh", "NCT of Delhi", "Bihar", "Madhya Pradesh",
                "Maharashtra")


def scrub(text: str, url: str) -> str:
    """Remove a connection string and its parts from text bound for the console."""
    parsed = make_url(url)
    out = text.replace(url, "<database url>")
    for secret in (parsed.password, parsed.username, parsed.host):
        if secret:
            out = out.replace(str(secret), "***")
    return out


def connect(url_env: str) -> str:
    """Point the session layer at the named variable. Returns a safe label."""
    from src.runtime import env

    raw = (env(url_env, "") or "").strip()
    if not raw:
        raise DataError(
            f"{url_env} is not set in .env, so there is no database to manage. Add it, "
            f"or pass --db-url-env with the right name.")
    use_url(raw)
    driver = make_url(database_url()).drivername.split("+")[0]
    return f"{driver} (from {url_env})"


def read_new_password(username: str, password_env: str | None) -> str:
    """A password from getpass, confirmed. Never echoed, never in argv.

    `--password-env` is the non-interactive escape hatch, for a CI step or an
    automated rotation. It is an environment variable and not an argument for the same
    reason the rest of this script is careful: argv is world-readable in the process
    list, and it lands in the shell history.
    """
    if password_env:
        from src.runtime import env

        value = (env(password_env, "") or "").strip()
        if not value:
            raise DataError(f"{password_env} is not set")
        if len(value) < MIN_PASSWORD_LENGTH:
            print(f"  note: the password in {password_env} is shorter than "
                  f"{MIN_PASSWORD_LENGTH} characters")
        return value

    if not sys.stdin.isatty():
        raise DataError(
            "no terminal to read a password from. Run this interactively, or put the "
            "password in an environment variable and pass --password-env NAME.")

    first = getpass.getpass(f"  new password for {username}: ")
    if not first:
        raise DataError("empty password")
    if len(first) < MIN_PASSWORD_LENGTH:
        print(f"  note: shorter than {MIN_PASSWORD_LENGTH} characters")
    if getpass.getpass("  confirm: ") != first:
        raise DataError("the two entries did not match; nothing was changed")
    return first


def parse_states(raw: str | None, role: str) -> str:
    """Comma-separated states -> the pipe-joined form `User.states()` splits."""
    if role == "admin":
        if raw:
            print("  note: an admin sees every state; --states ignored")
        return ""
    if not raw or not raw.strip():
        raise DataError("an officer needs --states, e.g. --states Bihar")

    states = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in states if s not in KNOWN_STATES]
    if unknown:
        raise DataError(
            f"unknown state(s): {', '.join(unknown)}. Must match units.state exactly, "
            f"one of: {', '.join(KNOWN_STATES)}")
    return "|".join(states)


def active_admins(session, excluding: str | None = None) -> list[str]:
    query = session.query(User).filter(User.role == "admin", User.active.is_(True))
    return [u.username for u in query.all() if u.username != excluding]


def log(session, actor: str, action: str, username: str, detail: str) -> None:
    session.add(AuditLog(username=actor, action=action, entity="user",
                         entity_id=username, detail=detail))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def command_list(session, _args) -> int:
    users = session.query(User).order_by(User.role, User.username).all()
    if not users:
        print("  no accounts. Run python -m src.db.init to seed them.")
        return 0

    width = max(len(u.username) for u in users) + 2
    print(f"  {'username':<{width}}{'role':<10}{'active':<9}scope")
    print(f"  {'-' * (width - 2):<{width}}{'-' * 8:<10}{'-' * 7:<9}{'-' * 30}")
    for user in users:
        scope = ", ".join(user.states()) if user.states() else "all states"
        print(f"  {user.username:<{width}}{user.role:<10}"
              f"{'yes' if user.active else 'no':<9}{scope}")

    # Neither the password nor its hash is read or shown - the column is only ever
    # written, by this script and by db.init.
    print(f"\n  {len(users)} account(s); "
          f"{len(active_admins(session))} active admin(s). "
          "Passwords are bcrypt hashes and are never displayed.")
    return 0


def command_create(session, args) -> int:
    if args.role not in ROLES:
        raise DataError(f"--role must be one of {', '.join(ROLES)}")

    existing = session.get(User, args.username)
    if existing is not None:
        raise DataError(
            f"{args.username} already exists. Use reset-password to change its "
            "password, or pick another username.")

    assigned = parse_states(args.states, args.role)
    password = read_new_password(args.username, args.password_env)

    session.add(User(
        username=args.username,
        name=args.name or args.username.replace("_", " ").title(),
        email=args.email or f"{args.username}@example.invalid",
        password_hash=hash_password(password),
        role=args.role,
        assigned_states=assigned,
        active=True,
    ))
    log(session, args.actor, "user_create", args.username,
        f"role={args.role} states={assigned or 'all'}")

    scope = assigned.replace("|", ", ") if assigned else "all states"
    print(f"  created {args.username} ({args.role}, {scope})")
    return 0


def command_reset_password(session, args) -> int:
    user = session.get(User, args.username)
    if user is None:
        raise DataError(f"no such user: {args.username}")

    password = read_new_password(args.username, args.password_env)
    user.password_hash = hash_password(password)
    log(session, args.actor, "user_reset_password", args.username,
        "password replaced")
    print(f"  password reset for {args.username}")
    return 0


def command_deactivate(session, args) -> int:
    user = session.get(User, args.username)
    if user is None:
        raise DataError(f"no such user: {args.username}")
    if not user.active:
        print(f"  {args.username} is already inactive")
        return 0

    if user.role == "admin" and not active_admins(session, excluding=args.username):
        raise DataError(
            f"{args.username} is the only active admin. Create another admin first, "
            "or nobody can administer the site and the only way back in is "
            "python -m src.db.init --reset, which also wipes the forecasts.")

    user.active = False
    log(session, args.actor, "user_deactivate", args.username, "active=False")
    print(f"  deactivated {args.username} (it can no longer sign in)")
    return 0


COMMANDS = {
    "list": command_list,
    "create": command_create,
    "reset-password": command_reset_password,
    "deactivate": command_deactivate,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-url-env", default=DEFAULT_URL_ENV, metavar="NAME",
                        help=f"variable holding the database URL "
                             f"(default {DEFAULT_URL_ENV})")
    parser.add_argument("--actor", default=None,
                        help="who to record in audit_log (default: the OS user)")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="show every account, never a password")

    create = subparsers.add_parser("create", help="add an account")
    create.add_argument("username")
    create.add_argument("--role", default="officer", choices=ROLES)
    create.add_argument("--states", default=None,
                        help="comma-separated, e.g. \"Bihar,Madhya Pradesh\"; "
                             "ignored for an admin")
    create.add_argument("--name", default=None, help="display name")
    create.add_argument("--email", default=None)

    reset = subparsers.add_parser("reset-password", help="replace a password")
    reset.add_argument("username")

    deactivate = subparsers.add_parser("deactivate",
                                       help="block sign-in without deleting history")
    deactivate.add_argument("username")

    # Only on the commands that set a password. Never a --password flag: argv is
    # visible in the process list and ends up in the shell history.
    for sub in (create, reset):
        sub.add_argument("--password-env", default=None, metavar="NAME",
                         help="read the password from this environment variable "
                              "instead of prompting (for non-interactive use)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for attribute in ("password_env", "states", "name", "email"):
        if not hasattr(args, attribute):
            setattr(args, attribute, None)
    if not args.actor:
        import getpass as _getpass

        args.actor = _getpass.getuser()

    print("--- manage_users ---")
    label = connect(args.db_url_env)
    print(f"  database: {label}")
    print(f"  command : {args.command}")

    url = database_url()
    try:
        with session_scope() as session:
            return COMMANDS[args.command](session, args)
    except DataError:
        raise
    except Exception as error:
        raise DataError(
            scrub(f"{type(error).__name__}: {error}", url)) from None
    finally:
        use_url(None)


if __name__ == "__main__":
    raise SystemExit(main())
