// Public MapLibre map (st.components.v2).
//
// Mounted once per page; Streamlit re-invokes the default export with new `data` on each
// rerun and the DOM survives (verified on 1.64: same parent element, no cleanup between
// reruns), so the map, the wind particles and any playback keep running while the page
// updates around them.
//
// The whole control panel lives here and reports choices back with setStateValue
// (hazard / week / weather), so the Python summary below the map follows the map.
//
// Boundaries: every basemap `boundary` layer and every country/state label is removed;
// the only borders drawn come from app/static/ (Survey of India layers). See
// scripts/build_public_map_assets.py.

const LIB_JS = "https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.js";
const LIB_JS_SRI = "sha384-5+cfbwT0iiub6VsQAdn6yz16nr6sDiQoHx6tm4O8OVYXHYOxcffFmCJBL0dgdvGp";
const LIB_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.css";
const LIB_CSS_SRI = "sha384-uTttxo/aOKbdE5RlD/SPzSDoDmNvGlUYPjONi2MN/b7c9HPSvW07OIuyP7uL6jxK";
const STYLES = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};
const INDIA = { center: [80.5, 22.8], zoom: 3.55 };
const INDIA_BOUNDS = [[68.1, 6.5], [97.4, 37.1]];   // Survey of India outline extent
const INTRO_FROM = { center: [0, 18], zoom: 1.25 };
const RISK_ZOOM = 4.8;    // covered states switch from weather to monsoon risk
const UNIT_ZOOM = 6.8;    // districts -> sub-districts
const WIND_MIN_ZOOM = 2.6; // particles only where the whole view faces the viewer
const BREAKS = { rain24: [1, 5, 15, 40], rain7: [10, 35, 80, 150], temp: [18, 24, 30, 36] };
const WEATHER_OPTS = [["rain24", "Rain 24 h"], ["rain7", "Rain 7 days"], ["temp", "Temperature"]];
const WEATHER_TITLE = {
  rain24: "Rain in the next 24 hours (mm)",
  rain7: "Rain in the next 7 days (mm)",
  temp: "Temperature now (°C)",
};
const WEATHER_FIELD = { rain24: "r24", rain7: "r7", temp: "t" };
const WEATHER_UNIT = { rain24: "mm", rain7: "mm", temp: "°C" };
const HAZARDS = [["onset", "Monsoon onset"], ["dry", "Dry spell"], ["heavy", "Heavy rain"]];
const HAZARD_SHORT = { onset: "Onset", dry: "Dry spell", heavy: "Heavy rain" };
const WEEKS = [[1, "1 week"], [2, "2 weeks"], [3, "3 weeks"], [4, "4 weeks"]];
const LEVELS = {
  red: ["◆", "High", "risk-high-ink"],
  amber: ["▲", "Medium", "risk-med-ink"],
  green: ["●", "Low", "risk-low-ink"],
};
const LANG_LABEL = { hi: "हिन्दी", mr: "मराठी", en: "English" };
// Rain playback: mm/h stops. Transparent below 0.5 mm/h, as the brief asks.
const RAIN_STOPS = [0.5, 1, 2.5, 5, 10, 20];
const RAIN_COLORS = {
  light: [[207, 227, 240, .42], [168, 204, 224, .55], [92, 156, 196, .66], [46, 94, 126, .74], [23, 58, 82, .82], [14, 36, 52, .88]],
  dark: [[45, 84, 112, .50], [79, 131, 170, .60], [127, 176, 212, .70], [181, 214, 236, .78], [230, 242, 250, .85], [255, 255, 255, .92]],
};
const SAND = { light: "#E8DCC4", dark: "#4A4538" };
const MONSOON = { light: "#2E5E7E", dark: "#7FB0D4" };
const SS_CAMERA = "mo-camera-v1";
const SS_INTRO = "mo-intro-done-v1";
const SS_PANEL = "mo-panel-collapsed-v1";

// ------------------------------------------------------------------ helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (p) => (p == null ? "—" : `${Math.round(p * 100)}%`);
const ss = {
  get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};
const reducedMotion = () => {
  try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
  catch (e) { return false; }
};
const isPhone = () => {
  try { return window.matchMedia("(max-width: 720px)").matches; } catch (e) { return false; }
};
const staticUrl = (path) => new URL(`app/static/${path}`, document.baseURI).toString();

function levelHtml(level) {
  const l = LEVELS[level];
  if (!l) return `<span style="color:var(--mo-muted)">○ No forecast</span>`;
  return `<span style="color:var(--mo-${l[2]})"><span aria-hidden="true">${l[0]}</span> ${l[1]}</span>`;
}

function bboxOf(geometry) {
  let minx = 180, miny = 90, maxx = -180, maxy = -90;
  const walk = (c) => {
    if (!c || !c.length) return;
    if (typeof c[0] === "number") {
      if (c[0] < minx) minx = c[0]; if (c[0] > maxx) maxx = c[0];
      if (c[1] < miny) miny = c[1]; if (c[1] > maxy) maxy = c[1];
    } else c.forEach(walk);
  };
  if (!geometry) return [68, 6, 98, 37];
  if (geometry.type === "GeometryCollection") geometry.geometries.forEach((g) => walk(g.coordinates || []));
  else walk(geometry.coordinates || []);
  return [minx, miny, maxx, maxy];
}

function loadLib() {
  if (window.maplibregl) return Promise.resolve();
  if (window.__moLib) return window.__moLib;
  if (!document.querySelector(`link[href="${LIB_CSS}"]`)) {
    const link = document.createElement("link");
    Object.assign(link, { rel: "stylesheet", href: LIB_CSS, integrity: LIB_CSS_SRI,
                          crossOrigin: "anonymous" });
    document.head.appendChild(link);
  }
  window.__moLib = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    Object.assign(s, { src: LIB_JS, integrity: LIB_JS_SRI, crossOrigin: "anonymous",
                       onload: resolve, onerror: () => reject(new Error("MapLibre failed to load")) });
    document.head.appendChild(s);
  });
  return window.__moLib;
}

const jsonCache = new Map();
function getJSON(path) {
  // Static files are fetched once per page and then served from the browser cache;
  // the grid files carry ?v=<fetch time> so a new run busts the cache.
  if (!jsonCache.has(path)) {
    jsonCache.set(path, fetch(staticUrl(path)).then((r) => {
      if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
      return r.json();
    }).catch((e) => { jsonCache.delete(path); throw e; }));
  }
  return jsonCache.get(path);
}

// Time labels in India Standard Time, e.g. "Tue 3 PM".
const IST = "Asia/Kolkata";
// IST is UTC+5:30, so the minutes are kept: "Tue 3:30 PM", never a rounded "3 PM".
const fmtHour = (d) => new Intl.DateTimeFormat("en-IN", { weekday: "short", hour: "numeric",
  minute: "2-digit", hour12: true, timeZone: IST }).format(d).replace(/\s?am/i, " AM").replace(/\s?pm/i, " PM");
const fmtDay = (d) => new Intl.DateTimeFormat("en-IN", { weekday: "short", day: "numeric",
  month: "short", timeZone: IST }).format(d);
const fmtDate = (d) => new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short",
  year: "numeric", timeZone: "UTC" }).format(d);

// ------------------------------------------------------------------ DOM
function segmented(name, legend, options, selected) {
  return `<fieldset class="mo-seg" role="radiogroup" aria-label="${esc(legend)}">
    <legend>${esc(legend)}</legend><div class="mo-seg-row">${options.map(([v, l]) =>
      `<label><input type="radio" name="${name}" value="${v}" ${String(v) === String(selected) ? "checked" : ""}>
       <span>${esc(l)}</span></label>`).join("")}</div></fieldset>`;
}

