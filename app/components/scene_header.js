// Seasonal header band (st.components.v2). Vector scenes on one canvas - no images, no
// downloads. Page text stays in Streamlit (st.title) for screen readers; the band draws
// the same words over the scene, aria-hidden. Decisions (which scene, how much rain) are
// made in app/scenes.py from config/scenes.yaml and arrive as data.

/* Seasonal header scenes - vector shapes on one canvas, no images.
 * drawScene(ctx, w, h, name, {dark, t, rain, dry}) draws one frame at time t (seconds).
 * Static previews call it once; the live header will call it per animation frame. */
(function (global) {
  "use strict";

  const PAL = {
    light: { ink: "#1C2B36", primary: "#2E5E7E", accent: "#4A8B4F", bg: "#F6F8F9" },
    dark:  { ink: "#E6EDF1", primary: "#7FB0D4", accent: "#8CC98F", bg: "#0F1B24" },
  };

  // Deterministic random so a static frame never changes between renders.
  function rng(seed) {
    let s = seed >>> 0;
    return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
  }

  function sky(ctx, w, h, top, bottom) {
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, top); g.addColorStop(1, bottom);
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
  }

  function blob(ctx, x, y, r, color) {
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
  }

  function cloudBank(ctx, w, y, scale, color, seed, drift) {
    // Soft, flat-bottomed banks: wide overlapping ellipses clipped to a line.
    const r = rng(seed), span = 260 * scale, n = Math.ceil(w / span) + 2;
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, w, y + 12 * scale); ctx.clip();
    ctx.fillStyle = color;
    for (let i = 0; i < n; i++) {
      const cx = ((i * span + drift) % (w + span)) - span * .5;
      for (let k = 0; k < 5; k++) {
        ctx.beginPath();
        ctx.ellipse(cx + (k - 2) * 40 * scale + (r() - .5) * 20, y - r() * 16 * scale,
                    (60 + r() * 50) * scale, (18 + r() * 14) * scale, 0, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.restore();
  }

  // Rolling hill / terrace band: a smooth wave filled to the bottom.
  function band(ctx, w, h, y, amp, freq, phase, color) {
    ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(0, h);
    for (let x = 0; x <= w; x += 8) ctx.lineTo(x, y + Math.sin(x * freq + phase) * amp);
    ctx.lineTo(w, h); ctx.closePath(); ctx.fill();
  }

  function paddyRows(ctx, w, y0, y1, color, sway, seed) {
    const r = rng(seed);
    ctx.strokeStyle = color; ctx.lineCap = "round";
    for (let y = y0; y < y1; y += 7) {
      const depth = (y - y0) / Math.max(1, y1 - y0);
      const step = 6 + (1 - depth) * 6, len = 4 + depth * 9;
      ctx.lineWidth = .8 + depth * 1.1;
      for (let x = (y * 3) % step; x < w; x += step) {
        const lean = Math.sin(x * .02 + sway) * (1.5 + depth * 2) + (r() - .5);
        ctx.beginPath(); ctx.moveTo(x, y + len); ctx.quadraticCurveTo(x + lean * .4, y + len * .5, x + lean, y); ctx.stroke();
      }
    }
  }

  // ------------------------------------------------------------------ scenes
  function monsoon(ctx, w, h, o) {
    const d = o.dark, t = o.t || 0, rain = o.dry ? 0 : (o.rain == null ? 0.7 : o.rain);
    const clear = o.dry, heavy = rain >= 0.9;
    sky(ctx, w, h, d ? (clear ? "#16324A" : heavy ? "#141F28" : "#1A2A36")
                     : (clear ? "#CFE3EF" : heavy ? "#8FA2B0" : "#AEBFCB"),
                  d ? "#223C4E" : (clear ? "#EAF3F7" : heavy ? "#C3CFD7" : "#D8E2E8"));
    if (!clear) {
      cloudBank(ctx, w, h * .2, 1.1, d ? "rgba(52,70,84,.9)" : "rgba(150,168,182,.85)", 7, t * 4);
      cloudBank(ctx, w, h * .34, .9, d ? "rgba(62,82,96,.75)" : "rgba(186,200,210,.8)", 11, t * 7);
      // Soft lightning: a slow glow inside one cloud, never a flash.
      const glow = o.flash || 0;
      if (glow > 0) {
        const gx = w * .72, gy = h * .18, gr = ctx.createRadialGradient(gx, gy, 2, gx, gy, 90);
        gr.addColorStop(0, `rgba(255,250,225,${.45 * glow})`); gr.addColorStop(1, "rgba(255,250,225,0)");
        ctx.fillStyle = gr; ctx.fillRect(gx - 90, gy - 90, 180, 180);
      }
    }
    // Terraced paddy: far to near, lighter to deeper greens.
    const greens = d ? ["#1F3B2E", "#244634", "#2A523B", "#305E42"] : ["#9CC7A0", "#7DB385", "#5E9E69", "#4A8B4F"];
    const ys = [.58, .68, .79, .9];
    ys.forEach((y, i) => band(ctx, w, h, h * y, 4 + i * 2, .006 + i * .002, i * 1.7, greens[i]));
    paddyRows(ctx, w, h * .8, h, d ? "#3E7552" : "#3A7A45", t * .8, 3);
    // Rain: diagonal streaks, near drops longer and faster.
    if (rain > 0) {
      const r = rng(42), n = Math.round((o.maxParticles || 150) * rain);
      for (let i = 0; i < n; i++) {
        const near = r() < .35, speed = near ? 520 : 300, len = near ? 16 : 9;
        const x0 = r() * (w + 60), y0 = r() * h;
        const y = (y0 + t * speed) % (h + len), x = (((x0 - t * speed * .25) % (w + 60)) + w + 60) % (w + 60);
        ctx.strokeStyle = d ? `rgba(200,220,235,${near ? .45 : .25})` : `rgba(40,70,95,${near ? .38 : .2})`;
        ctx.lineWidth = near ? 1.3 : .8;
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - len * .25, y + len); ctx.stroke();
      }
    }
  }

  function harvest(ctx, w, h, o) {
    const d = o.dark, t = o.t || 0;
    sky(ctx, w, h, d ? "#1B2433" : "#F4E6C4", d ? "#3A3324" : "#FBF3DE");
    blob(ctx, w * .82, h * .32, 26, d ? "#E9D9A6" : "#F2C66D");            // low sun / moon
    band(ctx, w, h, h * .62, 5, .005, 1, d ? "#3B3A28" : "#E3C77E");
    band(ctx, w, h, h * .74, 6, .007, 2, d ? "#4A4530" : "#D8B25A");
    // Golden stalks with drooping grain heads, swaying.
    const r = rng(9);
    for (let x = -4; x < w + 4; x += 5) {
      const base = h * (.86 + r() * .14), tall = 28 + r() * 26;
      const sway = Math.sin(t * 1.1 + x * .03) * 4;
      ctx.strokeStyle = d ? "#8C7A45" : "#B8892F"; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(x, base);
      ctx.quadraticCurveTo(x + sway * .3, base - tall * .6, x + sway, base - tall); ctx.stroke();
      ctx.fillStyle = d ? "#C9AE62" : "#D9A63A";
      for (let k = 0; k < 4; k++) blob(ctx, x + sway + k * 1.6, base - tall + k * 3, 1.6, ctx.fillStyle);
    }
    // A few drifting leaves.
    for (let i = 0; i < 6; i++) {
      const lx = (w * (i / 6 + .05) + t * 22) % w, ly = h * .2 + Math.sin(t + i) * 12 + i * 9;
      ctx.save(); ctx.translate(lx, ly); ctx.rotate(t * .6 + i);
      ctx.fillStyle = d ? "#A9864A" : "#C07A2C";
      ctx.beginPath(); ctx.ellipse(0, 0, 5, 2.2, 0, 0, Math.PI * 2); ctx.fill(); ctx.restore();
    }
  }

  function ridgePoints(w, base, peak, jag, seed, offset) {
    const r = rng(seed), pts = [];
    for (let x = -60; x <= w + 60; x += jag * (.6 + r() * .8)) {
      const y = base - peak * (.25 + .75 * Math.pow(r(), .8));
      pts.push([x + offset % jag, y]);
      pts.push([x + offset % jag + jag * .3, y + peak * (.08 + r() * .12)]);   // shoulders
    }
    return pts;
  }

  function ridge(ctx, w, h, pts, color) {
    ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(-60, h);
    pts.forEach(([x, y]) => ctx.lineTo(x, y)); ctx.lineTo(w + 60, h); ctx.closePath(); ctx.fill();
  }

  function winter(ctx, w, h, o) {
    const d = o.dark, t = o.t || 0;
    sky(ctx, w, h, d ? "#101C2B" : "#D6E4EE", d ? "#23384A" : "#F3F7FA");
    // Far snowy range: rock body, then snow clipped to the upper part of each peak.
    const far = ridgePoints(w, h * .66, h * .5, 70, 5, t * 1);
    ridge(ctx, w, h, far, d ? "#3A5064" : "#A9BCCB");
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, w, h * .36); ctx.clip();
    ridge(ctx, w, h, far, d ? "#C9D6E0" : "#FFFFFF"); ctx.restore();
    ridge(ctx, w, h, ridgePoints(w, h * .8, h * .3, 50, 8, t * 2.5), d ? "#2B3F51" : "#86A0B3");
    ridge(ctx, w, h, ridgePoints(w, h * .96, h * .22, 38, 13, t * 5), d ? "#1C2D3C" : "#5F7C92");
    // Low mist: soft horizontal ellipses drifting slowly.
    const mx = (t * 10) % (w + 400);
    for (let k = 0; k < 4; k++) {
      const cx = (mx + k * (w / 3)) % (w + 400) - 200, cy = h * (.7 + (k % 2) * .06);
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 260);
      g.addColorStop(0, d ? "rgba(190,210,225,.16)" : "rgba(255,255,255,.6)"); g.addColorStop(1, "rgba(255,255,255,0)");
      ctx.save(); ctx.translate(cx, cy); ctx.scale(1, .22); ctx.translate(-cx, -cy);
      ctx.fillStyle = g; ctx.fillRect(cx - 260, cy - 260, 520, 520); ctx.restore();
    }
    // Light snowfall.
    const r = rng(21), n = Math.round((o.maxParticles || 150) * .5);
    ctx.fillStyle = d ? "rgba(235,242,248,.85)" : "rgba(255,255,255,.95)";
    for (let i = 0; i < n; i++) {
      const s2 = .6 + r() * 1.8, x = (r() * w + Math.sin(t + i) * 8 + w) % w, y = (r() * h + t * (12 + s2 * 10)) % h;
      blob(ctx, x, y, s2, ctx.fillStyle);
    }
  }

  function blossom(ctx, x, y, s, rot, c1, c2) {
    // Palash: a curved, claw-like orange petal with a dark calyx at its base.
    ctx.save(); ctx.translate(x, y); ctx.rotate(rot);
    ctx.fillStyle = c1;
    ctx.beginPath(); ctx.moveTo(0, 0);
    ctx.bezierCurveTo(s * .9, -s * .2, s * 1.5, -s * .9, s * 1.1, -s * 1.9);
    ctx.bezierCurveTo(s * .7, -s * 1.1, s * .2, -s * .5, 0, 0); ctx.fill();
    ctx.fillStyle = c2; ctx.beginPath(); ctx.ellipse(0, 0, s * .35, s * .22, .5, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  }

  function spring(ctx, w, h, o) {
    const d = o.dark, t = o.t || 0;
    sky(ctx, w, h, d ? "#172334" : "#E3EDF3", d ? "#2A2A3A" : "#FBF0E3");
    band(ctx, w, h, h * .84, 4, .006, 0, d ? "#26382C" : "#BCD4AA");
    const wood = d ? "#3A2E28" : "#5B463A", c1 = d ? "#E8813E" : "#E8601F", c2 = d ? "#4A3024" : "#4E2A16";
    // Curved branches reaching in from the top right; flowers cluster along them.
    // Phones: the title spans the band, so the branch shrinks into the lower right.
    const phone = w < 640;
    ctx.save();
    if (phone) { ctx.translate(w, h * .42); ctx.scale(.62, .62); ctx.translate(-w, 0); }
    const branches = [[w + 20, h * .02, w * .9, h * .2, w * .74, h * .34, 7],
                      [w * .9, h * .16, w * .84, h * .02, w * .76, h * .06, 4],
                      [w * .84, h * .27, w * .78, h * .42, w * .68, h * .48, 3.5],
                      [w * .97, h * .1, w * .95, h * .3, w * .9, h * .44, 3]];
    ctx.strokeStyle = wood; ctx.lineCap = "round";
    branches.forEach(([x0, y0, cx, cy, x1, y1, lw]) => {
      ctx.lineWidth = lw; ctx.beginPath(); ctx.moveTo(x0, y0); ctx.quadraticCurveTo(cx, cy, x1, y1); ctx.stroke();
    });
    const r = rng(17);
    branches.forEach(([x0, y0, cx, cy, x1, y1]) => {
      for (let i = 0; i < 7; i++) {
        const k = .25 + r() * .75, u = 1 - k;
        const bx = u * u * x0 + 2 * u * k * cx + k * k * x1, by = u * u * y0 + 2 * u * k * cy + k * k * y1;
        for (let j = 0; j < 3; j++) blossom(ctx, bx + (r() - .5) * 8, by + (r() - .5) * 6, 7 + r() * 4,
                                            -1.2 + r() * 2.4, c1, c2);
      }
    });
    ctx.restore();
    const n = Math.round((o.maxParticles || 150) * .1);   // a few falling, tumbling slowly
    for (let i = 0; i < n; i++) {
      const x = ((w * .95 - r() * w * .8 - t * 14) % w + w) % w, y = (r() * h + t * 18) % h;
      blossom(ctx, x, y, 7, t * .8 + i, c1, c2);
    }
  }

  function summer(ctx, w, h, o) {
    const d = o.dark, t = o.t || 0;
    sky(ctx, w, h, d ? "#1C1E2C" : "#F6E3C8", d ? "#3A2A26" : "#FBEFE0");
    blob(ctx, w * .18, h * .5, 34, d ? "rgba(240,200,150,.25)" : "rgba(245,180,110,.45)");  // hazy sun
    band(ctx, w, h, h * .8, 2, .004, 0, d ? "#3A3026" : "#E2C9A2");
    // Heat shimmer: thin wavering bands just above the horizon.
    for (let i = 0; i < 5; i++) {
      ctx.strokeStyle = d ? "rgba(255,220,180,.10)" : "rgba(255,255,255,.55)"; ctx.lineWidth = 1.4;
      ctx.beginPath();
      for (let x = 0; x <= w; x += 10) ctx.lineTo(x, h * (.74 - i * .025) + Math.sin(x * .04 + t * 3 + i) * 1.6);
      ctx.stroke();
    }
    // Gulmohar: short trunk forking into a wide, flat umbrella crown of red flowers.
    const tx = w < 640 ? w * .86 : w * .82, wood = d ? "#2E2622" : "#5E4535";
    ctx.save();
    if (w < 640) { ctx.translate(tx, h); ctx.scale(.58, .58); ctx.translate(-tx, -h); }  // below the title
    ctx.strokeStyle = wood; ctx.lineCap = "round";
    [[0, h * .9, 0, h * .55, 7], [0, h * .6, -46, h * .36, 4], [0, h * .6, 52, h * .38, 4], [0, h * .55, 8, h * .3, 3]]
      .forEach(([x0, y0, x1, y1, lw]) => { ctx.lineWidth = lw; ctx.beginPath(); ctx.moveTo(tx + x0, y0); ctx.lineTo(tx + x1, y1); ctx.stroke(); });
    const r = rng(33);
    ctx.fillStyle = d ? "rgba(46,70,50,.9)" : "rgba(92,130,80,.85)";         // feathery green under the flowers
    ctx.beginPath(); ctx.ellipse(tx, h * .34, 110, 26, 0, 0, Math.PI * 2); ctx.fill();
    for (let i = 0; i < 160; i++) {
      const a = r() * Math.PI * 2, rr = Math.sqrt(r());
      const x = tx + Math.cos(a) * rr * 118, y = h * .31 + Math.sin(a) * rr * 24 - (1 - rr) * 6;
      blob(ctx, x, y, 2.6 + r() * 2.4, d ? `rgba(214,78,60,${.6 + r() * .35})` : `rgba(214,52,34,${.7 + r() * .3})`);
    }
    ctx.restore();
    const n = Math.round((o.maxParticles || 150) * .08);
    for (let i = 0; i < n; i++) {
      const x = (w * .8 - r() * w * .5 - t * 10 + w) % w, y = (h * .3 + r() * h * .6 + t * 12) % h;
      ctx.save(); ctx.translate(x, y); ctx.rotate(t + i);
      ctx.fillStyle = d ? "#C8463A" : "#D23C28"; ctx.beginPath(); ctx.ellipse(0, 0, 4, 2.4, 0, 0, 6.28); ctx.fill();
      ctx.restore();
    }
  }

  function classic(ctx, w, h, o) {                   // today's look: plain page ground
    ctx.fillStyle = (o.dark ? PAL.dark : PAL.light).bg; ctx.fillRect(0, 0, w, h);
  }

  const SCENES = { monsoon, harvest, winter, spring, summer, classic };

  // Asia/Kolkata month -> scene. Jun-Sep monsoon, Oct-Nov harvest, Dec-Feb winter,
  // Mar-Apr spring, May summer.
  function sceneFor(date) {
    const m = new Date(date.toLocaleString("en-US", { timeZone: "Asia/Kolkata" })).getMonth() + 1;
    if (m >= 6 && m <= 9) return "monsoon";
    if (m >= 10 && m <= 11) return "harvest";
    if (m === 12 || m <= 2) return "winter";
    if (m <= 4) return "spring";
    return "summer";
  }

  // Scrim under the text: a soft fade from the page ground on the left, so the title
  // and description keep AA contrast over any scene.
  function scrim(ctx, w, h, dark, phone) {
    const bg = dark ? "15,27,36" : "246,248,249";
    if (phone) {                                   // phones: the title spans the band
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, `rgba(${bg},.84)`); g.addColorStop(.45, `rgba(${bg},.6)`); g.addColorStop(.8, `rgba(${bg},0)`);
      ctx.fillStyle = g; ctx.fillRect(0, 0, w, h); return;
    }
    const g = ctx.createLinearGradient(0, 0, Math.min(w, 760), 0);
    g.addColorStop(0, `rgba(${bg},.92)`); g.addColorStop(.6, `rgba(${bg},.78)`); g.addColorStop(1, `rgba(${bg},0)`);
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
  }

  function drawScene(ctx, w, h, name, o) {
    (SCENES[name] || monsoon)(ctx, w, h, o || {});
  }

  global.MoScenes = { drawScene, scrim, sceneFor, SCENES: Object.keys(SCENES), PAL };
})(window);

