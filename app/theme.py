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
[data-testid="stMain"] .block-container { padding-top: 0.5rem; padding-bottom: 2rem;
  max-width: 1480px; }
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

/* ---- top bar ---- */
.st-key-mo_topbar { border-bottom: 1px solid var(--mo-line); padding: 4px 0 6px;
  margin-bottom: 4px; }
.st-key-mo_topbar [data-testid="stPageLink"] a { padding: 2px 8px; border-radius: 4px; }
.st-key-mo_topbar [data-testid="stPageLink"] a p { font-weight: 500; }
.st-key-mo_topbar [data-testid="stPageLink"] a[aria-current="page"] p,
.st-key-mo_topbar [data-testid="stPageLink"] a[data-active="true"] p {
  color: var(--mo-primary) !important; text-decoration: underline; text-underline-offset: 6px;
  text-decoration-thickness: 2px; }
.mo-brand { display: inline-flex; align-items: center; gap: 8px; text-decoration: none !important;
  white-space: nowrap; }
.mo-brand .mo-mark { flex: none; }
.mo-brand .mo-word { font-weight: 700; font-size: 1.1875rem; letter-spacing: .08em; color: var(--mo-ink);
  padding-top: 2px; }
.mo-brand .mo-app { font-weight: 500; font-size: .9375rem; color: var(--mo-muted); padding-left: 10px;
  border-left: 1px solid var(--mo-line); }
.st-key-mo_coverage button { border-radius: 999px !important; border-color: var(--mo-accent) !important;
  background: var(--mo-surface) !important; padding: 0 12px !important; min-height: 32px; }