function buildDom(root) {
  const el = document.createElement("div");
  el.className = "mo-root";
  el.innerHTML = `
    <div class="mo-map">
      <div class="mo-canvas" role="region" aria-label="Map of India with weather and monsoon risk"></div>
      <canvas class="mo-wind" aria-hidden="true"></canvas>
      <div class="mo-search mo-surface" role="search">
        <label for="mo-q" class="mo-sr">Search a district or sub-district</label>
        <svg class="mo-search-ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"
          fill="none" stroke="currentColor" stroke-width="2"/><path d="M15.5 15.5 21 21" stroke="currentColor"
          stroke-width="2" stroke-linecap="round"/></svg>
        <input id="mo-q" type="search" autocomplete="off" spellcheck="false"
          placeholder="Search a district or sub-district" role="combobox" aria-expanded="false"
          aria-controls="mo-results" aria-autocomplete="list">
        <ul id="mo-results" class="mo-results" role="listbox" hidden></ul>
      </div>
      <div class="mo-legend mo-surface" aria-live="polite">
        <button class="mo-legend-toggle" type="button" aria-expanded="true">Legend</button>
        <div class="mo-legend-body"></div>
      </div>
      <section class="mo-panel mo-surface" hidden tabindex="-1" aria-label="Details"></section>
      <div class="mo-tip mo-surface" hidden></div>
      <div class="mo-note mo-surface" hidden></div>
      <div class="mo-hint mo-surface" hidden></div>
    </div>
    <div class="mo-timeline mo-surface" hidden role="group" aria-label="Playback">
      <button type="button" class="mo-play" aria-label="Play">▶</button>
      <div class="mo-tl-main">
        <div class="mo-tl-top"><span class="mo-tl-label" aria-live="off"></span><span class="mo-tl-count"></span></div>
        <input type="range" class="mo-scrub" min="0" max="0" value="0" aria-label="Playback position">
        <p class="mo-tl-caption"></p>
      </div>
      <div class="mo-speed" role="radiogroup" aria-label="Speed">
        <label><input type="radio" name="mo-speed" value="1" checked><span>1×</span></label>
        <label><input type="radio" name="mo-speed" value="2"><span>2×</span></label>
      </div>
      <button type="button" class="mo-tl-close" aria-label="Close playback">×</button>
    </div>
    <form class="mo-controls mo-surface" aria-label="Map options" onsubmit="return false">
      <div class="mo-controls-head">
        <h2>Map options</h2>
        <button type="button" class="mo-collapse" aria-expanded="true">Hide</button>
      </div>
      <div class="mo-controls-body">
        <div class="mo-grp"><h3>Weather layer</h3><div data-slot="weather"></div></div>
        <div class="mo-grp"><h3>Monsoon risk</h3><div data-slot="hazard"></div><div data-slot="week"></div>
          <p class="mo-window" aria-live="polite"></p></div>
        <div class="mo-grp" data-slot="anim-group"><h3>Animations</h3>
          <label class="mo-switch" data-anim="wind"><input type="checkbox" class="mo-wind-on">
            <span class="mo-switch-ui" aria-hidden="true"></span><span>Wind flow <small>850 hPa, now</small></span></label>
          <button type="button" class="mo-anim-btn" data-anim="rain"><span aria-hidden="true">▶</span> Rain forecast <small>next 7 days</small></button>
          <div class="mo-anim-row" data-anim="advance">
            <button type="button" class="mo-anim-btn"><span aria-hidden="true">▶</span> Monsoon advance</button>
            <label class="mo-sr" for="mo-season">Season</label>
            <select id="mo-season" class="mo-season"></select>
          </div>
        </div>
      </div>
    </form>`;
  root.appendChild(el);
  const q = (s) => el.querySelector(s);
  return { root: el, wrap: q(".mo-map"), canvas: q(".mo-canvas"), wind: q(".mo-wind"),
           input: q("#mo-q"), results: q("#mo-results"),
           legend: q(".mo-legend"), legendBody: q(".mo-legend-body"), legendToggle: q(".mo-legend-toggle"),
           panel: q(".mo-panel"), tip: q(".mo-tip"), note: q(".mo-note"), hint: q(".mo-hint"),
           controls: q(".mo-controls"), timeline: q(".mo-timeline"), play: q(".mo-play"),
           scrub: q(".mo-scrub"), tlLabel: q(".mo-tl-label"), tlCount: q(".mo-tl-count"),
           tlCaption: q(".mo-tl-caption"), tlClose: q(".mo-tl-close"), season: q("#mo-season") };
}

// ------------------------------------------------------------------ style
function stripBasemap(_prev, next) {
  // India's borders and names must come from our Survey of India layers only.
  next.layers = next.layers.filter((l) => {
    if (l["source-layer"] === "boundary") return false;
    if (l["source-layer"] === "place" && /country|state/.test(l.id)) return false;
    if (/(^|_)(label_)?(country|state)/.test(l.id) && l.type === "symbol") return false;
    return true;
  }).map((l) => (
    // Town and city names only once the viewer zooms in: at the India view the only
    // names are our Survey of India state labels, which keeps the national map calm.
    l["source-layer"] === "place" ? { ...l, minzoom: Math.max(l.minzoom || 0, 5.5) }
      : l["source-layer"] === "water_name" ? { ...l, minzoom: Math.max(l.minzoom || 0, 5) } : l));
  return next;
}

function hatchImage(color) {
  const size = 12, c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  g.strokeStyle = color; g.lineWidth = 1.2;
  g.beginPath();
  g.moveTo(0, size); g.lineTo(size, 0);
  g.moveTo(-size / 2, size / 2); g.lineTo(size / 2, -size / 2);
  g.moveTo(size / 2, size * 1.5); g.lineTo(size * 1.5, size / 2);
  g.stroke();
  return g.getImageData(0, 0, size, size);
}

const styleCache = new Map();
function basemap(theme) {
  // Fetched and filtered before MapLibre sees it, so no boundary or country label is
  // ever drawn - not even for one frame.
  if (!styleCache.has(theme)) {
    styleCache.set(theme, fetch(STYLES[theme]).then((r) => {
      if (!r.ok) throw new Error(`basemap style: HTTP ${r.status}`);
      return r.json();
    }).then((style) => stripBasemap(null, style)).catch((e) => { styleCache.delete(theme); throw e; }));
  }
  return styleCache.get(theme);
}

function firstSymbolId(map) {
  const layer = map.getStyle().layers.find((l) => l.type === "symbol");
  return layer ? layer.id : undefined;
}

// ------------------------------------------------------------------ init
function init(root, component) {
  const dom = buildDom(root);
  const st = { dom, component, map: null, data: null,
               geo: { units: [], districts: [] }, loadedStates: new Set(), loading: new Map(),
               selected: null, theme: null, search: null, activeResult: -1,
               sel: null, play: null, wind: null, windWanted: null, introSettled: false };
  st.ready = (async () => {
    try {
      await loadLib();
      const [states, outline, labels, meta] = await Promise.all([
        getJSON("india_states.geojson"), getJSON("india_outline.geojson"),
        getJSON("india_state_labels.geojson"), getJSON("boundary_source.json")]);
      st.geo.states = states; st.geo.outline = outline; st.geo.labels = labels; st.meta = meta;
      st.stateBox = {};
      states.features.forEach((f) => { st.stateBox[f.properties.state_key] = bboxOf(f.geometry); });
      await createMap(st);
      wireUi(st);
    } catch (err) {
      dom.canvas.innerHTML = `<div class="mo-fallback">The map could not load (${esc(err.message)}).
        The summary below still shows this forecast.</div>`;
      throw err;
    }
  })();
  return st;
}

async function createMap(st) {
  const d = st.data || {};
  const saved = (() => { try { return JSON.parse(ss.get(SS_CAMERA) || "null"); } catch (e) { return null; } })();
  const introDone = ss.get(SS_INTRO) === "1";
  const playIntro = !saved && !introDone && !reducedMotion();
  const start = saved || (playIntro ? INTRO_FROM : INDIA);
  st.theme = d.dark ? "dark" : "light";

  const style = await basemap(st.theme);
  const map = new maplibregl.Map({
    container: st.dom.canvas,
    style,
    center: start.center, zoom: start.zoom,
    minZoom: 1, maxZoom: 11,
    attributionControl: { compact: true, customAttribution: [
      "Boundaries: Survey of India layers", "Weather: <a href='https://open-meteo.com/'>Open-Meteo</a>"] },
    dragRotate: false, pitchWithRotate: false, touchPitch: false,
    keyboard: true,
  });
  st.map = map;
  map.touchZoomRotate.disableRotation();
  if (isPhone()) {
    // Start the attribution collapsed to its (i) button on phones, where it would
    // otherwise run under the legend chip.
    map.once("load", () => st.dom.canvas.querySelector(".maplibregl-ctrl-attrib")
      ?.classList.remove("maplibregl-compact-show"));
  }
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.on("style.load", () => {
    map.setProjection({ type: "globe" });
    addLayers(st);
    update(st);
  });
  map.on("styleimagemissing", (e) => {
    if (e.id === "mo-hatch") map.addImage("mo-hatch", hatchImage(tok(st, "hatch")), { pixelRatio: 2 });
  });
  const settle = () => { st.introSettled = true; ss.set(SS_INTRO, "1"); syncWind(st); };
  map.once("load", () => {
    // Fit India to this screen (a phone needs a lower zoom than a desktop).
    const fitted = map.cameraForBounds(INDIA_BOUNDS, { padding: isPhone() ? 12 : 40 });
    if (fitted && fitted.zoom) Object.assign(INDIA, { center: fitted.center.toArray ? fitted.center.toArray()
      : [fitted.center.lng, fitted.center.lat], zoom: Math.min(fitted.zoom, 4.2) });
    if (!saved && !playIntro) map.jumpTo(INDIA);
    if (playIntro) {
      // One animation only: the globe turns to India and settles. `essential: false`
      // lets MapLibre skip it if reduced motion is switched on mid-flight.
      map.flyTo({ ...INDIA, duration: 6000, curve: 1.3, essential: false });
      map.once("moveend", settle);
    } else settle();
  });
  map.on("movestart", () => windPause(st));
  map.on("moveend", () => {
    const c = map.getCenter();
    ss.set(SS_CAMERA, JSON.stringify({ center: [c.lng, c.lat], zoom: map.getZoom() }));
    ensureStates(st);
    renderLegend(st); renderHint(st);
    windResume(st);
  });
  map.on("zoom", () => renderHint(st));
  map.on("resize", () => windResize(st));
}

