"""Next 30 days: the full outlook for one area, readable without the map."""

from __future__ import annotations

import html

import altair as alt
import pandas as pd
import streamlit as st

from app import public_data as pdata
from app import unit_detail
from app import scenes
from app.i18n import lang, t
from app.theme import footer, page_header, tokens

code = lang()
run = pdata.live_run()
_picked = st.query_params.get("unit") or (st.session_state.get("mo_map") or {}).get("unit")
page_header(t("tab_30"), t("desc_30"), forecast_mode=scenes.unit_forecast_mode(_picked))
if run is None:
    st.info(t("empty_no_run"), icon=":material/schedule:")
    footer()
    st.stop()

units = pdata.live_units().sort_values(["state", "district", "unit_name"])
units = units[units["name_ok"]]
labels = {r.unit_id: f"{r.unit_name} · {r.district}, {r.state}" for r in units.itertuples()}
ids = list(labels)
chosen = st.query_params.get("unit") or (st.session_state.get("mo_map") or {}).get("unit") or ""
unit_id = st.selectbox(t("search_placeholder"), ids, index=ids.index(chosen) if chosen in ids else None,
                       format_func=labels.get, placeholder=t("search_placeholder"))
if not unit_id:
    st.html(f"<p class='mo-prose mo-muted'>{html.escape(t('pick_area'))}</p>")
    footer()
    st.stop()
st.query_params["unit"] = unit_id

try:
    base = str(st.context.url or "")
except Exception:
    base = ""
d = unit_detail.build(unit_id, code, base)
if d is None:
    st.warning(t("error_generic"))
    footer()
    st.stop()

tier_cls = {"validated": "validated", "experimental": "experimental", "low": "low"}[d["tier"]]
st.html(f"<h2 class='mo-unit-title'>{html.escape(d['name'])}</h2>"
        f"<p class='mo-muted'>{html.escape(d['district'])}, {html.escape(d['state'])} · "
        f"<span class='mo-tier mo-tier-{tier_cls}' title='{html.escape(d['tier_help'])}'>"
        f"{html.escape(d['tier_text'])}</span> · {html.escape(d['updated'])}</p>")
if d["delayed"]:
    st.warning(d["delayed_text"], icon=":material/hourglass_top:")

LEVEL_ICON = {"red": "◆", "amber": "▲", "green": "●"}
LEVEL_INK = {"red": "risk-high-ink", "amber": "risk-med-ink", "green": "risk-low-ink"}
if not d["has_forecast"]:
    st.info(t("no_forecast_unit"))
else:
    cols = st.columns(4)
    for col, wk in zip(cols, d["weeks"]):
        with col.container(border=True):
            st.markdown(f"**{wk['title']}**")
            for it in wk["items"]:
                st.html(f"<p class='mo-card-line'><span class='mo-label' style='color:var(--mo-"
                        f"{LEVEL_INK.get(it['level'], 'muted')})'>{LEVEL_ICON.get(it['level'], '○')} "
                        f"{html.escape(it['level_text'])}</span> · "
                        f"{html.escape(it['sentence'])}</p>")

if d["outlook"]:
    tok = tokens()
    frame = pd.DataFrame(d["outlook"])
    frame["label"] = [t("week_card", n=w) for w in frame["week"]]
    base_chart = alt.Chart(frame).encode(x=alt.X("label:N", sort=None, title=None,
                                                 axis=alt.Axis(labelAngle=0)))
    chart = (base_chart.mark_bar(size=34, opacity=0.35, color=tok["primary"])
             .encode(y=alt.Y("p10:Q", title="mm"), y2="p90:Q",
                     tooltip=[alt.Tooltip("p10:Q", title="10%"), alt.Tooltip("p50:Q", title="50%"),
                              alt.Tooltip("p90:Q", title="90%"), alt.Tooltip("normal:Q")])
             + base_chart.mark_tick(thickness=3, size=34, color=tok["primary"]).encode(y="p50:Q")
             + base_chart.mark_tick(thickness=2, size=48, color=tok["accent-ink"],
                                    strokeDash=[5, 3]).encode(y="normal:Q"))
    st.markdown(f"#### {t('chart_title')}")
    st.altair_chart(chart.properties(height=220), use_container_width=True)
    st.caption(f"{t('chart_range')} · {t('chart_median')} · {t('chart_normal')}")
    st.html(f"<p class='mo-prose'>{html.escape(d['month_sentence'])}</p>")

st.markdown(f"#### {t('weather_now')}")
w = d["weather"]
m1, m2, m3, m4 = st.columns(4)
m1.metric(t("rain_last_7"), "-" if w.get("rain_7d") is None else f"{w['rain_7d']:.0f} mm",
          None if w.get("normal_7d") is None else f"{t('normal')} {w['normal_7d']:.0f} mm",
          delta_color="off")
m2.metric(t("rain_last_14"), "-" if w.get("rain_14d") is None else f"{w['rain_14d']:.0f} mm",
          None if w.get("normal_14d") is None else f"{t('normal')} {w['normal_14d']:.0f} mm",
          delta_color="off")
m3.metric(t("temperature_now"), "-" if w.get("temp_c") is None else f"{w['temp_c']} °C")
m4.metric(t("humidity_now"), "-" if w.get("rh_pct") is None else f"{w['rh_pct']:.0f}%")

st.markdown(f"#### {t('crops_here')}")
for c in d["crops"]:
    ink = {"high": "risk-high-ink", "medium": "risk-med-ink", "low": "risk-low-ink"}[c["level"]]
    icon = {"high": "◆", "medium": "▲", "low": "●"}[c["level"]]
    notes = "".join(f"<p class='mo-muted'>{html.escape(n)}</p>" for n in c["notes"])
    st.html(f"<div class='mo-crop-row mo-prose'><p><b>{html.escape(c['name'])}</b> "
            f"<span class='mo-label' style='color:var(--mo-{ink})'>{icon} {html.escape(c['level_text'])}"
            f"</span></p><p>{html.escape(c['reason'])}</p>{notes}</div>")
st.caption(d["crops_note"])

st.markdown(f"#### {t('advisory')}")
adv = d["advisory"]
if adv:
    names = {"hi": "हिन्दी", "mr": "मराठी", "en": "English"}
    langs = [l for l in ("hi", "mr", "en") if adv.get(l)]
    for tab, l in zip(st.tabs([names[l] for l in langs]), langs):
        tab.html(f"<p class='mo-prose' lang='{l}'>{html.escape(adv[l])}</p>")
else:
    st.caption(t("no_advisory"))

st.caption(d["source_line"] + (f" {d['ml_note']}" if d["ml_note"] else ""))
if d["share"]:
    st.link_button(t("share_whatsapp"), d["share"], icon=":material/share:")
footer()
