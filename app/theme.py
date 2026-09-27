"""Design tokens, the light/dark switch, and the slim top bar shared by every page.

**Why CSS variables and not Streamlit's theme switch.** Streamlit 1.64 reads
`[theme.light]` / `[theme.dark]` from `.streamlit/config.toml` (both are set, from the
tokens below) but offers no way to change the active theme from Python. The brief asks
for a "Dark mode" toggle in the top bar, remembered in session state, that switches every
page and the map together - so the toggle drives one `<style>` block of `--mo-*`
variables, and the page chrome, native widgets and the MapLibre component all read those.

Known limit, stated rather than hidden: widgets that paint on a canvas (st.dataframe's
grid) follow Streamlit's *native* theme, which follows the browser's colour scheme. The
toggle starts from that same scheme (`st.context.theme.type`), so the two only disagree
after a viewer flips the toggle away from their OS setting. Public pages avoid
st.dataframe for exactly this reason.

Contrast: every text/background pair below is checked by tests/test_theme.py (WCAG AA,
4.5:1 for text, 3:1 for large text and non-text marks).
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

# --------------------------------------------------------------------------
# Tokens - the single source; config.toml mirrors the base colours (tested).
# --------------------------------------------------------------------------
TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#F6F8F9",
        "surface": "#FFFFFF",
        "surface-2": "#EEF2F4",       # subtle fills: table header, hover
        "ink": "#1C2B36",
        "muted": "#5B6B76",
        "line": "#DCE3E7",
        "line-strong": "#7D909C",     # control borders: 3.3:1 on surface (WCAG 1.4.11)
        "primary": "#2E5E7E",         # monsoon slate blue
        "on-primary": "#FFFFFF",
        "primary-soft": "#E3EDF4",
        "accent": "#4A8B4F",          # paddy green: rules, icons, large text only
        "accent-ink": "#2F7A4B",      # paddy green for body text
        "risk-low": "#3E9A61",
        "risk-med": "#E1A42A",
        "risk-high": "#C4453D",
        "risk-low-ink": "#2F7A4B",
        "risk-med-ink": "#8A5A00",
        "risk-high-ink": "#A8362F",
        "risk-outline": "#5B4200",    # outline for the amber fill (2.1:1 alone)
        "nofc": "#C5CDD2",
        "hatch": "#B8C2C8",
        "focus": "#2E5E7E",
    },
    "dark": {
        "bg": "#0F1B24",
        "surface": "#172733",
        "surface-2": "#1E3240",
        "ink": "#E6EDF1",
        "muted": "#9AABB6",
        "line": "#2A3E4C",
        "line-strong": "#627D8E",     # 3.5:1 on the dark surface
        "primary": "#7FB0D4",
        "on-primary": "#0F1B24",
        "primary-soft": "#1F3A4E",
        "accent": "#8CC98F",
        "accent-ink": "#8CC98F",
        "risk-low": "#5DBB80",
        "risk-med": "#E9B64E",
        "risk-high": "#E0726A",
        "risk-low-ink": "#5DBB80",
        "risk-med-ink": "#E9B64E",
        "risk-high-ink": "#E0726A",
        "risk-outline": "#0F1B24",
        "nofc": "#3B4F5C",
        "hatch": "#4A6272",
        "focus": "#7FB0D4",
    },
}

# Sequential rain ramp (ends on the primary: wet = slate blue) and diverging temperature.
RAIN_RAMP = {
    "light": ["#E8F1F6", "#A8CCE0", "#5C9CC4", "#2E5E7E", "#173A52"],
    "dark": ["#1E3240", "#2D5470", "#4F83AA", "#7FB0D4", "#C4DDEE"],
}
TEMP_RAMP = {
    "light": ["#3A6E96", "#9CC3DA", "#EEF0E6", "#E9A25B", "#B5462F"],
    "dark": ["#4F83AA", "#8FB9D2", "#3B4F5C", "#E9A25B", "#E0726A"],
}

BRAND_DIR = Path(__file__).resolve().parent / "static" / "brand"

FONT_CSS = ("https://fonts.googleapis.com/css2?family=Mukta:wght@400;500;600;700"
            "&display=swap")

# Risk thresholds are the forecast engine's (src.pipeline.run); only the words and
# shapes live here. Shapes differ so the three levels read without colour.
RISK_LABELS = {
    "green": ("●", "Low", "risk-low-ink"),
    "amber": ("▲", "Medium", "risk-med-ink"),
    "red": ("◆", "High", "risk-high-ink"),
}
STATUS_LABELS = {
    "pending_approval": ("⏳", "Pending", "risk-med-ink"),
    "approved": ("✓", "Approved", "accent-ink"),
    "rejected": ("✕", "Rejected", "risk-high-ink"),
    "sent": ("➤", "Sent", "primary"),
}

COVERAGE_STATES = ("Uttar Pradesh", "Delhi", "Bihar", "Madhya Pradesh", "Maharashtra")
COVERAGE_TEXT = ("Forecasts currently cover Uttar Pradesh, Delhi, Bihar, Madhya Pradesh "
                 "and Maharashtra. We're working on adding more states.")
DISCLAIMER = ("Experimental prototype. Always follow official IMD and state agriculture "
              "department advisories.")


# --------------------------------------------------------------------------
# Mode
# --------------------------------------------------------------------------
def _browser_prefers_dark() -> bool:
    try:
        return (st.context.theme.type or "light") == "dark"
    except Exception:
        return False


def is_dark() -> bool:
    if "dark" not in st.session_state:
        st.session_state["dark"] = _browser_prefers_dark()
    return bool(st.session_state["dark"])


def mode() -> str:
    return "dark" if is_dark() else "light"


def tokens() -> dict[str, str]:
    return TOKENS[mode()]


# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------
def _vars(values: dict[str, str]) -> str:
    return "".join(f"--mo-{k}:{v};" for k, v in values.items())


_BASE_CSS = """
@import url('@@FONT@@');
:root { @@VARS@@ color-scheme: @@SCHEME@@; }