function tok(st, name) {
  return (st.data && st.data.tokens && st.data.tokens[name]) || "#888";
}

// ------------------------------------------------------------------ layers
function addLayers(st) {
  const map = st.map;
  if (map.getSource("mo-states")) return;
  const before = firstSymbolId(map);
  map.addSource("mo-states", { type: "geojson", data: st.geo.states, promoteId: "state_key" });
  map.addSource("mo-outline", { type: "geojson", data: st.geo.outline });
  map.addSource("mo-labels", { type: "geojson", data: st.geo.labels });
  map.addSource("mo-districts", { type: "geojson", promoteId: "did",
    data: { type: "FeatureCollection", features: st.geo.districts } });
  map.addSource("mo-units", { type: "geojson", promoteId: "unit_id",
    data: { type: "FeatureCollection", features: st.geo.units } });

  map.addLayer({ id: "mo-states-fill", type: "fill", source: "mo-states",
    paint: { "fill-opacity": 0.88, "fill-opacity-transition": { duration: 400 } } }, before);
  map.addLayer({ id: "mo-hatch", type: "fill", source: "mo-states", minzoom: 4.2,
    filter: ["all", ["!", ["get", "covered"]], ["!", ["get", "disputed"]]],
    paint: { "fill-pattern": "mo-hatch", "fill-opacity": 0.55 } }, before);
  map.addLayer({ id: "mo-districts-fill", type: "fill", source: "mo-districts",
    minzoom: RISK_ZOOM, maxzoom: UNIT_ZOOM, paint: { "fill-opacity": 0.9 } }, before);
  map.addLayer({ id: "mo-units-fill", type: "fill", source: "mo-units",
    minzoom: UNIT_ZOOM, paint: { "fill-opacity": 0.9 } }, before);
  map.addLayer({ id: "mo-replay-fill", type: "fill", source: "mo-units",
    layout: { visibility: "none" }, paint: { "fill-opacity": 0.92,
      "fill-color-transition": { duration: 300 } } }, before);
  map.addLayer({ id: "mo-districts-line", type: "line", source: "mo-districts",
    minzoom: RISK_ZOOM, maxzoom: UNIT_ZOOM, paint: { "line-width": 0.8 } }, before);
  map.addLayer({ id: "mo-units-line", type: "line", source: "mo-units", minzoom: UNIT_ZOOM,
    paint: { "line-width": 0.7 } }, before);
  map.addLayer({ id: "mo-replay-line", type: "line", source: "mo-units",
    layout: { visibility: "none" }, paint: { "line-width": 0.4 } }, before);
  // Two image layers crossfade between rain frames.
  const blank = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
  const corners = [[64, 39], [101, 39], [101, 4], [64, 4]];
  for (const id of ["a", "b"]) {
    map.addSource(`mo-rain-${id}`, { type: "image", url: blank, coordinates: corners });
    map.addLayer({ id: `mo-rain-${id}`, type: "raster", source: `mo-rain-${id}`,
      layout: { visibility: "none" },
      paint: { "raster-opacity": 0, "raster-resampling": "linear",
               "raster-opacity-transition": { duration: 400 }, "raster-fade-duration": 0 } }, before);
  }
  map.addLayer({ id: "mo-states-line", type: "line", source: "mo-states",
    paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.5, 7, 1.2] } }, before);
  map.addLayer({ id: "mo-covered-line", type: "line", source: "mo-states",
    filter: ["get", "covered"],
    paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 3, 1.6, 7, 2.6] } }, before);
  map.addLayer({ id: "mo-outline-line", type: "line", source: "mo-outline",
    paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 2, 1, 6, 1.8] } }, before);
  map.addLayer({ id: "mo-selected", type: "line", source: "mo-units",
    filter: ["==", ["get", "unit_id"], ""], paint: { "line-width": 3 } });
  map.addLayer({ id: "mo-labels", type: "symbol", source: "mo-labels", minzoom: 3.2, maxzoom: 7.5,
    layout: { "text-field": ["get", "state"], "text-font": ["Noto Sans Regular"],
              "text-size": ["interpolate", ["linear"], ["zoom"], 3.2, 10, 6, 13],
              "text-max-width": 7, "text-padding": 4 } });
  paint(st);
  if (st.play) restorePlayback(st);
}

function paint(st) {
  const map = st.map, d = st.data, sel = st.sel;
  if (!map || !d || !sel || !map.getLayer("mo-states-fill")) return;
  const w = sel.weather, ramp = d.ramps[w], b = BREAKS[w];
  const val = ["feature-state", "w"];
  map.setPaintProperty("mo-states-fill", "fill-color",
    ["case", ["==", ["typeof", val], "number"],
      ["step", val, ramp[0], b[0], ramp[1], b[1], ramp[2], b[2], ramp[3], b[3], ramp[4]],
      tok(st, "surface-2")]);
  const lvl = ["coalesce", ["feature-state", "lvl"], "none"];
  const riskFill = ["match", lvl, "green", tok(st, "risk-low"), "amber", tok(st, "risk-med"),
    "red", tok(st, "risk-high"), tok(st, "nofc")];
  const riskLine = ["match", lvl, "amber", tok(st, "risk-outline"), tok(st, "surface")];
  for (const id of ["mo-districts-fill", "mo-units-fill"]) map.setPaintProperty(id, "fill-color", riskFill);
  for (const id of ["mo-districts-line", "mo-units-line"]) map.setPaintProperty(id, "line-color", riskLine);
  map.setPaintProperty("mo-replay-fill", "fill-color",
    ["case", ["boolean", ["feature-state", "on"], false], MONSOON[st.theme], SAND[st.theme]]);
  map.setPaintProperty("mo-replay-line", "line-color", tok(st, "surface"));
  map.setPaintProperty("mo-states-line", "line-color", tok(st, "line-strong"));
  map.setPaintProperty("mo-covered-line", "line-color", tok(st, "accent"));
  map.setPaintProperty("mo-outline-line", "line-color", tok(st, "ink"));
  map.setPaintProperty("mo-selected", "line-color", tok(st, "ink"));
  map.setPaintProperty("mo-labels", "text-color", tok(st, "ink"));
  map.setPaintProperty("mo-labels", "text-halo-color", tok(st, "surface"));
  map.setPaintProperty("mo-labels", "text-halo-width", 1.4);
}

// ------------------------------------------------------------------ data -> map
function update(st) {
  const map = st.map, d = st.data;
  if (!map || !d || !map.isStyleLoaded() || !map.getSource("mo-states")) return;

  const want = d.dark ? "dark" : "light";
  if (want !== st.theme) {
    // New basemap; our layers are re-added and repainted on the next style.load.
    st.theme = want;
    if (map.hasImage("mo-hatch")) map.removeImage("mo-hatch");
    basemap(want).then((style) => { if (st.theme === want) map.setStyle(style, { diff: false }); });
    windColour(st);
    return;
  }
  if (map.hasImage("mo-hatch")) map.removeImage("mo-hatch");
  map.addImage("mo-hatch", hatchImage(tok(st, "hatch")), { pixelRatio: 2 });
  paint(st);
  paintWeather(st);
  paintRisk(st);
  if (st.selected) openUnit(st, st.selected, false);
  renderLegend(st); renderHint(st); renderNote(st);
}

function paintWeather(st) {
  const field = WEATHER_FIELD[st.sel.weather];
  for (const f of st.geo.states.features) {
    const key = f.properties.state_key, rec = st.data.weatherStates[key];
    st.map.setFeatureState({ source: "mo-states", id: key },
      { w: rec && rec[field] != null ? rec[field] : null });
  }
}

function riskFor(st) {
  // Python sends the probabilities for the week it last rendered; a week change waits
  // for that rerun, a hazard change is instant (all three hazards are in `risk`).
  return st.data.week === st.sel.week ? st.data.risk : {};
}

function paintRisk(st) {
  const map = st.map, risk = riskFor(st), hz = st.sel.hazard;
  const byDistrict = {};
  for (const f of st.geo.units) {
    const id = f.properties.unit_id, r = risk[id] && risk[id][hz];
    map.setFeatureState({ source: "mo-units", id }, { lvl: r ? r[1] : null, p: r ? r[0] : null });
    if (r && r[0] != null) {
      const did = f.properties.did;
      if (!byDistrict[did] || r[0] > byDistrict[did][0]) byDistrict[did] = r;
    }
  }
  for (const f of st.geo.districts) {
    const r = byDistrict[f.properties.did];
    map.setFeatureState({ source: "mo-districts", id: f.properties.did },
      { lvl: r ? r[1] : null, p: r ? r[0] : null });
  }
}

