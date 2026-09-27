"""Accuracy: how the forecasts did in past seasons and since going live - in plain words."""

from __future__ import annotations

import datetime as dt
import html

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from app import public_data as pdata
from app.i18n import lang, t
from app.theme import footer, tokens
from src.accuracy import score, sentences, verdict
from src.common import load_config

code = lang()
cfg = load_config("accuracy")
HAZARDS = ["dry", "heavy", "onset"]
VERDICT = {"better": ("✓", "accent-ink", "Better than the historical average"),
           "about the same": ("≈", "muted", "About the same as the historical average"),
           "worse": ("✕", "risk-high-ink", "Worse than the historical average"),
           "not enough data": ("·", "muted", "Not enough data")}

st.title(t("tab_accuracy"))
st.html("<p class='mo-prose mo-lede'>We do not publish one overall accuracy figure: for a rare "
        "event, always saying “no” would look over 90% accurate while never warning anyone. "
        "Instead, each hazard and week is compared with the historical average for that time "
        "of year, and we show how often our high, medium and low chances came true.</p>")

# --------------------------------------------------------------------------
# Past seasons (test years, pilot sub-districts of the validated states)
# --------------------------------------------------------------------------
past = pdata.accuracy_test_years()
st.header("Past seasons")
if not past:
    st.info("Past-season scores are not available yet.")
else:
    st.caption(f"Test years {past['years']}, which the model never saw while learning · "
               f"{past['scope']} · validated tier. Experimental states: national models are "
               "coming soon, so there are no past-season scores for them yet.")
    rows = pd.DataFrame(past["rows"])
    hz = st.segmented_control(t("monsoon_risk"), HAZARDS, default="dry", required=True,
                              format_func=lambda h: t(f"hazard_{h}"), key="acc_hazard")
    sub = rows[(rows["hazard"] == hz) & (rows["state"] == "All validated states")].sort_values("week")
    table = []
    for r in sub.itertuples():
        icon, ink, words = VERDICT[r.verdict]
        table.append(f"<tr><td>{html.escape(t('week_card', n=r.week))}</td>"
                     f"<td><span class='mo-label' style='color:var(--mo-{ink})'>{icon} {words}</span></td>"
                     f"<td class='mo-num'>{'-' if r.hit_rate is None or pd.isna(r.hit_rate) else f'{r.hit_rate:.0%}'}</td>"
                     f"<td class='mo-num'>{'-' if r.false_alarm_ratio is None or pd.isna(r.false_alarm_ratio) else f'{r.false_alarm_ratio:.0%}'}</td>"
                     f"<td class='mo-num'>{int(r.n):,} ({int(r.events):,})</td></tr>")
    st.html("<div class='mo-table-wrap'><table class='mo-table'><thead><tr><th>Looking ahead</th>"
            "<th>Compared with the historical average</th><th>Warned of events</th>"
            "<th>Warnings that did not happen</th><th>Cases (events)</th></tr></thead>"
            f"<tbody>{''.join(table)}</tbody></table></div>")
    st.caption("A warning is a chance of 30% or more. “Warned of events” is the share of real "
               "events we had warned about; “warnings that did not happen” is the share of our "
               "warnings where the event did not come.")

    week = st.segmented_control(t("looking_ahead"), [1, 2, 3, 4], default=1, required=True,
                                format_func=lambda n: t("week_card", n=n), key="acc_week")
    row = sub[sub["week"] == week]
    if len(row):
        r = row.iloc[0]
        for line in r["sentences"]:
            st.html(f"<p class='mo-prose'>{html.escape(line)}</p>")
        bins = pd.DataFrame([b for b in r["bins"] if b["n"] and b["observed"] is not None])
        if len(bins):
            tok = tokens()
            diag = pd.DataFrame({"x": [0, 1], "y": [0, 1]})
            chart = (alt.Chart(diag).mark_line(strokeDash=[4, 4], color=tok["muted"])
                     .encode(x=alt.X("x:Q", title="What we said (chance)", scale=alt.Scale(domain=[0, 1]),
                                     axis=alt.Axis(format="%")),
                             y=alt.Y("y:Q", title="How often it happened", scale=alt.Scale(domain=[0, 1]),
                                     axis=alt.Axis(format="%")))
                     + alt.Chart(bins).mark_line(point=alt.OverlayMarkDef(size=90), color=tok["primary"])
                     .encode(x="forecast:Q", y="observed:Q",
                             tooltip=[alt.Tooltip("label:N", title="We said"),
                                      alt.Tooltip("observed:Q", title="It happened", format=".0%"),
                                      alt.Tooltip("n:Q", title="Forecasts")]))
            st.altair_chart(chart.properties(height=260), use_container_width=True)
            st.caption("Reliability: points on the dashed line mean our chances came true as "
                       "often as we said.")
    with st.expander("By state"):
        by = rows[(rows["hazard"] == hz) & (rows["week"] == week) & (rows["state"] != "All validated states")]
        st.dataframe(pd.DataFrame({
            "State": by["state"], "Compared with average": by["verdict"],
            "Warned of events": by["hit_rate"].map(lambda v: "-" if v is None or pd.isna(v) else f"{v:.0%}"),
            "Cases": by["n"], "Events": by["events"]}), hide_index=True, use_container_width=True)