// ==========================================================================
// Runner: one band per page, kept across reruns (st.components.v2).
// ==========================================================================
const Scenes = window.MoScenes;
const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function lum(r, g, b) {
  const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
function hexRgb(hex) { const n = parseInt(hex.slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; }

function build(root) {
  root.innerHTML = `
    <div class="mo-scene" role="presentation">
      <canvas class="mo-scene-canvas" aria-hidden="true"></canvas>
      <div class="mo-scene-text" aria-hidden="true">
        <p class="mo-scene-title"></p><p class="mo-scene-desc"></p>
      </div>
      <p class="mo-scene-cap"></p>
    </div>`;
  const q = (s) => root.querySelector(s);
  return { band: q(".mo-scene"), canvas: q(".mo-scene-canvas"), title: q(".mo-scene-title"),
           desc: q(".mo-scene-desc"), cap: q(".mo-scene-cap") };
}

function init(root) {
  if (typeof MO_SCENE_CSS === "string" && !document.getElementById("mo-scene-css")) {
    const tag = document.createElement("style");
    tag.id = "mo-scene-css"; tag.textContent = MO_SCENE_CSS;
    document.head.appendChild(tag);
  }
  const st = { dom: build(root), data: {}, t0: performance.now(), raf: 0, last: 0,
               visible: true, lastInput: performance.now(), stopped: false, shown: false, flashAt: 12 };
  const wake = () => { st.lastInput = performance.now(); if (st.stopped) { st.stopped = false; loop(st); } };
  ["scroll", "click", "keydown", "pointerdown", "touchstart"].forEach((e) =>
    window.addEventListener(e, wake, { passive: true, capture: true }));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) wake(); });
  if ("IntersectionObserver" in window) {
    new IntersectionObserver((es) => { st.visible = es[0].isIntersecting; if (st.visible) wake(); })
      .observe(st.dom.band);
  }
  if ("ResizeObserver" in window) new ResizeObserver(() => { size(st); draw(st, st.tNow || 3.2); }).observe(st.dom.band);
  return st;
}