// Load a covered state's district + unit files once it is in view at risk zoom.
function ensureStates(st) {
  const map = st.map;
  if (!map || map.getZoom() < RISK_ZOOM - 0.6 || !st.meta) return;
  const b = map.getBounds();
  for (const info of Object.values(st.meta.covered)) {
    const key = info.key, box = st.stateBox[key];
    if (!box || st.loadedStates.has(key) || st.loading.has(key)) continue;
    const visible = !(box[2] < b.getWest() || box[0] > b.getEast() ||
                      box[3] < b.getSouth() || box[1] > b.getNorth());
    if (visible) loadState(st, key);
  }
}

function loadState(st, key) {
  if (st.loadedStates.has(key)) return Promise.resolve();
  if (st.loading.has(key)) return st.loading.get(key);
  const p = Promise.all([getJSON(`districts_${key}.geojson`), getJSON(`units_${key}.geojson`)])
    .then(([districts, units]) => {
      st.geo.districts.push(...districts.features);
      st.geo.units.push(...units.features);
      st.loadedStates.add(key);
      const map = st.map;
      map.getSource("mo-districts").setData({ type: "FeatureCollection", features: st.geo.districts });
      map.getSource("mo-units").setData({ type: "FeatureCollection", features: st.geo.units });
      // feature-state needs the features to exist; repaint once the source has them.
      map.once("idle", () => { paintRisk(st); if (st.play && st.play.kind === "advance") seekAdvance(st, st.play.index); });
    })
    .finally(() => st.loading.delete(key));
  st.loading.set(key, p);
  return p;
}

// ------------------------------------------------------------------ legend / notes
function sw(st, name) { return `<span class="sw" style="background:${tok(st, name)}"></span>`; }

function renderLegend(st) {
  const d = st.data, map = st.map, sel = st.sel;
  if (!d || !sel) return;
  let html = "";
  if (st.play && st.play.kind === "rain") {
    const c = RAIN_COLORS[st.theme];
    const daily = st.play.index >= st.play.hours;
    html = `<div class="mo-legend-block"><h4>${daily ? "Rain, daily average (mm per hour)" : "Rain (mm per hour)"}</h4>
      <div class="ramp">${c.map((x) => `<span style="background:rgba(${x[0]},${x[1]},${x[2]},${Math.min(1, x[3] + .1)})"></span>`).join("")}</div>
      <div class="ramp-labels">${RAIN_STOPS.slice(1).map((x, i) => `<span style="left:${(i + 1) * 100 / 6}%">${x}</span>`).join("")}</div>
      <p class="hint">Clear below 0.5 mm per hour.</p></div>`;
  } else if (st.play && st.play.kind === "advance") {
    html = `<div class="mo-legend-block"><h4>Monsoon onset, ${esc(st.play.year)}</h4>
      <div class="row"><span class="sw" style="background:${MONSOON[st.theme]}"></span>Onset has happened</div>
      <div class="row"><span class="sw" style="background:${SAND[st.theme]}"></span>Not yet, or no confirmed onset by 15 Aug</div>
      <p class="hint">Onset: 20 mm in 3 days with no 7-day dry spell in the next 30 days.</p></div>`;
  } else {
    const ramp = d.ramps[sel.weather], b = BREAKS[sel.weather];
    html = `<div class="mo-legend-block"><h4>${esc(WEATHER_TITLE[sel.weather])}</h4>
      <div class="ramp">${ramp.map((c) => `<span style="background:${c}"></span>`).join("")}</div>
      <div class="ramp-labels">${b.map((x, i) => `<span style="left:${(i + 1) * 20}%">${x}</span>`).join("")}</div></div>`;
    const zoomedIn = map && map.getZoom() >= RISK_ZOOM;
    html += `<div class="mo-legend-block"><h4>${esc(HAZARD_SHORT[sel.hazard])} · next ${esc(WEEKS[sel.week - 1][1])}</h4>
      <div class="row">${sw(st, "risk-high")}<span class="ico" style="color:var(--mo-risk-high-ink)">◆</span>High · over 60%</div>
      <div class="row">${sw(st, "risk-med")}<span class="ico" style="color:var(--mo-risk-med-ink)">▲</span>Medium · 30–60%</div>
      <div class="row">${sw(st, "risk-low")}<span class="ico" style="color:var(--mo-risk-low-ink)">●</span>Low · under 30%</div>
      <div class="row">${sw(st, "nofc")}<span class="ico" style="color:var(--mo-muted)">○</span>No forecast yet</div>
      <div class="row"><span class="sw" style="background:repeating-linear-gradient(135deg,${tok(st, "hatch")} 0 1.5px,transparent 1.5px 5px)"></span><span class="ico"></span>Forecasts coming soon</div>
      ${zoomedIn && map.getZoom() < UNIT_ZOOM
        ? `<p class="hint">Districts show their highest sub-district risk.</p>` : ""}</div>`;
  }
  st.dom.legendBody.innerHTML = html;
  // The details panel stops above the legend instead of covering it.
  requestAnimationFrame(() => st.dom.wrap.style.setProperty("--mo-legend-h", `${st.dom.legend.offsetHeight}px`));
}

function renderHint(st) {
  const map = st.map;
  if (!map) return;
  const show = !st.play && map.getZoom() < RISK_ZOOM - 0.4;
  st.dom.hint.hidden = !show;
  if (show) st.dom.hint.textContent = "Select a state outlined in green, or zoom in, to see monsoon risk";
}

function renderNote(st) {
  const official = st.meta && st.meta.official;
  st.dom.note.hidden = !!official;
  if (!official) st.dom.note.textContent = "Boundaries indicative, not official";
}

// ------------------------------------------------------------------ controls panel
function renderControls(st) {
  const c = st.dom.controls, sel = st.sel, d = st.data;
  const slot = (n) => c.querySelector(`[data-slot="${n}"]`);
  if (!slot("weather").firstChild) {
    slot("weather").innerHTML = segmented("mo-weather", "Weather layer", WEATHER_OPTS, sel.weather);
    slot("hazard").innerHTML = segmented("mo-hazard", "Hazard", HAZARDS.map(([k]) => [k, HAZARD_SHORT[k]]), sel.hazard);
    slot("week").innerHTML = segmented("mo-week", "Looking ahead", WEEKS, sel.week);
    c.addEventListener("change", (e) => {
      const t = e.target;
      if (t.name === "mo-weather") setSel(st, "weather", t.value);
      else if (t.name === "mo-hazard") setSel(st, "hazard", t.value);
      else if (t.name === "mo-week") setSel(st, "week", Number(t.value));
    });
  } else {
    for (const [name, v] of [["mo-weather", sel.weather], ["mo-hazard", sel.hazard], ["mo-week", sel.week]]) {
      const input = c.querySelector(`input[name="${name}"][value="${v}"]`);
      if (input && !input.checked) input.checked = true;
    }
  }
  c.querySelector(".mo-window").textContent = d.week === sel.week ? d.windowLabel : "Updating…";

  // Animations: options whose data file is missing are hidden, never an error.
  const a = d.anim || {};
  const wind = c.querySelector('[data-anim="wind"]'), rain = c.querySelector('[data-anim="rain"]'),
        adv = c.querySelector('[data-anim="advance"]');
  wind.hidden = !a.wind; rain.hidden = !a.rain; adv.hidden = !a.advance;
  c.querySelector('[data-slot="anim-group"]').hidden = !a.wind && !a.rain && !a.advance;
  c.querySelector(".mo-wind-on").checked = !!st.windWanted;
  if (a.advance && !st.dom.season.options.length) {
    st.dom.season.innerHTML = (a.seasons || []).map((y) => `<option value="${y}">${y}</option>`).join("");
    st.dom.season.value = String((a.seasons || []).slice(-1)[0] || "");
  }
}

function setSel(st, key, value) {
  st.sel = { ...st.sel, [key]: value };
  try { st.component.setStateValue(key, value); } catch (e) { /* not mounted with state */ }
  paint(st); paintWeather(st); paintRisk(st); renderLegend(st);
  st.dom.controls.querySelector(".mo-window").textContent =
    st.data.week === st.sel.week ? st.data.windowLabel : "Updating…";
  if (st.selected) openUnit(st, st.selected, false);
}

// ------------------------------------------------------------------ details panel
function langsFor(st, stateKey) {
  return (st.data.langs && st.data.langs[stateKey]) || ["hi", "en"];
}