# --------------------------------------------------------------------------
# Live scorecard
# --------------------------------------------------------------------------
st.header("Since going live")
frame = pdata.live_verification_frame()
need = cfg["min_verified"]
if len(frame) < need:
    today = dt.date.today()
    monday = today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)
    ready = monday + dt.timedelta(days=7 - 1 + 9 + 1)
    st.info(f"Not enough results yet, check back after {ready:%d %b %Y}. Each Monday's forecasts "
            f"are checked against the rain that actually fell once their week has passed "
            f"({len(frame)} checked so far).", icon=":material/hourglass_top:")
else:
    lines = []
    for (hz_, h, tier), g in frame.groupby(["hazard", "horizon", "tier"]):
        y = g["outcome"].astype(float).to_numpy()
        s = score(y, g["p_blend"].to_numpy(dtype=float) / 1000)
        lines.append({"Hazard": t(f"hazard_{hz_}"), "Week": int(h // 7), "Tier": t(f"tier_{tier}"),
                      "Checked": s["n"], "Events": s["events"],
                      "Hit rate": None if s.get("hit_rate") is None else round(s["hit_rate"], 2),
                      "False alarms": None if s.get("false_alarm_ratio") is None
                      else round(s["false_alarm_ratio"], 2),
                      "Brier": round(s["brier"], 3), "Brier (average)": round(s["brier_ref"], 3),
                      "vs average": verdict(s.get("bss"))})
    st.dataframe(pd.DataFrame(lines), hide_index=True, use_container_width=True)
    st.markdown("**Last 10 verified forecasts**")
    last = frame.sort_values("run_date", ascending=False).head(10)
    st.dataframe(pd.DataFrame({
        "Issued": last["run_date"], "Hazard": last["hazard"].map(lambda h: t(f"hazard_{h}")),
        "Weeks": (last["horizon"] // 7).astype(int), "Chance": (last["p_blend"] / 1000).map("{:.0%}".format),
        "Happened": last["outcome"].map({True: "yes", False: "no"})}), hide_index=True,
        use_container_width=True)

    st.markdown("**Blend vs each part**")
    comp = []
    for (hz_, h), g in frame.groupby(["hazard", "horizon"]):
        y = g["outcome"].astype(float).to_numpy()
        row = {"Hazard": t(f"hazard_{hz_}"), "Week": int(h // 7)}
        for col, name in (("p_blend", "Blend"), ("p_ml", "ML only"), ("p_ec46", "EC46 only")):
            p = g[col].to_numpy(dtype=float) / 1000
            row[name] = "coming soon" if not np.isfinite(p).any() else \
                f"{score(y, p).get('brier', float('nan')):.3f}"
        comp.append(row)
    st.dataframe(pd.DataFrame(comp), hide_index=True, use_container_width=True)
    st.caption("Brier score: lower is better. Suggested blend weights are computed nightly for "
               "officers to review; they are never applied automatically.")

footer()