/* ---- page ---- */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background: var(--mo-bg) !important; color: var(--mo-ink);
}
.stApp, .stApp button, .stApp input, .stApp textarea, .stApp select, .stApp label,
.stMarkdown, [data-testid="stMarkdownContainer"], [data-baseweb="popover"] * {
  font-family: "Mukta", system-ui, sans-serif !important;
}
/* 8 px spacing scale; content column at most 1200 px; room for the fixed header. */
[data-testid="stMain"] .block-container { padding-top: 4rem; padding-bottom: 2rem;
  max-width: 1200px; }
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp p, .stApp li, .stApp label,
.stApp span, [data-testid="stMarkdownContainer"] { color: var(--mo-ink); }
.stApp h1 { font-size: 1.75rem; line-height: 2.125rem; font-weight: 700;
  letter-spacing: 0; padding: .25rem 0 .5rem; }
.stApp h2 { font-size: 1.25rem; line-height: 1.625rem; font-weight: 600; padding-top: .5rem; }
.stApp h3 { font-size: 1.0625rem; line-height: 1.5rem; font-weight: 600; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *,
.mo-muted { color: var(--mo-muted) !important; }
.stApp a { color: var(--mo-primary); text-underline-offset: 2px; }
hr { border-color: var(--mo-line) !important; }
.mo-prose { max-width: 72ch; }
.mo-tier { display: inline-block; font-size: 12.5px; font-weight: 600; line-height: 1.2; padding: 2px 8px;
  border-radius: 999px; border: 1px solid var(--mo-line-strong); color: var(--mo-ink); white-space: nowrap; }
.mo-tier-validated { border-color: var(--mo-accent); color: var(--mo-accent-ink) !important; }
.mo-tier-experimental { border-style: dashed; color: var(--mo-muted) !important; }
.mo-tier-low { color: var(--mo-risk-med-ink) !important; border-color: var(--mo-risk-med-ink); }
.mo-card-line { margin: 0 0 8px; line-height: 1.5; }
.mo-crop-row { border-left: 4px solid var(--mo-line-strong); padding-left: 10px; margin-bottom: 10px; }
.mo-crop-row p { margin: 2px 0; }
.mo-unit-title { margin: .5rem 0 0; }
.mo-lede { font-size: 1.0625rem; line-height: 1.6; color: var(--mo-ink); }
.mo-sr { position: absolute !important; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap; }
.mo-summary { margin: 0; padding-left: 1.1rem; line-height: 1.6; }
.mo-summary li + li { margin-top: 4px; }
.mo-table-wrap { overflow-x: auto; border: 1px solid var(--mo-line); border-radius: 8px;
  max-width: 980px; }
.mo-table caption { text-align: left; }
.mo-table { min-width: 600px; }     /* scrolls inside .mo-table-wrap on phones */
.mo-table td:first-child, .mo-table th { white-space: nowrap; }
.mo-steps h3 { margin: 1rem 0 .25rem; }
.mo-steps p, .mo-team p { margin: 0 0 .5rem; line-height: 1.6; }
.mo-team ul { margin: .25rem 0 .75rem; padding-left: 1.1rem; }
:lang(hi), :lang(mr), .mo-deva { line-height: 1.6; }
.mo-num { font-variant-numeric: tabular-nums; }

/* ---- native widgets bound to the tokens ---- */
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"],
[data-baseweb="textarea"], .stApp input, .stApp textarea {
  background: var(--mo-surface) !important; color: var(--mo-ink) !important;
  border-color: var(--mo-line-strong) !important;
}
[data-baseweb="select"] svg { fill: var(--mo-muted); }
[data-baseweb="popover"] > div, [data-baseweb="popover"] ul, [data-baseweb="menu"],
[data-testid="stPopoverBody"] {
  background: var(--mo-surface) !important; color: var(--mo-ink) !important;
  border-color: var(--mo-line) !important;
}
[data-baseweb="popover"] li[role="option"] { color: var(--mo-ink) !important; }
[data-baseweb="popover"] li[role="option"]:hover,
[data-baseweb="popover"] li[aria-selected="true"] { background: var(--mo-surface-2) !important; }
.stApp button[kind], .stApp [data-testid^="stBaseButton"] {
  background: var(--mo-surface); color: var(--mo-ink); border: 1px solid var(--mo-line-strong);
  font-weight: 500;
}
.stApp button[kind]:hover, .stApp [data-testid^="stBaseButton"]:hover {
  border-color: var(--mo-primary); color: var(--mo-primary);
}
.stApp [data-testid="stBaseButton-tertiary"], .stApp button[kind="tertiary"] {
  background: transparent !important; border-color: transparent !important; padding: 2px 8px;
  white-space: nowrap; color: var(--mo-ink);
}
.stApp [data-testid^="stBaseButton-primary"], .stApp button[kind^="primary"],
.stApp [data-testid="stBaseButton-segmented_controlActive"],
.stApp [data-testid="stBaseButton-pillsActive"] {
  background: var(--mo-primary) !important; color: var(--mo-on-primary) !important;
  border-color: var(--mo-primary) !important;
}
.stApp [data-testid="stBaseButton-segmented_controlActive"] *,
.stApp [data-testid="stBaseButton-pillsActive"] *,
.stApp [data-testid^="stBaseButton-primary"] * { color: var(--mo-on-primary) !important; }
[data-testid="stExpander"] details, [data-testid="stExpander"] summary {
  background: var(--mo-surface); border-color: var(--mo-line) !important; color: var(--mo-ink);
}
.stApp [data-testid="stVerticalBlockBorderWrapper"],
.stApp div[data-testid="stVerticalBlock"][class*="border"] { border-color: var(--mo-line) !important; }
.stApp button[role="tab"] { color: var(--mo-muted); }
.stApp button[role="tab"][aria-selected="true"] { color: var(--mo-ink); }
[data-baseweb="tab-highlight"] { background: var(--mo-primary) !important; }
[data-baseweb="tab-border"] { background: var(--mo-line) !important; }
[data-testid="stAlertContainer"] { background: var(--mo-surface-2) !important;
  color: var(--mo-ink) !important; border-left: 4px solid var(--mo-primary); }
[data-testid="stAlertContainer"] * { color: var(--mo-ink) !important; }
[data-testid="stCheckbox"] span, [data-testid="stToggle"] span { color: var(--mo-ink); }
[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
[data-testid="stTable"] table, .mo-table { border-collapse: collapse; width: 100%;
  color: var(--mo-ink); background: var(--mo-surface); font-variant-numeric: tabular-nums; }
[data-testid="stTable"] th, .mo-table th { background: var(--mo-surface-2); color: var(--mo-ink);
  text-align: left; font-weight: 600; }
[data-testid="stTable"] th, [data-testid="stTable"] td, .mo-table th, .mo-table td {
  border-bottom: 1px solid var(--mo-line) !important; padding: 6px 10px; }
.stApp code { background: var(--mo-surface-2); color: var(--mo-ink); }

/* ---- keyboard focus: always visible, both themes ---- */
.stApp *:focus-visible, [data-baseweb="popover"] *:focus-visible {
  outline: 3px solid var(--mo-focus) !important; outline-offset: 2px !important;
  box-shadow: none !important;
}
.stApp [data-baseweb="select"] > div:focus-within,
.stApp [data-baseweb="input"]:focus-within { outline: 3px solid var(--mo-focus); outline-offset: 1px; }

/* ---- header: logo + page menu (st.navigation, position="top") ---- */
[data-testid="stHeader"] { background: var(--mo-surface) !important;
  border-bottom: 1px solid var(--mo-line); }
[data-testid="stHeaderLogo"] { height: 32px; max-width: 170px; }
[data-testid="stTopNavLink"] { border-radius: 6px; padding: 4px 10px !important; }
[data-testid="stTopNavLink"] *, [data-testid="stTopNavLink"] span { color: var(--mo-ink) !important; }
[data-testid="stTopNavLink"]:hover { background: var(--mo-surface-2) !important; }
[data-testid="stTopNavLink"][aria-current="page"],
[data-testid="stTopNavLink"][data-active="true"] { background: var(--mo-primary-soft) !important; }
[data-testid="stTopNavLink"][aria-current="page"] *,
[data-testid="stTopNavLink"][data-active="true"] * { color: var(--mo-primary) !important; font-weight: 600; }
[data-testid="stTopNavSection"] *, [data-testid="stNavSectionHeader"] * { color: var(--mo-ink) !important; }

/* ---- slim bar under the header: who, sign out, language, dark mode ---- */
.st-key-mo_topbar { border-bottom: 1px solid var(--mo-line); padding: 0 0 8px; margin-bottom: 8px;
  row-gap: 8px !important; flex-wrap: wrap !important; }
.st-key-mo_coverage button { border-radius: 999px !important; border-color: var(--mo-accent) !important;
  background: var(--mo-surface) !important; padding: 0 12px !important; min-height: 32px; }
.st-key-mo_coverage button p { color: var(--mo-accent-ink) !important; font-weight: 600; }
.st-key-mo_lang [data-testid="stButtonGroup"] button { min-height: 32px; padding: 0 10px; }
.mo-who { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
.mo-who-name { font-weight: 600; color: var(--mo-ink); }
.mo-role { font-size: .8125rem; font-weight: 600; line-height: 1.2; padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--mo-line-strong); color: var(--mo-ink); }
.mo-role-admin { border-color: var(--mo-primary); color: var(--mo-primary) !important; }
.mo-role-demo { background: var(--mo-primary-soft); border-color: var(--mo-primary); color: var(--mo-primary) !important; }
.stApp .mo-page-desc { color: var(--mo-muted) !important; margin: -8px 0 16px; max-width: 72ch; font-size: 1rem; line-height: 1.5; }
@media (max-width: 720px) {
  .st-key-mo_topbar > div:has(> .st-key-mo_coverage), .st-key-mo_coverage { display: none !important; }  /* the strip says it */
  .st-key-mo_lang [data-testid="stButtonGroup"] button { padding: 0 7px; min-width: 0; }
  .mo-who-name { max-width: 9rem; overflow: hidden; text-overflow: ellipsis; }
  [data-testid="stHeaderLogo"] { max-width: 120px; }
}

/* ---- sign-in demo note: calm, bordered, no shadow ---- */
.mo-demo-note { background: var(--mo-primary-soft); border: 1px solid var(--mo-line);
  border-left: 4px solid var(--mo-primary); border-radius: 8px; padding: 12px 16px; }
.mo-demo-note p { margin: 0 0 8px; line-height: 1.5; }
.mo-demo-note p:last-child { margin-bottom: 0; }
.mo-demo-title { font-weight: 600; }
.mo-demo-note code { font-size: .9375rem; padding: 1px 6px; border-radius: 4px;
  background: var(--mo-surface) !important; border: 1px solid var(--mo-line); }

/* ---- officer cards: border, no shadow; urgent = red left rule ---- */
.stApp [class*="st-key-card-"] { background: var(--mo-surface); border-radius: 8px;
  padding: 16px !important; gap: 8px !important; }
.stApp [class*="st-key-card-urgent-"] { border-left: 4px solid var(--mo-risk-high) !important; }
.mo-card-head { display: flex; flex-direction: column; gap: 4px; }
.mo-card-title { margin: 0 !important; padding: 0 !important; font-size: 1.125rem !important; }
.mo-card-meta, .mo-card-why { margin: 0; line-height: 1.5; }
.mo-card-meta { color: var(--mo-ink); }
.stApp .mo-card-why { color: var(--mo-muted); }
.mo-card-why b { color: var(--mo-ink); font-weight: 600; }
.mo-urgent-flag, .mo-urgent-text { color: var(--mo-risk-high-ink) !important; font-weight: 600; }
.mo-urgent-flag { font-size: .875rem; }
.mo-queue-count { margin: 0 0 8px; }
.stApp [class*="st-key-actions-"] [data-testid="stBaseButton-tertiary"] *,
.stApp [class*="st-key-actions-"] [data-testid="stBaseButton-tertiary"] {
  color: var(--mo-risk-high-ink) !important; }
.stApp [class*="st-key-reject-row-"] [data-testid="stBaseButton-secondary"] {
  border-color: var(--mo-risk-high-ink) !important; color: var(--mo-risk-high-ink) !important; }
.stApp [class*="st-key-reject-row-"] [data-testid="stBaseButton-secondary"] * { color: var(--mo-risk-high-ink) !important; }
.mo-empty { border: 1px dashed var(--mo-line-strong); border-radius: 8px; padding: 24px;
  background: var(--mo-surface); max-width: 640px; }
.mo-empty p { margin: 0; color: var(--mo-muted); }
.mo-empty .mo-empty-title { color: var(--mo-ink); font-weight: 600; font-size: 1.0625rem; margin-bottom: 4px; }
.mo-fresh { margin: 8px 0 16px; }
.mo-fresh.mo-ok, .mo-fresh.mo-ok * { color: var(--mo-accent-ink) !important; }
.mo-fresh.mo-stale, .mo-fresh.mo-stale * { color: var(--mo-risk-med-ink) !important; }
[data-testid="stMetric"] { background: var(--mo-surface); border: 1px solid var(--mo-line);
  border-radius: 8px; padding: 12px 16px; }

/* Key numbers: one row on desktop, 2 x 2 on phones. */
@media (max-width: 640px) {
  .st-key-mo_metrics [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 8px !important; }
  .st-key-mo_metrics [data-testid="stColumn"] { flex: 1 1 calc(50% - 8px) !important; min-width: calc(50% - 8px) !important; }
  .st-key-mo_metrics [data-testid="stMetric"] { padding: 8px 12px; }
}

/* ---- seasonal scene band: the real title is kept for screen readers ---- */
.st-key-mo_hero { position: relative; gap: 0 !important; margin-bottom: 8px; }
.st-key-mo_hero [data-testid="stHeading"], .st-key-mo_hero h1 { position: absolute !important;
  width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; margin: 0; padding: 0; }
.stApp .mo-desc-under { position: absolute !important; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap; }
.st-key-mo_scene_menu button { min-height: 32px; padding: 0 10px !important; }
@media (max-width: 640px) {
  .stApp .mo-desc-under { position: static !important; width: auto; height: auto; clip: auto;
    white-space: normal; margin: 8px 0 16px; }
}

/* ---- coverage strip and footer ---- */
.mo-strip { background: var(--mo-primary-soft); color: var(--mo-ink); border-left: 4px solid var(--mo-accent);
  padding: 8px 12px; margin: 4px 0 10px; font-size: .95rem; line-height: 1.4; }
.mo-footer { border-top: 1px solid var(--mo-line); margin-top: 2rem; padding-top: 12px;
  color: var(--mo-muted); font-size: .875rem; }
.mo-footer strong { color: var(--mo-ink); }
.mo-footer p { margin: 0 0 8px; }
.mo-footer-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 24px; margin-top: 10px;
  padding-top: 10px; border-top: 1px solid var(--mo-line); }
.mo-built { display: inline-flex; align-items: center; gap: 6px; color: var(--mo-ink); white-space: nowrap; }
.mo-credit { display: inline-flex; align-items: center; gap: 10px; max-width: 80ch; font-size: .8125rem; }
.mo-partner { height: 28px; width: auto; flex: none; }

/* ---- status + risk labels: icon + coloured text, never colour alone ---- */
.mo-label { font-weight: 600; white-space: nowrap; }
.mo-label .mo-ico { display: inline-block; width: 1.1em; text-align: center; }
.stApp .mo-label span { color: inherit !important; }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important;
    transition-duration: .01ms !important; scroll-behavior: auto !important; }
}
"""

# Every page: Streamlit's header stays - it carries the logo and the page menu
# (st.navigation, position="top") - but its main menu, Deploy button, status widget,
# decoration line and the sidebar are hidden, as is Streamlit's own footer.
_CHROME_CSS = """
[data-testid="stMainMenu"], #MainMenu, [data-testid="stDecoration"],
[data-testid="stStatusWidget"], [data-testid="stAppDeployButton"], .stDeployButton,
[data-testid="stToolbarActions"], footer { display: none !important; }
/* Phones: st.navigation(position="top") folds its menu into the sidebar - keep it. */
[data-testid="stSidebar"], [data-testid="stSidebarContent"] { background: var(--mo-surface) !important;
  border-right: 1px solid var(--mo-line); }