function openUnit(st, unitId, focus = true) {
  const d = st.data, f = st.geo.units.find((u) => u.properties.unit_id === unitId);
  if (!f) return;
  st.selected = unitId;
  st.map.setFilter("mo-selected", ["==", ["get", "unit_id"], unitId]);
  const p = f.properties, risk = riskFor(st)[unitId] || {};
  const rows = HAZARDS.map(([k, label]) => {
    const r = risk[k];
    return `<tr><td>${label}</td><td class="pct">${r ? pct(r[0]) : "—"}</td>
      <td class="lvl">${levelHtml(r && r[1])}</td></tr>`;
  }).join("");
  const adv = d.adv[unitId];
  let advice;
  if (!adv) {
    advice = `<p class="note">No approved advisory for this area in this forecast.</p>`;
  } else if (adv.edited) {
    advice = `<div class="tabs" role="tablist"><button role="tab" aria-selected="true" type="button">English</button></div>
      <p class="advice" lang="en">${esc(adv.en)}</p>
      <p class="note">Edited and approved by an agriculture officer.</p>`;
  } else {
    const wanted = langsFor(st, p.state_key), langs = wanted.filter((l) => adv[l]);
    const missing = wanted.filter((l) => !adv[l]).map((l) => LANG_LABEL[l]);
    advice = `<div class="tabs" role="tablist" aria-label="Advisory language">${langs.map((l, i) =>
      `<button role="tab" type="button" aria-controls="mo-adv" data-lang="${l}"
        aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}">${LANG_LABEL[l]}</button>`).join("")}</div>
      <p class="advice" id="mo-adv" role="tabpanel" lang="${langs[0]}">${esc(adv[langs[0]])}</p>
      ${missing.length ? `<p class="note">${esc(missing.join(" and "))} text is not available yet for this advisory.</p>` : ""}`;
  }
  const noForecast = !Object.keys(risk).length;
  st.dom.panel.innerHTML = `
    <button class="close" type="button" aria-label="Close details">×</button>
    <h3>${esc(p.name_ok ? p.unit_name : "Unnamed area")}</h3>
    <p class="sub">${esc(p.district)} district, ${esc(d.stateNames[p.state_key] || p.state)}</p>
    ${noForecast
      ? `<p class="note">No forecast has been issued for this sub-district yet. Forecasts are
         being rolled out district by district.</p>`
      : `<p class="week">Next ${esc(WEEKS[st.sel.week - 1][1])} · ${esc(d.windowLabel)}</p><table>${rows}</table>`}
    ${noForecast ? "" : advice}
    <p class="foot">Forecast for ${esc(d.issuedFor)} · prepared ${esc(d.updated)}</p>`;
  st.dom.panel.hidden = false;
  bindPanel(st, adv);
  if (focus) st.dom.panel.focus({ preventScroll: true });
}

function openState(st, key, name) {
  const d = st.data, rec = d.weatherStates[key];
  const covered = !!Object.values(st.meta.covered).find((c) => c.key === key);
  const w = (f, unit) => (rec && rec[f] != null ? `${rec[f]} ${unit}` : "—");
  st.dom.panel.innerHTML = `
    <button class="close" type="button" aria-label="Close details">×</button>
    <h3>${esc(name)}</h3>
    <table>
      <tr><td>Rain, next 24 hours</td><td class="pct">${w("r24", "mm")}</td></tr>
      <tr><td>Rain, next 7 days</td><td class="pct">${w("r7", "mm")}</td></tr>
      <tr><td>Temperature now</td><td class="pct">${w("t", "°C")}</td></tr>
    </table>
    <p class="note">${covered ? "Zoom in to see monsoon risk by district and sub-district."
                               : "Monsoon forecasts coming soon for this state."}</p>
    <p class="foot">Average of ${rec ? rec.n : 0} districts · weather updated ${esc(d.weatherUpdated)}</p>`;
  st.dom.panel.hidden = false;
  bindPanel(st, null);
  st.dom.panel.focus({ preventScroll: true });
}

function closePanel(st) {
  st.dom.panel.hidden = true;
  st.selected = null;
  if (st.map.getLayer("mo-selected")) st.map.setFilter("mo-selected", ["==", ["get", "unit_id"], ""]);
}

function bindPanel(st, adv) {
  const panel = st.dom.panel;
  panel.querySelector(".close").onclick = () => { closePanel(st); st.map.getCanvas().focus(); };
  const tabs = [...panel.querySelectorAll('[role="tab"][data-lang]')];
  const select = (i) => {
    tabs.forEach((t, j) => { t.setAttribute("aria-selected", String(i === j)); t.tabIndex = i === j ? 0 : -1; });
    const lang = tabs[i].dataset.lang, box = panel.querySelector("#mo-adv");
    box.lang = lang; box.textContent = adv[lang];
    tabs[i].focus();
  };
  tabs.forEach((t, i) => {
    t.onclick = () => select(i);
    t.onkeydown = (e) => {
      if (e.key === "ArrowRight") select((i + 1) % tabs.length);
      if (e.key === "ArrowLeft") select((i - 1 + tabs.length) % tabs.length);
    };
  });
}

// ------------------------------------------------------------------ interaction
function wireUi(st) {
  const map = st.map, dom = st.dom;
  const hoverLayers = ["mo-units-fill", "mo-districts-fill", "mo-states-fill"];
  const live = () => hoverLayers.filter((l) => map.getLayer(l));

  map.on("mousemove", (e) => {
    if (st.play) { dom.tip.hidden = true; return; }
    const feats = map.queryRenderedFeatures(e.point, { layers: live() });
    if (!feats.length) { dom.tip.hidden = true; map.getCanvas().style.cursor = ""; return; }
    map.getCanvas().style.cursor = "pointer";
    dom.tip.innerHTML = tipHtml(st, feats[0]);
    dom.tip.hidden = false;
    const x = Math.min(e.point.x + 14, dom.wrap.clientWidth - 270);
    dom.tip.style.left = `${x}px`; dom.tip.style.top = `${e.point.y + 14}px`;
  });
  map.on("mouseout", () => { dom.tip.hidden = true; });

  map.on("click", (e) => {
    if (st.play) return;
    const feats = map.queryRenderedFeatures(e.point, { layers: live() });
    if (!feats.length) { closePanel(st); return; }
    const f = feats[0], layer = f.layer.id, p = f.properties;
    if (layer === "mo-units-fill") openUnit(st, p.unit_id);
    else if (layer === "mo-districts-fill") {
      const src = st.geo.districts.find((x) => x.properties.did === p.did);
      if (src) map.fitBounds(bboxOf(src.geometry), { padding: 60, maxZoom: 9, essential: false });
    } else if (layer === "mo-states-fill") {
      if (p.covered && map.getZoom() < RISK_ZOOM) {
        map.fitBounds(st.stateBox[p.state_key], { padding: 40, essential: false });
      } else if (!p.disputed) openState(st, p.state_key, p.state);
    }
  });

  dom.root.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !dom.panel.hidden) { closePanel(st); map.getCanvas().focus(); }
  });
  dom.legendToggle.onclick = () => {
    const collapsed = dom.legend.classList.toggle("collapsed");
    dom.legendToggle.setAttribute("aria-expanded", String(!collapsed));
  };
  if (isPhone()) {
    dom.legend.classList.add("collapsed");
    dom.legendToggle.setAttribute("aria-expanded", "false");
  }
  const collapse = dom.controls.querySelector(".mo-collapse");
  const setCollapsed = (v) => {
    dom.controls.classList.toggle("collapsed", v);
    collapse.setAttribute("aria-expanded", String(!v));
    collapse.textContent = v ? "Show" : "Hide";
    ss.set(SS_PANEL, v ? "1" : "0");
  };
  collapse.onclick = () => setCollapsed(!dom.controls.classList.contains("collapsed"));
  if (ss.get(SS_PANEL) === "1" && !isPhone()) setCollapsed(true);

  dom.controls.querySelector(".mo-wind-on").addEventListener("change", (e) => {
    st.windWanted = e.target.checked;
    ss.set("mo-wind-v1", st.windWanted ? "1" : "0");
    syncWind(st);
  });
  dom.controls.querySelector('[data-anim="rain"]').onclick = () => startRain(st);
  dom.controls.querySelector('[data-anim="advance"] button').onclick = () => startAdvance(st, Number(dom.season.value));
  wireTimeline(st);
  wireSearch(st);
  document.addEventListener("visibilitychange", () => (document.hidden ? windPause(st) : windResume(st)));
}

function tipHtml(st, f) {
  const d = st.data, p = f.properties, layer = f.layer.id;
  if (layer === "mo-units-fill" || layer === "mo-districts-fill") {
    const r = f.state && f.state.p != null ? [f.state.p, f.state.lvl] : null;
    const name = layer === "mo-units-fill" ? (p.name_ok ? p.unit_name : "Unnamed area") : `${p.district} district`;
    return `<b>${esc(name)}</b><br>${r ? `${esc(HAZARD_SHORT[st.sel.hazard])} ${pct(r[0])} · ${levelHtml(r[1])}`
                                       : "No forecast yet"}`;
  }
  const rec = d.weatherStates[p.state_key], field = WEATHER_FIELD[st.sel.weather];
  const val = rec && rec[field] != null ? `${rec[field]} ${WEATHER_UNIT[st.sel.weather]}` : "—";
  const extra = p.disputed ? "" : p.covered
    ? (st.map.getZoom() < RISK_ZOOM ? "<br>Select to see monsoon risk" : "")
    : "<br>Monsoon forecasts coming soon";
  return `<b>${esc(p.state)}</b><br>${esc(WEATHER_TITLE[st.sel.weather].replace(/ \(.*\)$/, ""))}: ${esc(val)}${extra}`;
}

// ------------------------------------------------------------------ search
function norm(s) { return String(s).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, " ").trim(); }

function score(query, name) {
  const n = norm(name);
  if (n === query) return 100;
  if (n.startsWith(query)) return 90 - (n.length - query.length) * 0.1;
  if (n.includes(` ${query}`)) return 80;
  if (n.includes(query)) return 70;
  // subsequence: every query letter appears in order ("gorkpr" -> Gorakhpur)
  let i = 0;
  for (const ch of n) if (ch === query[i]) i++;
  return i === query.length ? 50 - (n.length - query.length) * 0.2 : 0;
}

function wireSearch(st) {
  const { input, results } = st.dom;
  const ensureIndex = () => st.search || (st.search = getJSON("search_index.json").catch(() => []));
  const render = async () => {
    const q = norm(input.value);
    if (!q) { results.hidden = true; input.setAttribute("aria-expanded", "false"); return; }
    const index = await ensureIndex();
    const hits = index.map((e) => [score(q, e.n), e]).filter((x) => x[0] > 0)
      .sort((a, b) => b[0] - a[0] || a[1].n.length - b[1].n.length).slice(0, 8).map((x) => x[1]);
    st.hits = hits; st.activeResult = hits.length ? 0 : -1;
    results.innerHTML = hits.length ? hits.map((h, i) => `<li role="option" id="mo-r${i}"
      aria-selected="${i === 0}" data-i="${i}">${esc(h.n)}<small>${h.k === "subdistrict"
        ? `Sub-district · ${esc(h.d)}, ${esc(h.s)}` : `District · ${esc(h.s)}`}</small></li>`).join("")
      : `<li class="mo-empty" role="option" aria-disabled="true">No match in India's districts</li>`;
    results.hidden = false;
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-activedescendant", hits.length ? "mo-r0" : "");
  };
  const move = (delta) => {
    if (!st.hits || !st.hits.length) return;
    st.activeResult = (st.activeResult + delta + st.hits.length) % st.hits.length;
    results.querySelectorAll("li").forEach((li, i) => li.setAttribute("aria-selected", String(i === st.activeResult)));
    input.setAttribute("aria-activedescendant", `mo-r${st.activeResult}`);
  };
  const choose = async (i) => {
    const h = st.hits && st.hits[i];
    if (!h) return;
    results.hidden = true; input.setAttribute("aria-expanded", "false");
    input.value = h.n;
    if (st.play) stopPlayback(st);
    const sub = h.k === "subdistrict";
    st.map.fitBounds(h.b, { padding: 60, maxZoom: sub ? 9 : 7.6, essential: false });
    if (h.st) {
      await loadState(st, h.st);
      if (sub && h.id) st.map.once("idle", () => openUnit(st, h.id, false));
    }
  };
  input.addEventListener("focus", ensureIndex);
  input.addEventListener("input", render);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "Enter") { e.preventDefault(); choose(st.activeResult); }
    else if (e.key === "Escape") { results.hidden = true; input.setAttribute("aria-expanded", "false"); }
  });
  results.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); choose(Number(li.dataset.i)); }
  });
  input.addEventListener("blur", () => setTimeout(() => { results.hidden = true; }, 150));
}

