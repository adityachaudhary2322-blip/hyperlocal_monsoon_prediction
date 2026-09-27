"""Risk changes: what escalated since the previous run, and a choropleth of one state's
sub-districts for one hazard and horizon.

Geometry comes from `app/assets/map_layers_simplified.gpkg`, committed and simplified to
2.1 MB by `scripts/build_map_assets.py`. `data/processed/units.gpkg` is gitignored and
built by the Phase 1 pipeline, so it does not exist on Streamlit Community Cloud.

**One state at a time, on purpose.** The free tier gives ~2.7 GB of RAM for the whole
container, and the map is the only page that holds polygons. Reading the GeoPackage with
a `state = ...` filter loads 53 shapes instead of 739 - measured 11x faster than reading
the layer whole - and the result is cached per state, so switching states costs one read
and never accumulates all five.
"""

from __future__ import annotations

from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from app.common import RISK_COLOURS, require_login, risk_chip, visible_states
from app.theme import page_header, status_label
from src import demo

RISK_TINTS = {"green": "#DCEFE3", "amber": "#F8E8C4", "red": "#F4D5D2"}
from src.db.models import Advisory, Forecast, ForecastChange, Unit
from src.db.session import get_session

HAZARDS = {"dry": "Dry spell", "heavy": "Heavy rain", "onset": "Monsoon onset"}
HORIZONS = [7, 14, 21, 28]

ASSETS = Path(__file__).resolve().parents[1] / "assets"
MAP_LAYERS = ASSETS / "map_layers_simplified.gpkg"
MAP_LAYER_NAME = "units"

user = require_login()
page_header("Risk changes", "Where risk went up since the previous forecast, and a map of "
            "one state's sub-districts for one hazard and week.")


@st.cache_data(show_spinner="Loading map layer...", max_entries=2, ttl=3600)
def load_state_geometry(state: str) -> dict:
    """GeoJSON features for one state, keyed by unit_id.

    `max_entries=2` is the memory guard: a user flipping through states keeps at most
    two states' polygons alive instead of all five.
    """
    if not MAP_LAYERS.is_file():
        return {}

    import geopandas as gpd

    # Quoted for SQL; no Indian state name contains an apostrophe today, but a
    # doubled quote costs nothing and keeps the filter from ever being malformed.
    escaped = state.replace("'", "''")
    frame = gpd.read_file(MAP_LAYERS, layer=MAP_LAYER_NAME,
                          where=f"state = '{escaped}'")
    if frame.empty:
        return {}

    payload = frame.to_json(drop_id=True)
    import json

    return {f["properties"]["unit_id"]: f
            for f in json.loads(payload)["features"]}


session = get_session()
try:
    run = demo.latest_real_run(session)
    if run is None:
        st.info("No forecast yet. New forecasts arrive every morning.",
                icon=":material/schedule:")
        st.stop()

    scope = visible_states(user)
    unit_query = session.query(Unit)
    if scope is not None:
        unit_query = unit_query.filter(Unit.state.in_(scope))
    units = pd.read_sql(unit_query.statement, session.connection())

    forecasts = pd.read_sql(
        session.query(Forecast).filter(Forecast.run_id == run.id).statement,
        session.connection())
    # The demo account sees its demo copies; everyone else the real run's advisories.
    advisory_run = demo.advisory_run(session, user)
    advisories = pd.read_sql(
        demo.advisory_scope(session.query(Advisory), session, user)
        .filter(Advisory.run_id == (advisory_run.id if advisory_run else -1)).statement,
        session.connection())
    changes = pd.read_sql(
        session.query(ForecastChange)
        .filter(ForecastChange.run_id == run.id, ForecastChange.escalated.is_(True)).statement,
        session.connection())
finally:
    session.close()

if units.empty:
    st.info("No units are in your scope.")
    st.stop()

# ------------------------------------------------------------ what changed
st.subheader("Escalated since the previous run")
changes = changes[changes["unit_id"].isin(set(units["unit_id"]))]
if changes.empty:
    st.caption("Nothing escalated since the previous run.")
else:
    view = changes.merge(units[["unit_id", "unit_name", "district"]], on="unit_id", how="left")
    view = view[["unit_name", "district", "hazard", "horizon", "previous_risk",
                 "risk_level", "delta"]].sort_values("delta", ascending=False)
    view["hazard"] = view["hazard"].map(HAZARDS).fillna(view["hazard"])
    view["horizon"] = view["horizon"].map(lambda h: f"week {int(h) // 7}")
    arrow = {"green": "● Low", "amber": "▲ Medium", "red": "◆ High"}
    view["previous_risk"] = view["previous_risk"].map(arrow).fillna("-")
    view["risk_level"] = view["risk_level"].map(arrow).fillna("-")
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        "unit_name": "Sub-district", "district": "District", "hazard": "Hazard",
        "horizon": "When", "previous_risk": "Was", "risk_level": "Now",
        "delta": st.column_config.ProgressColumn("Rise in chance", format="percent",
                                                 min_value=0.0, max_value=1.0),
    })

st.subheader("Map")
available_states = sorted(units["state"].unique())

controls = st.columns([2, 2, 2, 3])
state = controls[0].selectbox(
    "State", available_states,
    help="One state at a time keeps the map inside the hosted memory budget.")
hazard = controls[1].selectbox("Hazard", list(HAZARDS),
                               format_func=lambda h: HAZARDS[h])
horizon = controls[2].selectbox("Horizon", HORIZONS,
                                format_func=lambda h: f"{h} days", index=1)
controls[3].caption(
    f"Run {run.as_of} · green <30% · amber 30-60% · red >60%"
)