[data-testid="stSidebarNavLink"] * , [data-testid="stSidebarNavSeparator"],
[data-testid="stNavSectionHeader"] * { color: var(--mo-ink) !important; }
[data-testid="stSidebarNavLink"][aria-current="page"] { background: var(--mo-primary-soft) !important; }
[data-testid="stSidebarNavLink"][aria-current="page"] * { color: var(--mo-primary) !important; font-weight: 600; }
[data-testid="stExpandSidebarButton"] *, [data-testid="stSidebarCollapseButton"] * { color: var(--mo-ink) !important; }
/* The phone menu button says what it is: "☰ Menu" rather than a bare ">>". */
[data-testid="stExpandSidebarButton"] { width: auto !important; padding: 0 12px !important; gap: 6px;
  display: inline-flex !important; align-items: center; }
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] { display: none !important; }
[data-testid="stExpandSidebarButton"]::before { content: "☰  Menu"; white-space: pre; font-weight: 600;
  color: var(--mo-ink); font-family: "Mukta", system-ui, sans-serif; }
"""
_PUBLIC_CSS = _CHROME_CSS
_OFFICER_CSS = _CHROME_CSS + """
.stApp [data-testid="stMain"] { font-size: .9375rem; }
"""


BRAND_COLOURS = {
    "light": {"brand-blue": "#2E5E7E", "brand-green": "#4A8B4F"},
    # One-colour mark on dark grounds (vrrtanta-mark-dark.svg).
    "dark": {"brand-blue": "#E6EDF1", "brand-green": "#E6EDF1"},
}


def css(dark: bool, public: bool) -> str:
    name = "dark" if dark else "light"
    base = (_BASE_CSS.replace("@@FONT@@", FONT_CSS)
            .replace("@@VARS@@", _vars({**TOKENS[name], **BRAND_COLOURS[name]}))
            .replace("@@SCHEME@@", name))
    return base + (_PUBLIC_CSS if public else _OFFICER_CSS)


def apply_theme(public: bool) -> None:
    """Inject the stylesheet for the current mode. Call once per page run."""
    st.html(f"<style>{css(is_dark(), public)}</style>")


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------
def _label(icon: str, text: str, token: str, extra: str = "") -> str:
    return (f"<span class='mo-label' style='color:var(--mo-{token})'>"
            f"<span class='mo-ico' aria-hidden='true'>{icon}</span> {text}{extra}</span>")


def risk_label(level: str | None, probability: float | None = None) -> str:
    icon, text, token = RISK_LABELS.get(level or "", ("○", "No forecast", "muted"))
    pct = "" if probability is None else f" · {probability:.0%}"
    return _label(icon, text, token, pct)


def status_label(status: str | None) -> str:
    icon, text, token = STATUS_LABELS.get(status or "", ("•", (status or "unknown")
                                                        .replace("_", " ").capitalize(),
                                                        "muted"))
    return _label(icon, text, token)


STATUS_TEXT = {k: f"{v[0]} {v[1]}" for k, v in STATUS_LABELS.items()}


# --------------------------------------------------------------------------
# Brand (VRRTANTA, option A "cloud canopy"; files in app/static/brand/)
# --------------------------------------------------------------------------
STATIC = Path(__file__).resolve().parent / "static"
PARTNER_LOGO = STATIC / "partners" / "moes-logo.png"
HACKATHON = "Smart India Hackathon 2026"
CREDIT = ("Problem statement by the Ministry of Earth Sciences, Government of India · "
          f"{HACKATHON} · Independent prototype, not an official government service")


def brand_mark(size: int = 28, label: str | None = None) -> str:
    """The tree mark as an <img>: two colours in light mode, the one-colour file in dark.

    An <img> rather than inline SVG because st.html sanitises SVG elements away; the
    files are the committed app/static/brand/ SVGs, served by static file serving.
    """
    name = "vrrtanta-mark-dark.svg" if is_dark() else "vrrtanta-mark.svg"
    alt = label or ""
    return (f"<img class='mo-mark' src='app/static/brand/{name}' width='{size}' "
            f"height='{size}' alt='{alt}'>")


# --------------------------------------------------------------------------
# Top bar
# --------------------------------------------------------------------------
def logo() -> None:
    """The VRRTANTA lockup in Streamlit's header, left of the page menu; it links Home.

    st.logo only accepts an absolute link, so Home is rebuilt from the request URL."""
    name = "vrrtanta-logo-dark.svg" if is_dark() else "vrrtanta-logo.svg"
    link = None
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(str(st.context.url or ""))
        if parts.scheme and parts.netloc:
            link = f"{parts.scheme}://{parts.netloc}/"
    except Exception:
        link = None
    # No icon_image: it replaces the full lockup whenever the sidebar is closed, and this
    # app has no sidebar.
    st.logo(str(BRAND_DIR / name), size="large", link=link)


def sign_out(pages: dict) -> None:
    """Forget the signed-in user and everything they had open, then go Home.

    Sign-in lives only in session state (there is no auth cookie), so clearing it is the
    whole sign-out. The viewer's language and light/dark choice are kept."""
    from app.common import current_user, log

    user = current_user()
    if user is not None:
        log(user.username, "logout", "user", user.username,
            "[demo]" if getattr(user, "role", "") == "demo" else None)
    keep = {k: st.session_state[k] for k in ("dark", "lang") if k in st.session_state}
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.session_state.update(keep)
    st.switch_page(pages["home"])