// ================================================================== WIND FLOW
// Particles live in lon/lat, are advected by the 850 hPa field, and are drawn on a
// canvas over the map with map.project() every frame, so they follow pan, zoom and the
// globe. They only run from the India zoom level with India in view (the whole view
// faces the viewer there), pause while the map moves or the tab is hidden, and shed
// particles when frames get slow.
function windField(st, w) {
  const n = w.nx * w.ny, u = new Float32Array(n), v = new Float32Array(n);
  let max = 0;
  for (let i = 0; i < n; i++) {
    u[i] = w.u[i] == null ? NaN : w.u[i] / w.scale;
    v[i] = w.v[i] == null ? NaN : w.v[i] / w.scale;
    const s = Math.hypot(u[i], v[i]);
    if (s > max) max = s;
  }
  return { ...w, uu: u, vv: v, max };
}

function sampleWind(f, lon, lat) {
  const x = (lon - f.lon0) / f.step, y = (lat - f.lat0) / f.step;
  if (x < 0 || y < 0 || x > f.nx - 1 || y > f.ny - 1) return null;
  const x0 = Math.floor(x), y0 = Math.floor(y), x1 = Math.min(x0 + 1, f.nx - 1), y1 = Math.min(y0 + 1, f.ny - 1);
  const fx = x - x0, fy = y - y0;
  const at = (a, xi, yi) => a[yi * f.nx + xi];
  const lerp = (a) => {
    const v00 = at(a, x0, y0), v10 = at(a, x1, y0), v01 = at(a, x0, y1), v11 = at(a, x1, y1);
    return (v00 * (1 - fx) + v10 * fx) * (1 - fy) + (v01 * (1 - fx) + v11 * fx) * fy;
  };
  const u = lerp(f.uu), v = lerp(f.vv);
  return Number.isFinite(u) && Number.isFinite(v) ? [u, v] : null;
}

async function syncWind(st) {
  const a = st.data && st.data.anim;
  const on = !!(a && a.wind && st.windWanted && st.introSettled && !st.play);
  if (!on) { windStop(st); return; }
  if (!st.wind) {
    try {
      const raw = await getJSON(a.wind);
      st.wind = { field: windField(st, raw), particles: [], running: false, raf: 0,
                  frameTimes: [], cap: isPhone() ? 800 : 3000 };
      windColour(st);
    } catch (e) { st.data.anim.wind = null; renderControls(st); return; }
  }
  windResume(st);
}

function windColour(st) {
  if (!st.wind) return;
  st.wind.rgb = st.theme === "dark" ? "230,237,241" : "46,94,126";
  // Opacity per speed band: light mode needs less, the slate lines sit on pale land.
  st.wind.alpha = st.theme === "dark" ? [0.22, 0.34, 0.46, 0.6] : [0.16, 0.26, 0.38, 0.52];
}

function windResize(st) {
  const c = st.dom.wind, r = st.dom.wrap.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
  c.width = Math.round(r.width * dpr); c.height = Math.round(r.height * dpr);
  c.style.width = `${r.width}px`; c.style.height = `${r.height}px`;
  if (st.wind) st.wind.dpr = dpr;
}

function windInView(st) {
  const m = st.map, c = m.getCenter();
  return m.getZoom() >= WIND_MIN_ZOOM && c.lng > 55 && c.lng < 110 && c.lat > -2 && c.lat < 45;
}

function spawn(st, p) {
  const f = st.wind.field, b = st.map.getBounds();
  const w = Math.max(b.getWest(), f.lon0), e = Math.min(b.getEast(), f.lon0 + (f.nx - 1) * f.step);
  const s = Math.max(b.getSouth(), f.lat0), n = Math.min(b.getNorth(), f.lat0 + (f.ny - 1) * f.step);
  p.lon = w + Math.random() * Math.max(0.1, e - w);
  p.lat = s + Math.random() * Math.max(0.1, n - s);
  p.age = Math.floor(Math.random() * 80);
  p.life = 60 + Math.floor(Math.random() * 60);
  return p;
}

function windResume(st) {
  const w = st.wind;
  if (!w || w.running || !st.windWanted || st.play || document.hidden || !st.introSettled) return;
  if (!windInView(st)) { st.dom.wind.classList.remove("on"); return; }
  windResize(st);
  const target = w.cap;
  while (w.particles.length < target) w.particles.push(spawn(st, {}));
  w.particles.forEach((p) => spawn(st, p));
  const ctx = st.dom.wind.getContext("2d");
  ctx.clearRect(0, 0, st.dom.wind.width, st.dom.wind.height);
  w.running = true; w.last = performance.now(); w.frameTimes = [];
  st.dom.wind.classList.add("on");        // CSS fades the canvas in
  const step = (now) => {
    if (!w.running) return;
    const dt = Math.min(50, now - w.last); w.last = now;
    windFrame(st, ctx, dt);
    // Frame budget: shed 15% of particles when the average frame is slower than ~45 fps.
    w.frameTimes.push(dt);
    if (w.frameTimes.length >= 40) {
      const avg = w.frameTimes.reduce((a, b) => a + b, 0) / w.frameTimes.length;
      if (avg > 22 && w.particles.length > 200) w.particles.length = Math.floor(w.particles.length * 0.85);
      w.frameTimes = [];
    }
    w.raf = requestAnimationFrame(step);
  };
  w.raf = requestAnimationFrame(step);
}