function size(st) {
  const c = st.dom.canvas, r = st.dom.band.getBoundingClientRect();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  st.w = Math.max(1, Math.round(r.width)); st.h = Math.max(1, Math.round(r.height));
  c.width = st.w * dpr; c.height = st.h * dpr;
  st.ctx = c.getContext("2d"); st.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function options(st, t) {
  const d = st.data, phone = st.w < 640, cfg = d.cfg || {};
  const caps = cfg.particles || { desktop: 150, phone: 60 };
  const o = { dark: !!d.dark, t, maxParticles: phone ? caps.phone : caps.desktop };
  const mode = d.mode, inten = cfg.rain_intensity || { none: 0, light: .35, heavy: 1 };
  if (mode === "clear") o.dry = true;
  else if (mode && inten[mode] != null) o.rain = inten[mode];
  // Soft lightning in monsoon cloud: a slow fade in and out every ~15 s, never a flash.
  if (st.animating && !o.dry && (o.rain == null || o.rain > 0)) {
    const k = (t - st.flashAt);
    if (k > 1.6) st.flashAt = t + 10 + ((t * 7919) % 9);
    o.flash = k >= 0 && k <= 1.6 ? Math.sin(Math.PI * k / 1.6) * (o.rain >= 0.9 ? .8 : .5) : 0;
  }
  return o;
}

function draw(st, t) {
  if (!st.ctx) return;
  st.tNow = t;
  Scenes.drawScene(st.ctx, st.w, st.h, st.data.scene, options(st, t));
  Scenes.scrim(st.ctx, st.w, st.h, !!st.data.dark, st.w < 640);
}

// Worst WCAG contrast between the title colour and the frame under the text box.
function measure(st) {
  try {
    const tb = st.dom.title.getBoundingClientRect(), br = st.dom.band.getBoundingClientRect();
    const dpr = st.dom.canvas.width / st.w, ctx = st.ctx;
    const ink = lum(...hexRgb(st.data.ink || "#1C2B36"));
    let worst = 21;
    for (let i = 0; i <= 8; i++) for (let j = 0; j <= 2; j++) {
      const x = (tb.left - br.left + tb.width * i / 8) * dpr, y = (tb.top - br.top + tb.height * j / 2) * dpr;
      if (x < 0 || y < 0 || x >= st.dom.canvas.width || y >= st.dom.canvas.height) continue;
      const p = ctx.getImageData(x, y, 1, 1).data, L = lum(p[0], p[1], p[2]);
      const cr = (Math.max(L, ink) + .05) / (Math.min(L, ink) + .05);
      worst = Math.min(worst, cr);
    }
    st.dom.band.dataset.contrast = worst.toFixed(2);
  } catch (e) { /* measurement is best effort */ }
}

function loop(st) {
  cancelAnimationFrame(st.raf);
  const fps = (st.data.cfg && st.data.cfg.fps) || 30, idle = ((st.data.cfg && st.data.cfg.idle_stop_seconds) || 60) * 1000;
  const tick = (now) => {
    if (!st.animating) return;
    if (now - st.lastInput > idle) { st.stopped = true; st.dom.band.dataset.state = "idle"; return; }
    if (!st.visible || document.hidden) { st.raf = requestAnimationFrame(tick); return; }
    if (now - st.last >= 1000 / fps) { st.last = now; draw(st, 3.2 + (now - st.t0) / 1000); }
    st.raf = requestAnimationFrame(tick);
  };
  st.dom.band.dataset.state = "running";
  st.raf = requestAnimationFrame(tick);
}

export default function (component) {
  const { data, parentElement } = component;
  let st = parentElement.__moScene;
  if (!st) st = parentElement.__moScene = init(parentElement);
  const changed = JSON.stringify(st.data) !== JSON.stringify(data);
  st.data = data;
  const d = st.dom;
  d.band.className = `mo-scene mo-scene-${data.scene}` + (data.faint ? " mo-scene-faint" : "");
  d.band.style.setProperty("--mo-scene-h", `${data.height || 180}px`);
  d.band.style.setProperty("--mo-scene-h-phone", `${data.height_phone || 120}px`);
  d.title.textContent = data.title || ""; d.desc.textContent = data.desc || "";
  d.title.style.color = data.ink || ""; d.desc.style.color = data.ink_soft || "";
  d.cap.textContent = data.caption || ""; d.cap.hidden = !data.caption;
  if (!changed && st.ctx) return;
  size(st);
  st.animating = !!data.animate && !reducedMotion();
  cancelAnimationFrame(st.raf);
  draw(st, 3.2);                                    // first frame (the static frame, too)
  measure(st);
  if (!st.shown) {                                  // page first, then fade the scene in
    st.shown = true;
    requestAnimationFrame(() => d.canvas.classList.add("mo-scene-in"));
  }
  if (st.animating) { st.lastInput = performance.now(); loop(st); }
  else d.band.dataset.state = "static";
}