def top_bar(pages: dict, user=None) -> None:
    """The slim bar under Streamlit's header, on every page: coverage, who is signed in
    (with a role badge, and "Demo mode" for the evaluator account), Sign out or Sign in,
    language, Dark mode. Page links live in the header (st.navigation, position="top")."""
    from app.i18n import LANGS, t

    is_dark()                                   # seed the toggle from the browser
    with st.container(key="mo_topbar", horizontal=True, vertical_alignment="center",
                      gap="small"):
        with st.popover(t("coverage_badge"), key="mo_coverage"):
            st.markdown(t("coverage"))
        st.space("stretch")
        if user is None:
            st.page_link(pages["signin"], label=t("sign_in"), icon=":material/login:")
        else:
            from app.permissions import badge

            role = getattr(user, "role", "officer")
            st.html(f"<span class='mo-who'><span class='mo-who-name'>{_esc(user.name)}</span>"
                    f"<span class='mo-role mo-role-{_esc(role)}'>{_esc(badge(user))}</span>"
                    "</span>", width="content")
            if st.button(t("sign_out"), key="mo_signout", icon=":material/logout:",
                         type="tertiary"):
                sign_out(pages)
        if "lang" not in st.session_state:
            st.session_state["lang"] = "en"
        with st.container(key="mo_lang", width="content"):
            short = {"en": "EN", "hi": "हिं", "mr": "मरा"}
            st.segmented_control(t("lang_label"), list(LANGS), format_func=short.get,
                                 key="lang", label_visibility="collapsed", required=True,
                                 help=" / ".join(LANGS.values()))
        st.toggle(f":material/dark_mode: {t('dark_mode')}", key="dark")
        _scene_controls()