function windFrame(st, ctx, dt) {
  const w = st.wind, f = w.field, map = st.map, dpr = w.dpr || 1;
  const cw = st.dom.wind.width, ch = st.dom.wind.height;
  // Fade the previous frame so trails decay; faster wind leaves longer trails because
  // each segment it draws is longer.
  ctx.globalCompositeOperation = "destination-in";
  ctx.fillStyle = "rgba(0,0,0,0.9)";
  ctx.fillRect(0, 0, cw, ch);
  ctx.globalCompositeOperation = "source-over";
  ctx.lineCap = "round";
  // Speed is set in screen pixels so the flow reads the same at every zoom:
  // ~0.12 px per frame for each m/s, i.e. a 20 m/s jet moves ~2.5 px a frame.
  const pxPerDeg = (512 * Math.pow(2, map.getZoom())) / 360;
  const k = (0.12 * (dt / 16.7)) / pxPerDeg;
  const buckets = [[], [], [], []];
  for (const p of w.particles) {
    const uv = sampleWind(f, p.lon, p.lat);
    if (!uv || ++p.age > p.life) { spawn(st, p); continue; }
    const a = map.project([p.lon, p.lat]);
    const cos = Math.max(0.2, Math.cos(p.lat * Math.PI / 180));
    p.lon += uv[0] * k / cos; p.lat += uv[1] * k;
    const b = map.project([p.lon, p.lat]);
    if (a.x < 0 || a.y < 0 || a.x * dpr > cw || a.y * dpr > ch) { spawn(st, p); continue; }
    // Thin the particles out over the last two grid cells so the field fades at the
    // edge of the data instead of ending in a hard straight line.
    const edge = Math.min((p.lon - f.lon0) / f.step, (p.lat - f.lat0) / f.step,
      f.nx - 1 - (p.lon - f.lon0) / f.step, f.ny - 1 - (p.lat - f.lat0) / f.step);
    if (edge < 2 && Math.random() * 2 > edge) continue;
    const speed = Math.hypot(uv[0], uv[1]);
    const bi = Math.min(3, Math.floor(speed / Math.max(4, f.max / 4)));
    buckets[bi].push(a.x * dpr, a.y * dpr, b.x * dpr, b.y * dpr);
  }
  for (let i = 0; i < 4; i++) {
    const seg = buckets[i];
    if (!seg.length) continue;
    ctx.strokeStyle = `rgba(${w.rgb},${w.alpha[i]})`;
    ctx.lineWidth = (0.7 + i * 0.2) * dpr;
    ctx.beginPath();
    for (let j = 0; j < seg.length; j += 4) { ctx.moveTo(seg[j], seg[j + 1]); ctx.lineTo(seg[j + 2], seg[j + 3]); }
    ctx.stroke();
  }
}

function windPause(st) {
  const w = st.wind;
  if (!w) return;
  w.running = false;
  cancelAnimationFrame(w.raf);
  st.dom.wind.classList.remove("on");
  const ctx = st.dom.wind.getContext("2d");
  setTimeout(() => { if (!w.running) ctx.clearRect(0, 0, st.dom.wind.width, st.dom.wind.height); }, 250);
}

function windStop(st) { windPause(st); }

// ================================================================== PLAYBACK
// One playback at a time; starting one pauses the wind, closing it resumes the wind.
function wireTimeline(st) {
  const dom = st.dom;
  dom.play.onclick = () => (st.play && st.play.playing ? pausePlayback(st) : resumePlayback(st));
  dom.scrub.addEventListener("input", () => {
    if (!st.play) return;
    pausePlayback(st);
    seek(st, Number(dom.scrub.value));
  });
  dom.timeline.querySelectorAll('input[name="mo-speed"]').forEach((r) =>
    r.addEventListener("change", () => { if (st.play) st.play.speed = Number(r.value); }));
  dom.tlClose.onclick = () => stopPlayback(st);
  dom.timeline.addEventListener("keydown", (e) => {
    if (e.key === "Escape") stopPlayback(st);
    if (e.key === " " && e.target === dom.timeline) { e.preventDefault(); dom.play.click(); }
  });
}

function beginPlayback(st, play) {
  if (st.play) stopPlayback(st, false);
  st.play = { ...play, index: 0, playing: false, speed: Number(
    st.dom.timeline.querySelector('input[name="mo-speed"]:checked').value) };
  windPause(st);
  closePanel(st);
  st.dom.timeline.hidden = false;
  st.dom.scrub.max = String(play.frames - 1);
  st.dom.tlCaption.textContent = play.caption;
  st.dom.root.classList.add("mo-playing");
  renderHint(st); renderLegend(st);
  st.dom.play.focus({ preventScroll: true });
  // On a phone the Play buttons sit below the map; bring the map and its timeline up.
  if (isPhone()) st.dom.wrap.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
}

function resumePlayback(st) {
  const p = st.play;
  if (!p) return;
  if (p.index >= p.frames - 1) seek(st, 0);
  p.playing = true;
  st.dom.play.textContent = "❚❚"; st.dom.play.setAttribute("aria-label", "Pause");
  const tick = (now) => {
    if (!st.play || !p.playing) return;
    if (!p.last) p.last = now;
    if (now - p.last >= p.frameMs / p.speed) {
      p.last = now;
      if (p.index >= p.frames - 1) { pausePlayback(st); return; }   // hold the final frame
      seek(st, p.index + 1);
    }
    p.raf = requestAnimationFrame(tick);
  };
  p.last = 0;
  p.raf = requestAnimationFrame(tick);
}

function pausePlayback(st) {
  const p = st.play;
  if (!p) return;
  p.playing = false;
  cancelAnimationFrame(p.raf);
  st.dom.play.textContent = "▶"; st.dom.play.setAttribute("aria-label", "Play");
}

function seek(st, i) {
  const p = st.play;
  p.index = Math.max(0, Math.min(p.frames - 1, i));
  st.dom.scrub.value = String(p.index);
  if (p.kind === "rain") seekRain(st, p.index);
  else seekAdvance(st, p.index);
}

function stopPlayback(st, resumeWind = true) {
  const p = st.play, map = st.map;
  if (!p) return;
  pausePlayback(st);
  st.play = null;
  for (const id of ["mo-rain-a", "mo-rain-b"]) {
    if (map.getLayer(id)) { map.setPaintProperty(id, "raster-opacity", 0); map.setLayoutProperty(id, "visibility", "none"); }
  }
  for (const id of ["mo-replay-fill", "mo-replay-line"]) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "none");
  for (const id of ["mo-districts-fill", "mo-units-fill", "mo-districts-line", "mo-units-line", "mo-hatch"])
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", "visible");
  if (map.getLayer("mo-states-fill")) map.setPaintProperty("mo-states-fill", "fill-opacity", 0.88);
  st.dom.timeline.hidden = true;
  st.dom.root.classList.remove("mo-playing");
  renderLegend(st); renderHint(st);
  if (resumeWind) syncWind(st);
}

function restorePlayback(st) {
  // After a basemap switch (dark mode), put the active playback's layers back.
  const p = st.play;
  if (p.kind === "rain") { showRainLayers(st); p.front = null; seekRain(st, p.index); }
  else { showAdvanceLayers(st); seekAdvance(st, p.index); }
}

