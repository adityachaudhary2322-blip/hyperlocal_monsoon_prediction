"""Shared helpers for the control panel: auth, scoping, banners, audit."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from src.db.models import AuditLog, Setting, Unit, User
from src.db.session import create_all, get_session
from src.runtime import status as runtime_status

# Map fills, from the design tokens in app/theme.py (light theme). Text never uses
# these: see theme.risk_label, which pairs an icon and a word with a darker ink.
RISK_COLOURS = {"green": "#3E9A61", "amber": "#E1A42A", "red": "#C4453D",
                "unknown": "#C5CDD2"}
LANGUAGE_LABELS = {"en": "English", "hi": "हिन्दी", "mr": "मराठी"}


def boot() -> None:
    create_all()
    _model_state()


@st.cache_resource(show_spinner="Checking model artifacts...")
def _model_state() -> tuple[bool, str]:
    """(models usable, one-line explanation). Cached once per container.

    Three sources, in the order they are preferred:

    1. `app/assets/models/` - committed boosters and isotonic breakpoints. This is the
       only one that exists on Streamlit Community Cloud, and it is also what
       `HOSTED_MODE=true` uses on the laptop.
    2. `models/lgbm/` - the locally trained artifacts, used when they are present and
       HOSTED_MODE is off.
    3. A private Hugging Face model repo, if HF_MODEL_REPO is configured.

    A failure must not blank the app: every page except "Run forecast" works without
    models, so the reason is recorded and shown rather than raised.
    """
    from src.config import flag

    from src import hosted_models

    if hosted_models.available():
        return True, f"committed assets - {hosted_models.describe()}"

    if not flag("HOSTED_MODE"):
        try:
            from src.artifacts import ensure_artifacts

            return True, ensure_artifacts(quiet=True).detail
        except Exception as error:
            return False, f"unavailable: {type(error).__name__}: {error}"

    return False, hosted_models.describe()


def artifact_state() -> tuple[bool, str]:
    """(models usable, one-line explanation) for the Models page and Run button."""
    return _model_state()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def current_user() -> User | None:
    return st.session_state.get("user") or None


def login_form() -> User | None:
    """Username/password against the bcrypt hashes in `users`. Returns the user on success.

    streamlit-authenticator wants a credentials dict up front; building it from the
    database keeps one source of truth and lets db.init rotate passwords.
    """
    import bcrypt

    with st.form("login", border=True, width=420):
        username = st.text_input("Username", autocomplete="username", key="login_user")
        password = st.text_input("Password", type="password",
                                 autocomplete="current-password", key="login_pass")
        submitted = st.form_submit_button("Sign in", type="primary")

    if not submitted:
        return None
    session = get_session()
    try:
        user = session.get(User, username.strip())
        ok = bool(
            user and user.active
            and bcrypt.checkpw(password.encode("utf-8"),
                               user.password_hash.encode("utf-8"))
        )
        if not ok:
            st.error("Wrong username or password.")
            return None
        session.expunge(user)
        st.session_state["user"] = user
        for key in ("login_user", "login_pass"):
            st.session_state.pop(key, None)
        log(username.strip(), "login", "user", username.strip(),
            "[demo]" if user.role == "demo" else None)
        return user
    finally:
        session.close()


def authenticate() -> User | None:
    """The signed-in user, or a login form (and None) when there is none."""
    user = current_user()
    if user:
        return user
    st.title("Officer sign in")
    st.caption("Accounts are created by an administrator.")
    if login_form():
        st.rerun()
    return None


def require_login() -> User:
    user = authenticate()
    if user is None:
        st.stop()
    return user


def visible_states(user: User) -> list[str] | None:
    """None means every state (admin). Officers are scoped to their own."""
    if user.role == "admin":
        return None
    return user.states()


def scope_query(query, model, user: User):
    states = visible_states(user)
    if states is None:
        return query
    return query.filter(model.state.in_(states))


# --------------------------------------------------------------------------
# Banners
# --------------------------------------------------------------------------
def mode_banner(user=None) -> dict:
    """One calm line saying what is real. The demo account always hears that it works on
    demo copies; everyone else hears it whenever anything is mocked."""
    from src import demo

    state = runtime_status()
    if demo.is_demo(user):
        st.info("**Demo mode.** You are reviewing demo copies of advisories for the pilot "
                "districts. Anything you send goes to a demo outbox, never to real phones, "
                "and the demo data resets every night.", icon=":material/science:")
        return state
    if state["llm_mock"] or state["sender_mock"]:
        bits = []
        if state["llm_mock"]:
            bits.append(f"**template text only** ({state['llm_reason']})")
        if state["sender_mock"]:
            bits.append(f"**messages are mocked** ({state['sender_reason']})")
        st.warning(
            "Offline mode: " + "; ".join(bits) +
            ". Nothing leaves this machine. Add keys to `.env` and set "
            "`MOCK_MODE=false` to go live - no code changes needed.",
            icon=":material/wifi_off:",
        )
    return state


def risk_chip(level: str) -> str:
    """Icon + word in a text colour that passes AA - never a colour-only pill."""
    from app.theme import risk_label

    return risk_label(level)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
def log(username: str | None, action: str, entity: str | None = None,
        entity_id: str | int | None = None, detail: str | None = None) -> None:
    session = get_session()
    try:
        session.add(AuditLog(username=username, action=action, entity=entity,
                             entity_id=str(entity_id) if entity_id is not None else None,
                             detail=detail))
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------
# Runtime settings (Models page)
# --------------------------------------------------------------------------
def get_setting(key: str, default=None):
    import json

    session = get_session()
    try:
        row = session.get(Setting, key)
        return json.loads(row.value) if row else default
    finally:
        session.close()


def set_setting(key: str, value, username: str) -> None:
    import datetime as _dt
    import json

    session = get_session()
    try:
        row = session.get(Setting, key)
        before = row.value if row else None
        if row is None:
            row = Setting(key=key, value=json.dumps(value))
            session.add(row)
        else:
            row.value = json.dumps(value)
        row.updated_by = username
        row.updated_utc = _dt.datetime.now(_dt.timezone.utc)
        session.add(AuditLog(username=username, action="change_setting",
                             entity="setting", entity_id=key,
                             detail=f"{before} -> {json.dumps(value)}"))
        session.commit()
    finally:
        session.close()


def effective_llm_config() -> dict:
    """config/llm.yaml with the admin's Models-page overrides applied."""
    import copy

    from src.common import load_config

    cfg = copy.deepcopy(load_config("llm"))
    order = get_setting("fallback_order")
    if order:
        cfg["fallback_order"] = order
    offline = get_setting("offline_mode")
    if offline is not None:
        cfg["offline_mode"] = bool(offline)
    return cfg


def app_chain():
    from src.llm.providers import build_chain

    cfg = effective_llm_config()
    return build_chain(cfg), cfg


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------
def units_frame(user: User) -> pd.DataFrame:
    session = get_session()
    try:
        query = scope_query(session.query(Unit), Unit, user)
        return pd.read_sql(query.statement, session.connection())
    finally:
        session.close()


def fmt_dt(value: dt.datetime | None) -> str:
    return "-" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def audit(user, action: str, entity: str | None = None, entity_id=None,
          detail: str | None = None) -> None:
    """log() for a signed-in user's action; the demo account's entries are tagged."""
    from src import demo

    log(user.username, action, entity, entity_id,
        demo.tag(detail) if demo.is_demo(user) else detail)