def _scene_controls() -> None:
    """Scene menu (Monsoon default, Auto by season, each season, Classic) and Effects."""
    from app import scenes
    from app.i18n import t

    scenes.choice(); scenes.effects_on()            # seed defaults before the widgets
    with st.container(key="mo_scene_ctl", horizontal=True, gap="small", width="content",
                      vertical_alignment="center"):
        with st.popover(t("scene_menu"), icon=":material/landscape:", key="mo_scene_menu"):
            st.radio(t("scene_menu"), scenes.cfg()["choices"], key="scene",
                     format_func=lambda c: t(f"scene_{c}"), label_visibility="collapsed")
        st.toggle(t("effects"), key="effects", help=t("effects_help"))


def _esc(text) -> str:
    import html

    return html.escape(str(text or ""))


def page_header(title: str, description: str, *, forecast_mode: str | None = None) -> None:
    """Page title plus the one line that says what the page is for, over the seasonal
    scene band (app/scenes.py) - or plain, when the viewer picked "Classic".

    The real st.title stays in the page (screen readers, tests); the band draws the same
    words aria-hidden over the scene. Public pages animate (unless Effects is off or the
    viewer prefers reduced motion); officer pages get a faint, still frame.
    `forecast_mode` (none / light / heavy / clear) ties the scene to a selected area."""
    from app import scenes
    from app.i18n import t

    scene = scenes.resolve(scenes.choice())
    if scene == "classic":
        st.title(title)
        st.html(f"<p class='mo-page-desc'>{_esc(description)}</p>")
        return

    from app.components.scene_header import scene_header

    cfg = scenes.cfg()
    public = bool(st.session_state.get("_mo_public", True))
    dark = is_dark()
    with st.container(key="mo_hero"):
        st.title(title)
        scene_header({
            "scene": scene, "title": title, "desc": description, "dark": dark,
            "animate": public and scenes.effects_on(), "faint": not public,
            "mode": forecast_mode if public else None,
            "caption": t("scene_caption") if public and forecast_mode else "",
            "ink": TOKENS[mode()]["ink"], "ink_soft": "#C3D0D9" if dark else "#3E4E59",
            "height": cfg["band_height"]["desktop"], "height_phone": cfg["band_height"]["phone"],
            "cfg": {k: cfg[k] for k in ("particles", "rain_intensity", "fps", "idle_stop_seconds")},
        })
    st.html(f"<p class='mo-page-desc mo-desc-under'>{_esc(description)}</p>")


def footer(extra: str = "") -> None:
    from app.i18n import t

    disclaimer = t("disclaimer")
    """Disclaimer, data credits, the VRRTANTA credit and the hackathon credit line.

    The partner slot shows app/static/partners/moes-logo.png only if someone has put
    that file there; nothing is drawn or downloaded in its place.
    """
    partner = ""
    if PARTNER_LOGO.is_file():
        partner = ("<img class='mo-partner' src='app/static/partners/moes-logo.png' "
                   "alt='Ministry of Earth Sciences' height='28'>")
    st.html(
        # A div, not <footer>: the public-page CSS hides Streamlit's own <footer>.
        "<div class='mo-footer' role='contentinfo'>"
        f"<p class='mo-prose'><strong>{disclaimer}</strong></p>"
        + (f"<p class='mo-prose'>{extra}</p>" if extra else "")
        + "<div class='mo-footer-row'>"
        f"<span class='mo-built'>{brand_mark(20)}Built by <strong>VRRTANTA</strong></span>"
        f"<span class='mo-credit'>{partner}<span>{CREDIT}</span></span>"
        "</div></div>")