// ------------------------------------------------------------------ rain playback
function upsampledFrame(st, values, scale, perDay) {
  // Bilinear interpolation of VALUES (not colours) onto a finer canvas whose rows are
  // spaced evenly in Web Mercator, so the image lines up with the map at every latitude.
  const g = st.rainGrid, K = 6;
  const W = (g.nx - 1) * K + 1, H = (g.ny - 1) * K + 1;
  const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
  const ctx = cv.getContext("2d"), img = ctx.createImageData(W, H);
  const merc = (lat) => Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
  const latTop = g.lat0 + (g.ny - 1) * g.step, yTop = merc(latTop), yBot = merc(g.lat0);
  const colors = RAIN_COLORS[st.theme];
  for (let r = 0; r < H; r++) {
    const my = yTop - (r / (H - 1)) * (yTop - yBot);
    const lat = (2 * Math.atan(Math.exp(my)) - Math.PI / 2) * 180 / Math.PI;
    const gy = (lat - g.lat0) / g.step, y0 = Math.min(Math.floor(gy), g.ny - 2), fy = gy - y0;
    for (let c = 0; c < W; c++) {
      const gx = c / K, x0 = Math.min(Math.floor(gx), g.nx - 2), fx = gx - x0;
      const at = (xi, yi) => { const v = values[yi * g.nx + xi]; return v == null ? 0 : v / scale; };
      let v = (at(x0, y0) * (1 - fx) + at(x0 + 1, y0) * fx) * (1 - fy) +
              (at(x0, y0 + 1) * (1 - fx) + at(x0 + 1, y0 + 1) * fx) * fy;
      if (perDay) v /= 24;                     // daily totals shown as mm per hour
      const o = (r * W + c) * 4;
      if (v < RAIN_STOPS[0]) { img.data[o + 3] = 0; continue; }
      let i = 0;
      while (i < RAIN_STOPS.length - 1 && v >= RAIN_STOPS[i + 1]) i++;
      const t = i < RAIN_STOPS.length - 1 ? (v - RAIN_STOPS[i]) / (RAIN_STOPS[i + 1] - RAIN_STOPS[i]) : 0;
      const a = colors[i], b = colors[Math.min(i + 1, colors.length - 1)];
      img.data[o] = a[0] + (b[0] - a[0]) * t; img.data[o + 1] = a[1] + (b[1] - a[1]) * t;
      img.data[o + 2] = a[2] + (b[2] - a[2]) * t; img.data[o + 3] = 255 * (a[3] + (b[3] - a[3]) * t);
    }
  }
  ctx.putImageData(img, 0, 0);
  return cv.toDataURL();
}

function showRainLayers(st) {
  const map = st.map;
  const g = st.rainGrid, half = g.step / 2;
  const w = g.lon0 - half, e = g.lon0 + (g.nx - 1) * g.step + half;
  const s = g.lat0, n = g.lat0 + (g.ny - 1) * g.step;
  st.rainCorners = [[g.lon0, n], [g.lon0 + (g.nx - 1) * g.step, n], [g.lon0 + (g.nx - 1) * g.step, s], [g.lon0, s]];
  void w; void e;
  for (const id of ["mo-rain-a", "mo-rain-b"]) map.setLayoutProperty(id, "visibility", "visible");
  // Calm the choropleth under the rain so the two do not compete.
  map.setPaintProperty("mo-states-fill", "fill-opacity", 0.18);
  for (const id of ["mo-districts-fill", "mo-units-fill", "mo-districts-line", "mo-units-line", "mo-hatch"])
    map.setLayoutProperty(id, "visibility", "none");
}

async function startRain(st) {
  const a = st.data.anim;
  let g;
  try { g = await getJSON(a.rain); } catch (e) { a.rain = null; renderControls(st); return; }
  st.rainGrid = g;
  st.rainFrames = new Map();
  const t0 = Date.parse(`${g.t0}:00Z`);
  const frames = g.hours + g.days.length;
  beginPlayback(st, {
    kind: "rain", frames, hours: g.hours, frameMs: 600, t0,
    caption: "Hourly rain forecast for three days, then daily averages for days 4 to 7 (Open-Meteo).",
  });
  showRainLayers(st);
  st.play.front = null;
  seekRain(st, 0);
  resumePlayback(st);
}

function seekRain(st, i) {
  const g = st.rainGrid, p = st.play, map = st.map;
  if (!st.rainFrames.has(i)) {
    const n = g.nx * g.ny;
    const daily = i >= g.hours;
    const values = daily ? g.days[i - g.hours].v : g.h.slice(i * n, (i + 1) * n);
    st.rainFrames.set(i, upsampledFrame(st, values, g.scale, daily));
    if (st.rainFrames.size > 24) st.rainFrames.delete(st.rainFrames.keys().next().value);
  }
  // Crossfade: draw the new frame into the hidden layer, then swap opacities.
  const back = p.front === "a" ? "b" : "a";
  map.getSource(`mo-rain-${back}`).updateImage({ url: st.rainFrames.get(i), coordinates: st.rainCorners });
  const ms = reducedMotion() ? 0 : Math.round(400 / (p.speed || 1));
  map.setPaintProperty(`mo-rain-${back}`, "raster-opacity-transition", { duration: ms });
  map.setPaintProperty(`mo-rain-${p.front || "b"}`, "raster-opacity-transition", { duration: ms });
  map.setPaintProperty(`mo-rain-${back}`, "raster-opacity", 0.9);
  if (p.front) map.setPaintProperty(`mo-rain-${p.front}`, "raster-opacity", 0);
  p.front = back;
  if (i < g.hours) {
    const t = new Date(p.t0 + i * 3600e3);
    st.dom.tlLabel.textContent = fmtHour(t);
    st.dom.tlCount.textContent = `Hour ${i + 1} of ${g.hours}`;
  } else {
    const day = g.days[i - g.hours];
    st.dom.tlLabel.textContent = `${fmtDay(new Date(`${day.date}T06:30:00Z`))}, daily`;
    st.dom.tlCount.textContent = `Day ${i - g.hours + 4} of 7`;
  }
  const before = st.dom.legend.dataset.daily, now = String(i >= g.hours);
  if (before !== now) { st.dom.legend.dataset.daily = now; renderLegend(st); }
}

// ------------------------------------------------------------------ monsoon advance
function showAdvanceLayers(st) {
  const map = st.map;
  for (const id of ["mo-replay-fill", "mo-replay-line"]) map.setLayoutProperty(id, "visibility", "visible");
  for (const id of ["mo-districts-fill", "mo-units-fill", "mo-districts-line", "mo-units-line", "mo-hatch"])
    map.setLayoutProperty(id, "visibility", "none");
  map.setPaintProperty("mo-states-fill", "fill-opacity", 0.25);
}

async function startAdvance(st, year) {
  const a = st.data.anim;
  let rep;
  try { rep = await getJSON(a.advance); } catch (e) { a.advance = null; renderControls(st); return; }
  const doy = rep.doy[String(year)];
  if (!doy) return;
  const start = Date.UTC(year, 4, 15), end = Date.UTC(year, 7, 15);   // 15 May -> 15 Aug
  const days = Math.round((end - start) / 864e5) + 1;
  const startDoy = Math.round((start - Date.UTC(year, 0, 1)) / 864e5) + 1;
  st.advance = { year, doy: new Map(rep.units.map((u, i) => [u, doy[i]])), total: rep.units.length,
                 start, startDoy };
  beginPlayback(st, {
    kind: "advance", year, frames: days, frameMs: 110,
    caption: "Watch the monsoon reach each sub-district, from our onset analysis of IMD rainfall.",
  });
  showAdvanceLayers(st);
  // Every covered state's sub-districts are needed; the map frames all five.
  const keys = Object.values(st.meta.covered).map((c) => c.key);
  let box = null;
  for (const k of keys) {
    const b = st.stateBox[k];
    box = box ? [Math.min(box[0], b[0]), Math.min(box[1], b[1]), Math.max(box[2], b[2]), Math.max(box[3], b[3])] : b;
  }
  st.map.fitBounds(box, { padding: isPhone() ? 20 : 60, essential: false });
  await Promise.all(keys.map((k) => loadState(st, k)));
  seek(st, 0);
  resumePlayback(st);
}

function seekAdvance(st, i) {
  const A = st.advance, map = st.map;
  if (!A) return;
  const today = A.startDoy + i;
  let n = 0;
  for (const f of st.geo.units) {
    const id = f.properties.unit_id, d = A.doy.get(id);
    const on = d != null && d <= today;
    if (on) n++;
    map.setFeatureState({ source: "mo-units", id }, { on });
  }
  st.dom.tlLabel.textContent = fmtDate(new Date(A.start + i * 864e5));
  st.dom.tlCount.textContent = `Sub-districts with monsoon onset: ${n.toLocaleString("en-IN")} / ${A.total.toLocaleString("en-IN")}`;
}

// ------------------------------------------------------------------ entry
export default function (component) {
  const { data, parentElement } = component;
  let st = parentElement.__mo;
  if (!st) {
    st = parentElement.__mo = init(parentElement, component);
    let stored = null;
    try { stored = sessionStorage.getItem("mo-wind-v1"); } catch (e) { /* ignore */ }
    // Wind flow is on by default, off by default under reduced motion.
    st.windWanted = stored == null ? !reducedMotion() : stored === "1";
  }
  st.component = component;
  st.data = data;
  st.sel = { hazard: data.hazard, week: data.week, weather: data.weather };
  st.ready.then(() => {
    st.dom.wrap.style.setProperty("--mo-ctrl-filter", data.dark ? "invert(1)" : "none");
    renderControls(st);
    update(st);
    renderLegend(st); renderNote(st); renderHint(st);
    windColour(st);
    syncWind(st);
  }).catch(() => {});
  return () => {
    const s = parentElement.__mo;
    if (s) { windStop(s); if (s.play) pausePlayback(s); if (s.map) s.map.remove(); }
    delete parentElement.__mo;
  };
}