.st-key-mo_coverage button p { color: var(--mo-accent-ink) !important; font-weight: 600; }
.st-key-mo_subnav { border-bottom: 1px solid var(--mo-line); padding-bottom: 4px; margin-bottom: 8px; }
.st-key-mo_menu { display: none; }
.st-key-mo_mtabs { display: none !important; }
.st-key-mo_lang [data-testid="stButtonGroup"] button { min-height: 32px; padding: 0 10px; }
@media (max-width: 720px) {
  .st-key-mo_links { display: none !important; }
  .st-key-mo_menu { display: block; }
  .mo-brand .mo-app { display: none; }
  .st-key-mo_coverage { display: none !important; }   /* the coverage strip says it */
  .st-key-mo_mtabs { display: flex !important; flex-wrap: nowrap !important; overflow-x: auto;
    border-bottom: 1px solid var(--mo-line); padding-bottom: 4px; margin-bottom: 6px; }
  .st-key-mo_mtabs [data-testid="stPageLink"] { flex: none; }
  .st-key-mo_mtabs [data-testid="stPageLink"] a { white-space: nowrap; }
  /* Icon-only Menu button on phones; the word stays for screen readers. */
  .st-key-mo_menu button [data-testid="stMarkdownContainer"] { position: absolute; width: 1px;
    height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .st-key-mo_topbar { flex-wrap: nowrap !important; gap: 8px !important; }
  .st-key-mo_topbar > div:has(> .st-key-mo_links),
  .st-key-mo_topbar > div:has(> .st-key-mo_menu),
  .st-key-mo_topbar > div:has(> .st-key-mo_coverage) { display: none !important; }
  .st-key-mo_menu { display: none !important; }          /* the tab row replaces it */
  .st-key-mo_lang [data-testid="stButtonGroup"] button { padding: 0 7px; min-width: 0; }
  .mo-brand .mo-word { font-size: 1.0625rem; letter-spacing: .06em; }
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

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important;
    transition-duration: .01ms !important; scroll-behavior: auto !important; }
}
"""

# Public pages hide Streamlit's menu, toolbar, deploy button, footer and status widget.
_PUBLIC_CSS = """
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stMainMenu"],
#MainMenu, [data-testid="stDecoration"], [data-testid="stStatusWidget"],
[data-testid="stAppDeployButton"], .stDeployButton, footer,
[data-testid="stSidebarCollapsedControl"], [data-testid="stSidebar"] { display: none !important; }
[data-testid="stMain"] .block-container { padding-top: .25rem; }
"""

# Officer pages keep Streamlit's rerun control but lose the decoration line and sidebar.
_OFFICER_CSS = """
[data-testid="stHeader"] { background: transparent !important; height: 2.25rem; }
[data-testid="stDecoration"], [data-testid="stAppDeployButton"], .stDeployButton,
[data-testid="stSidebarCollapsedControl"], [data-testid="stSidebar"] { display: none !important; }
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
HACKATHON = "Smart India Hackathon"
CREDIT = ("Problem statement by the Ministry of Earth Sciences, Government of India, "
          f"{HACKATHON}. Independent prototype, not an official government service.")


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
def top_bar(pages: dict, user=None) -> None:
    """Slim bar: VRRTANTA logo + app name, coverage badge, links, Dark mode, sign in.

    `pages` maps a short name to its st.Page so links are real navigation. On phones
    the links collapse into a Menu popover (CSS, .st-key-mo_menu).
    """
    is_dark()                                   # seed the toggle from the browser
    with st.container(key="mo_topbar", horizontal=True, vertical_alignment="center",
                      gap="small"):
        st.html(f"<a class='mo-brand' href='./' target='_self' "
                f"aria-label='VRRTANTA Monsoon outlook, home'>{brand_mark(30)}"
                f"<span class='mo-word'>VRRTANTA</span>"
                f"<span class='mo-app'>Monsoon outlook</span></a>", width="content")
        from app.i18n import LANGS, t

        with st.popover(t("coverage_badge"), key="mo_coverage"):
            st.markdown(t("coverage"))
        st.space("stretch")

        with st.container(key="mo_links", horizontal=True, gap="small",
                          vertical_alignment="center", width="content"):
            _links(pages, user)
        with st.container(key="mo_menu", width="content"):
            with st.popover("Menu", icon=":material/menu:"):
                _links(pages, user, stacked=True)

        if "lang" not in st.session_state:
            st.session_state["lang"] = "en"
        with st.container(key="mo_lang", width="content"):
            short = {"en": "EN", "hi": "हिं", "mr": "मरा"}
            st.segmented_control(t("lang_label"), list(LANGS), format_func=short.get,
                                 key="lang", label_visibility="collapsed", required=True,
                                 help=" / ".join(LANGS.values()))
        st.toggle(f":material/dark_mode: {t('dark_mode')}", key="dark")
    # Phones: the four tabs as a scrollable row under the bar (the approved wireframe).
    with st.container(key="mo_mtabs", horizontal=True, gap="small"):
        st.page_link(pages["home"], label=t("tab_map"))
        st.page_link(pages["outlook"], label=t("tab_30"))
        st.page_link(pages["accuracy"], label=t("tab_accuracy"))
        st.page_link(pages["about"], label=t("tab_about"))
        if user is None:
            st.page_link(pages["signin"], label=t("sign_in"))
        else:
            st.page_link(pages["overview"], label="Officer portal")


def _links(pages: dict, user, stacked: bool = False) -> None:
    from app.i18n import t

    st.page_link(pages["home"], label=t("tab_map"))
    st.page_link(pages["outlook"], label=t("tab_30"))
    st.page_link(pages["accuracy"], label=t("tab_accuracy"))
    st.page_link(pages["about"], label=t("tab_about"))
    if user is None:
        st.page_link(pages["signin"], label=t("sign_in"),
                     icon=":material/login:")
    else:
        st.page_link(pages["overview"], label="Officer portal",
                     icon=":material/badge:")
        if st.button("Sign out", key=f"mo_signout_{'m' if stacked else 'd'}",
                     type="tertiary"):
            from app.common import log

            log(user.username, "logout", "user", user.username)
            st.session_state.pop("user", None)
            st.switch_page(pages["home"])


def officer_subnav(pages: dict, user) -> None:
    """Second row on officer pages: where you are in the portal and who you are."""
    with st.container(key="mo_subnav", horizontal=True, vertical_alignment="center",
                      gap="small"):
        for name in ("overview", "risk_map", "approvals", "custom_alert", "outbox",
                     "models"):
            st.page_link(pages[name])
        scope = "all states" if user.role == "admin" else ", ".join(user.states())
        st.space("stretch")
        st.caption(f"{user.name} · {user.role} · {scope}")


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