selection = forecasts[(forecasts["hazard"] == hazard) &
                      (forecasts["horizon"] == horizon)]
frame = units[units["state"] == state].merge(selection, on="unit_id", how="left")

geometry = load_state_geometry(state)
if not geometry:
    st.warning(
        f"No map geometry for {state}. Run "
        "`python scripts/build_map_assets.py` to write "
        "`app/assets/map_layers_simplified.gpkg`."
    )
    st.stop()

# One feature per unit in scope. The database's values win over the layer's copies, so
# a renamed or rezoned unit shows correctly without rebuilding the geometry.
features: list[dict] = []
latitudes: list[float] = []
longitudes: list[float] = []
for row in frame.itertuples():
    feature = geometry.get(row.unit_id)
    if feature is None:
        continue
    probability = getattr(row, "probability", None)
    properties = dict(feature["properties"])
    properties.update({
        "unit_id": row.unit_id,
        "unit_name": row.unit_name,
        "district": row.district,
        "state": row.state,
        "zone_id": getattr(row, "zone_id", None),
        "risk_level": getattr(row, "risk_level", None) or "unknown",
        "probability": (None if probability is None or pd.isna(probability)
                        else round(float(probability), 3)),
    })
    features.append({"type": "Feature", "geometry": feature["geometry"],
                     "properties": properties})
    latitudes.append(properties["centroid_lat"])
    longitudes.append(properties["centroid_lon"])

if not features:
    st.warning(f"No geometry matched the {len(frame)} unit(s) in {state}.")
    st.stop()

missing = len(frame) - len(features)
if missing:
    st.caption(f":material/info: {missing} unit(s) in {state} have no geometry in "
               "the map layer and are not drawn.")

counts = pd.Series([f["properties"]["risk_level"] for f in features]).value_counts()
chips = st.columns(4)
for column, level in zip(chips, ["red", "amber", "green", "unknown"]):
    column.metric(level.title(), int(counts.get(level, 0)))

centre = [sum(latitudes) / len(latitudes), sum(longitudes) / len(longitudes)]
# OpenStreetMap needs no API key; CartoDB basemaps now do, and without
# one the tiles come back blank.
fmap = folium.Map(location=centre, zoom_start=7,
                  tiles="OpenStreetMap", control_scale=True)

folium.GeoJson(
    {"type": "FeatureCollection", "features": features},
    name="risk",
    style_function=lambda feature: {
        "fillColor": RISK_COLOURS.get(
            feature["properties"].get("risk_level") or "unknown",
            RISK_COLOURS["unknown"]),
        "color": "white", "weight": 0.8, "fillOpacity": 0.75,
    },
    highlight_function=lambda _f: {"weight": 2.5, "color": "#0b0b0b"},
    tooltip=folium.GeoJsonTooltip(
        fields=["unit_name", "district", "state", "risk_level", "probability"],
        aliases=["Unit", "District", "State", "Risk", "Probability"],
        localize=True,
    ),
).add_to(fmap)

if len(features) > 1:
    fmap.fit_bounds([[min(latitudes), min(longitudes)],
                     [max(latitudes), max(longitudes)]])

left, right = st.columns([3, 2])
with left:
    clicked = st_folium(fmap, height=560, use_container_width=True,
                        returned_objects=["last_active_drawing"])

with right:
    st.subheader("Selected unit")
    unit_id = None
    drawing = (clicked or {}).get("last_active_drawing")
    if drawing:
        unit_id = (drawing.get("properties") or {}).get("unit_id")

    if not unit_id:
        st.caption("Click a sub-district on the map to see its probabilities and "
                   "the advisory it produced.")
    else:
        row = frame[frame["unit_id"] == unit_id].iloc[0]
        st.markdown(f"### {row['unit_name']}")
        st.caption(f"{row['district']}, {row['state']} · zone "
                   f"{row.get('zone_id') or 'unzoned'}")

        detail = forecasts[forecasts["unit_id"] == unit_id]
        table = (detail.pivot_table(index="hazard", columns="horizon",
                                    values="probability")
                 .reindex(list(HAZARDS)))

        # Cells are coloured in CSS rather than with Styler.background_gradient,
        # which would pull matplotlib into the hosted dependency set for one colormap.
        # Tints of the risk colours with ink text: the full-strength fills fail
        # WCAG AA under dark text, and the % in every cell carries the value anyway.
        def risk_css(value: float) -> str:
            if pd.isna(value):
                return "color:#5B6B76"
            tint = (RISK_TINTS["red"] if value > 0.6
                    else RISK_TINTS["amber"] if value >= 0.3
                    else RISK_TINTS["green"])
            return f"background-color:{tint};color:#1C2B36;font-weight:600"

        st.dataframe(
            table.style.format("{:.0%}", na_rep="-").map(risk_css),
            width="stretch",
        )

        advisory = advisories[advisories["unit_id"] == unit_id]
        if advisory.empty:
            st.info("No advisory for this unit on this run "
                    "(MONITOR advisories are not queued).")
        else:
            record = advisory.iloc[0]
            st.markdown(
                f"**{record['action']}** &nbsp; {risk_chip(record['risk_level'])} "
                f"&nbsp; <span style='color:var(--mo-muted)'>confidence "
                f"{record['confidence']}</span> &nbsp; {status_label(record['status'])}",
                unsafe_allow_html=True,
            )
            if record.get("status_reason"):
                st.caption(record["status_reason"])
            tabs = st.tabs(["English", "हिन्दी", "मराठी"])
            for tab, column in zip(tabs, ["text_en", "text_hi", "text_mr"]):
                with tab:
                    body = record.get(column)
                    if body:
                        st.write(body)
                    else:
                        st.caption("Not available in this language yet.")
