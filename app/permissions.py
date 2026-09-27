"""Role checks for the officer portal, from config/roles.yaml. Pages call can(user, perm)."""

from __future__ import annotations

from functools import lru_cache

from src.common import load_config


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return load_config("roles")


def can(user, permission: str) -> bool:
    if user is None:
        return False
    allowed = _cfg()["permissions"].get(permission)
    if allowed is None:
        raise KeyError(f"unknown permission {permission!r} (config/roles.yaml)")
    return user.role in allowed


def badge(user) -> str:
    return _cfg()["badges"].get(user.role, user.role.title())


def require(user, permission: str) -> None:
    """Stop the page (with a calm message) when the user lacks a permission."""
    import streamlit as st

    if not can(user, permission):
        st.info("This page is for administrators.", icon=":material/lock:")
        st.stop()
