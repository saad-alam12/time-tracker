"use strict";

// Visually distinct palette: 12 hues spaced ≥~25° apart (no two near-identical), all
// readable on the dark background. Categories are assigned collision-free (see below),
// so two categories never share — or sit adjacent on — a colour.
const PALETTE = [
  "#ff453a", // red
  "#ff9f0a", // orange
  "#ffd60a", // yellow
  "#a2e635", // lime
  "#30d158", // green
  "#2dd4bf", // teal
  "#0a84ff", // blue
  "#5e5ce6", // indigo
  "#bf5af2", // purple
  "#ff2d92", // pink
  "#c8966b", // brown
  "#98989d", // grey
];

const charts = {};
let currentDate = null;
let view = "day";

let CAT_COLOR = {};  // name -> colour, rebuilt from the full category set so every card agrees

function hashName(s) {
  let h = 0;
  for (const ch of (s || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
}

// Deterministic, collision-free assignment over the *whole* known category set: each name
// hashes to a preferred palette slot, then linear-probes to the next free slot. Result is
// independent of input order (names sorted first), so colours are stable across all cards.
function rebuildCategoryColors(names) {
  const used = new Array(PALETTE.length).fill(false);
  const map = {};
  for (const n of [...new Set(names)].sort()) {
    let i = hashName(n) % PALETTE.length, tries = 0;
    while (used[i] && tries < PALETTE.length) { i = (i + 1) % PALETTE.length; tries++; }
    used[i] = true;
    map[n] = PALETTE[i];
  }
  CAT_COLOR = map;
}

function categoryColor(name) {
  return CAT_COLOR[name] || PALETTE[hashName(name) % PALETTE.length];
}

function fmt(seconds) {
  const m = Math.round(seconds / 60);
  if (m < 1) return "0m";
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
function fmtHM(seconds) { return `${(seconds / 3600).toFixed(1)}h`; }

async function getJSON(url) { return (await fetch(url)).json(); }

function destroy(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

// Format from LOCAL date parts, not toISOString(): toISOString() is UTC, so in a
// timezone east of UTC (e.g. Munich UTC+2) "2026-06-20T00:00" local slices back to
// "2026-06-19", making the forward button appear dead and the back button skip a day.
function localISO(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function addDays(iso, n) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + n);
  return localISO(d);
}
function addMonths(iso, n) {
  const d = new Date(iso + "T00:00:00");
  d.setMonth(d.getMonth() + n);
  return localISO(d);
}

// --- Chart.js shared defaults ---
Chart.defaults.color = "#9b9ba3";
Chart.defaults.font.family = "Inter, -apple-system, sans-serif";
Chart.defaults.borderColor = "rgba(255,255,255,0.07)";

const centerTotal = {
  id: "centerTotal",
  afterDraw(chart) {
    const t = chart.config.options.plugins.centerTotal;
    if (!t || !t.text) return;
    const { ctx, chartArea: { left, right, top, bottom } } = chart;
    const x = (left + right) / 2, y = (top + bottom) / 2;
    ctx.save();
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = "#f5f5f7"; ctx.font = "600 20px Inter, sans-serif";
    ctx.fillText(t.text, x, y - 8);
    ctx.fillStyle = "#9b9ba3"; ctx.font = "12px Inter, sans-serif";
    ctx.fillText(t.sub || "", x, y + 13);
    ctx.restore();
  },
};
Chart.register(centerTotal);

// --- Icon element with letter-tile fallback ---
function iconEl(kind, key, name, color) {
  const wrap = document.createElement("span");
  wrap.className = "ico";
  const img = new Image();
  img.src = `/api/icon?kind=${kind}&key=${encodeURIComponent(key || "")}`;
  img.alt = "";
  img.onerror = () => {
    wrap.classList.add("tile");
    wrap.style.background = color;
    wrap.textContent = (name || "?").trim().charAt(0).toUpperCase() || "?";
  };
  wrap.appendChild(img);
  return wrap;
}

function esc(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function chip(category) {
  if (!category) return "";
  return `<span class="chip" style="background:${categoryColor(category)}33;color:${categoryColor(category)}">${category}</span>`;
}
// Minimal monochrome glyph marking where an app's usage came from.
function sourceIcon(source) {
  const ic = {
    mac: '<rect x="2" y="3" width="12" height="8.5" rx="1.2"/><path d="M1 13.5h14"/>',
    iphone: '<rect x="4.5" y="1.5" width="7" height="13" rx="1.4"/><path d="M6.7 12.6h2.6"/>',
    watch: '<rect x="4" y="4" width="8" height="8" rx="2"/><path d="M6 4l.6-2.2h2.8L10 4M6 12l.6 2.2h2.8L10 12"/>',
  }[source];
  if (!ic) return "";
  const label = source === "iphone" ? "iPhone" : source === "watch" ? "Apple Watch" : "Mac";
  return `<svg class="src" viewBox="0 0 16 16" aria-label="${label}"><title>${label}</title>${ic}</svg>`;
}

// --- Shared tooltip ---
const tip = () => document.getElementById("tip");
function showTip(html, ev) {
  const t = tip();
  t.innerHTML = html;
  t.style.opacity = "1";
  const pad = 14;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + 260 > window.innerWidth) x = ev.clientX - 260 - pad;
  t.style.left = x + "px"; t.style.top = y + "px";
}
function hideTip() { tip().style.opacity = "0"; }

// --- Hero: rings, legend, streak, insights ---
function ring(cx, cy, r, color, pct) {
  const c = 2 * Math.PI * r;
  const off = c * (1 - Math.max(0, Math.min(1, pct)));
  return `<circle class="track" cx="${cx}" cy="${cy}" r="${r}" stroke-width="10"></circle>` +
    `<circle class="prog" cx="${cx}" cy="${cy}" r="${r}" stroke-width="10" stroke="${color}" ` +
    `stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"></circle>`;
}
function renderHero(s) {
  const st = s.stats, g = s.goals;
  const focusPct = st.focus / g.focus_seconds;
  const leisPct = st.leisure / g.leisure_cap_seconds;
  const deepPct = st.deep_block_count / g.deep_blocks;
  const leisColor = st.leisure > g.leisure_cap_seconds ? "var(--red)" : "#ff9f0a";
  document.getElementById("rings").innerHTML =
    `<svg class="rings" viewBox="0 0 120 120">
       ${ring(60, 60, 52, "#30d158", focusPct)}
       ${ring(60, 60, 40, leisColor === "var(--red)" ? "#ff453a" : "#ff9f0a", Math.min(1, leisPct))}
       ${ring(60, 60, 28, "#0a84ff", deepPct)}
     </svg>`;
  document.getElementById("ring-legend").innerHTML = [
    `<div class="rl"><span class="dot" style="background:#30d158"></span><b>${fmt(st.focus)}</b><span>/ ${fmt(g.focus_seconds)} focus</span></div>`,
    `<div class="rl"><span class="dot" style="background:#ff9f0a"></span><b>${fmt(st.leisure)}</b><span>/ ${fmt(g.leisure_cap_seconds)} leisure</span></div>`,
    `<div class="rl"><span class="dot" style="background:#0a84ff"></span><b>${st.deep_block_count}</b><span>/ ${g.deep_blocks} deep blocks</span></div>`,
  ].join("");
  const streak = st.focus_streak || 0;
  document.getElementById("streak").textContent = streak > 0 ? `🔥 ${streak}-day focus streak` : "";
  const ul = document.getElementById("insights");
  ul.innerHTML = (s.insights && s.insights.length)
    ? s.insights.map((t) => `<li>${t}</li>`).join("")
    : `<li class="empty">No notable patterns for this day yet.</li>`;
  const d = new Date(s.date + "T00:00:00");
  document.getElementById("hero-date").textContent =
    d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

// --- Swimlane timeline ---
// Timeline (swimlane) display prefs. Persisted to config/timeline.json.
//   order  = top-to-bottom lane order
//   shown  = force a category onto the timeline even with no activity that day
//   hidden = force a category off the timeline
// A category is a LANE if: not hidden AND (has data that day OR in shown). Otherwise it's a CHIP.
let TLP = { order: [], shown: [], hidden: [] };
let lastSwim = { sessions: [], dayStart: 0 };
let dragEl = null;

function saveTLP() {
  fetch("/api/timeline_prefs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order: TLP.order, shown: TLP.shown, hidden: TLP.hidden }),
  });
}
// Lanes = categories on the timeline, in order. byCat: {cat: sessions[]} for the day.
function laneCats(present, byCat) {
  const hidden = new Set(TLP.hidden), shown = new Set(TLP.shown), seen = new Set(), out = [];
  const isLane = (c) => !hidden.has(c) && (byCat[c] || shown.has(c));
  const add = (c) => { if (c && isLane(c) && !seen.has(c)) { out.push(c); seen.add(c); } };
  TLP.order.forEach(add);   // saved order first
  present.forEach(add);     // then remaining data-carrying cats (already time-sorted)
  TLP.shown.forEach(add);   // then any force-shown empty lanes not yet placed
  return out;
}

function renderSwim(sessions, dayStart) {
  lastSwim = { sessions, dayStart };
  const DAY = 86400;
  const swim = document.getElementById("swim");
  const hiddenWrap = document.getElementById("swim-hidden");
  swim.innerHTML = "";
  hiddenWrap.innerHTML = "";
  const byCat = {};
  for (const x of sessions) {
    if (x.end - x.start < 60) continue; // hide flecks
    (byCat[x.category] = byCat[x.category] || []).push(x);
  }
  const present = Object.keys(byCat).sort(
    (a, b) => byCat[b].reduce((s, x) => s + x.end - x.start, 0) - byCat[a].reduce((s, x) => s + x.end - x.start, 0)
  );
  const lanes = laneCats(present, byCat);
  if (!lanes.length) swim.innerHTML = `<div class="empty-state">No categories on the timeline — click a chip below to add one.</div>`;
  for (const cat of lanes) {
    const lane = document.createElement("div");
    lane.className = "lane";
    lane.dataset.cat = cat;
    lane.innerHTML =
      `<div class="lane-label">
         <span class="drag-h" title="Drag to reorder">⠿</span>
         <span class="dot" style="background:${categoryColor(cat)}"></span>
         <span class="nm">${cat}</span>
         <button class="lane-hide" title="Hide this lane">✕</button>
       </div>`;
    const track = document.createElement("div");
    track.className = "lane-track";
    for (const x of (byCat[cat] || [])) {
      const b = document.createElement("div");
      b.className = "blk";
      b.style.left = ((x.start - dayStart) / DAY * 100) + "%";
      b.style.width = Math.max(0.2, (x.end - x.start) / DAY * 100) + "%";
      b.style.background = categoryColor(cat);
      const t0 = new Date(x.start * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const t1 = new Date(x.end * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      // Prefer the actual window/tab title (e.g. the open website) over the bare app name,
      // shortened for the tooltip; the source glyph replaces the old "(mac)" text label.
      const titleText = x.title || x.name;
      const short = titleText.length > 52 ? titleText.slice(0, 51) + "…" : titleText;
      const meta = chip(cat) + (x.tags || []).map((t) => chip(t)).join("");
      b.onmousemove = (e) => showTip(`<b>${esc(short)}</b>${sourceIcon(x.source)}<br><span class="sub">${t0}–${t1} · ${fmt(x.end - x.start)}</span><br>${meta}`, e);
      b.onmouseleave = hideTip;
      b.onclick = () => openDetail("app", x.bundle_id, x.name, x.category);
      track.appendChild(b);
    }
    lane.appendChild(track);
    lane.querySelector(".lane-hide").onclick = () => {       // remove this category from the timeline
      TLP.shown = TLP.shown.filter((c) => c !== cat);
      if (!TLP.hidden.includes(cat)) TLP.hidden.push(cat);
      saveTLP(); renderSwim(lastSwim.sessions, lastSwim.dayStart);
    };
    attachLaneDrag(lane, swim);
    swim.appendChild(lane);
  }
  // Chip row: every known category that is NOT on the timeline. Click a chip to add it as a lane.
  const laneSet = new Set(lanes);
  const known = [];
  const push = (c) => { if (c && !laneSet.has(c) && !known.includes(c)) known.push(c); };
  [...(CATS || []), ...TLP.order, ...TLP.shown, ...TLP.hidden, ...present].forEach(push);
  for (const cat of known) {
    const chip = document.createElement("button");
    chip.className = "swim-chip";
    chip.innerHTML = `<span class="dot" style="background:${categoryColor(cat)}"></span>${cat}`;
    chip.title = "Add to timeline";
    chip.onclick = () => {                                    // add this category to the timeline
      TLP.hidden = TLP.hidden.filter((c) => c !== cat);
      if (!TLP.shown.includes(cat)) TLP.shown.push(cat);
      saveTLP(); renderSwim(lastSwim.sessions, lastSwim.dayStart);
    };
    hiddenWrap.appendChild(chip);
  }
  document.getElementById("axis-ticks").innerHTML =
    ["00:00", "06:00", "12:00", "18:00", "24:00"].map((t) => `<span>${t}</span>`).join("");
}

// Drag-to-reorder lanes. Drag starts only from the ⠿ handle so block clicks/tooltips still work.
function attachLaneDrag(lane, swim) {
  const handle = lane.querySelector(".drag-h");
  handle.addEventListener("mousedown", () => { lane.draggable = true; });
  lane.addEventListener("dragstart", () => { dragEl = lane; lane.classList.add("dragging"); });
  lane.addEventListener("dragend", () => {
    lane.classList.remove("dragging"); lane.draggable = false; dragEl = null;
    const visible = [...swim.querySelectorAll(".lane")].map((l) => l.dataset.cat);
    TLP.order = [...visible, ...TLP.order.filter((c) => !visible.includes(c))];
    saveTLP();
  });
  lane.addEventListener("dragover", (e) => {
    e.preventDefault();
    if (!dragEl || dragEl === lane) return;
    const r = lane.getBoundingClientRect();
    swim.insertBefore(dragEl, e.clientY > r.top + r.height / 2 ? lane.nextSibling : lane);
  });
}

// --- Categories doughnut (+ overlapping tag rings) ---
function renderCategories(cats, catTags) {
  destroy("categories");
  const total = cats.reduce((a, c) => a + c.seconds, 0);
  charts.categories = new Chart(document.getElementById("categories"), {
    type: "doughnut",
    data: {
      labels: cats.map((c) => c.category),
      datasets: [{
        data: cats.map((c) => +(c.seconds / 60).toFixed(1)),
        backgroundColor: cats.map((c) => categoryColor(c.category)),
        borderColor: "rgba(0,0,0,0)", borderWidth: 2, hoverOffset: 6,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: "66%",
      plugins: {
        legend: { position: "right", labels: { color: "#f5f5f7", boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: { callbacks: {
          label: (x) => `${x.label}: ${fmt(x.raw * 60)}`,
          // Break the hovered category down by overlapping tag ("Coding — of which Work 3h").
          afterLabel: (x) => {
            const ct = catTags && catTags[x.label];
            if (!ct) return [];
            return Object.entries(ct).sort((a, b) => b[1] - a[1])
              .map(([t, sec]) => `  of which ${t}: ${fmt(sec)}`);
          },
        } },
        centerTotal: { text: fmt(total), sub: "total" },
      },
    },
  });
}
// Tag view: time spent per tag as a ranked bar list. Tags overlap categories (a tag like
// Work spans Coding + the docs site), so these totals are independent of the doughnut.
function renderTags(s) {
  const el = document.getElementById("tagrings");
  if (!el) return;
  const tags = s.tags || [];  // backend-sorted desc
  if (!tags.length) { el.innerHTML = ""; el.hidden = true; return; }
  el.hidden = false;
  const max = tags[0].seconds || 1, day = s.total_seconds || 1;
  const bars = tags.map((t) => {
    const col = categoryColor(t.tag);
    return `<div class="tagbar-row">
      <span class="tagbar-nm"><i class="dot" style="background:${col}"></i>${esc(t.tag)}</span>
      <span class="tagbar-track"><span class="tagbar-fill" style="width:${Math.max(3, t.seconds / max * 100)}%;background:${col}"></span></span>
      <span class="tagbar-val">${fmt(t.seconds)} <em>${Math.round(t.seconds / day * 100)}%</em></span>
    </div>`;
  }).join("");
  el.innerHTML = `<div class="tagrings-head">Tags <span>· time per tag</span></div>${bars}`;
}

// --- Top apps list (with Mac / iPhone source filter) ---
let ALL_APPS = [];
let APP_FILTER = "all";  // all | mac | iphone
function renderApps(apps) {
  ALL_APPS = apps;
  applyAppFilter();
}
function applyAppFilter() {
  const apps = APP_FILTER === "all" ? ALL_APPS
    : ALL_APPS.filter((a) => a.source === APP_FILTER);
  // Total reflects only the shown apps, so filtering re-scopes the sum.
  const total = apps.reduce((sum, a) => sum + a.seconds, 0);
  const label = APP_FILTER === "all" ? "all devices" : APP_FILTER === "mac" ? "Mac" : "iPhone";
  document.getElementById("app-total").textContent = `${fmt(total)} · ${label}`;
  const wrap = document.getElementById("apps");
  wrap.innerHTML = "";
  if (!apps.length) { wrap.innerHTML = `<div class="empty-state">No apps recorded.</div>`; return; }
  const max = apps[0].seconds || 1;
  for (const a of apps) {
    const dirs = a.directories || [];
    const row = document.createElement("div");
    row.className = "row";
    row.appendChild(iconEl("app", a.bundle_id, a.name, categoryColor(a.category)));
    const meta = document.createElement("div");
    meta.className = "meta";
    const caret = dirs.length ? `<span class="caret" aria-label="Show directories">›</span>` : "";
    meta.innerHTML = `<div class="nm">${caret}${a.name}${sourceIcon(a.source)}${chip(a.category)}</div>` +
      `<div class="bar" style="width:${Math.max(4, a.seconds / max * 100)}%;background:${categoryColor(a.category || "Uncategorised")}"></div>`;
    maybeTag(meta.querySelector(".nm"), "app", a.bundle_id, a.name, a.tags, a.category);
    row.appendChild(meta);
    const val = document.createElement("div");
    val.className = "val"; val.textContent = fmt(a.seconds);
    row.appendChild(val);
    row.onclick = () => openDetail("app", a.bundle_id, a.name, a.category);
    wrap.appendChild(row);

    if (dirs.length) {
      // Inline, collapsed list of the project directories worked in (Warp). Each dir can be
      // re-categorised on its own (type "dir" override, cascading to subfolders). The
      // "(other)" bucket — time whose title wasn't a path — has no picker.
      const sublist = document.createElement("div");
      sublist.className = "sublist"; sublist.hidden = true;
      for (const d of dirs) {
        const sr = document.createElement("div");
        sr.className = "subrow";
        sr.innerHTML = `<div class="nm" title="${esc(d.dir)}">${esc(d.dir)}</div>`;
        if (d.dir !== "(other)") {
          const cat = document.createElement("button");
          cat.className = "catedit sm";
          cat.innerHTML = `<span class="dot" style="background:${categoryColor(d.category)}"></span>${d.category}`;
          cat.onclick = (e) => { e.stopPropagation(); openCatMenu(e.currentTarget, "dir", d.dir, d.dir); };
          sr.appendChild(cat);
          maybeTag(sr, "dir", d.dir, d.dir, d.tags, d.category);
        }
        const sv = document.createElement("div");
        sv.className = "val"; sv.textContent = fmt(d.seconds);
        sr.appendChild(sv);
        sublist.appendChild(sr);
      }
      wrap.appendChild(sublist);
      const c = row.querySelector(".caret");
      c.onclick = (e) => {
        e.stopPropagation();
        sublist.hidden = !sublist.hidden;
        c.classList.toggle("open", !sublist.hidden);
      };
    }
  }
}

// --- Website intelligence ---
function renderBrowsing(b) {
  const el = document.getElementById("browsing");
  const total = (b.productive + b.distracting + b.neutral) || 1;
  const seg = (v, c) => v > 0 ? `<span style="width:${v / total * 100}%;background:${c}"></span>` : "";
  el.innerHTML =
    `<div class="split">${seg(b.productive, "#30d158")}${seg(b.neutral, "#8e8e93")}${seg(b.distracting, "#ff453a")}</div>` +
    `<div class="split-legend">
       <span><i class="dot" style="background:#30d158"></i>Productive ${fmt(b.productive)}</span>
       <span><i class="dot" style="background:#8e8e93"></i>Neutral ${fmt(b.neutral)}</span>
       <span><i class="dot" style="background:#ff453a"></i>Distracting ${fmt(b.distracting)}</span>
     </div>`;
}
function renderWebsites(sites) {
  const wrap = document.getElementById("websites");
  wrap.innerHTML = "";
  if (!sites.length) { wrap.innerHTML = `<div class="empty-state">No browser activity recorded for this day yet.</div>`; return; }
  const max = sites[0].seconds || 1;
  for (const s of sites) {
    const subs = s.subpages || [];
    const row = document.createElement("div");
    row.className = "row";
    row.appendChild(iconEl("site", s.domain, s.domain, categoryColor(s.category)));
    const meta = document.createElement("div");
    meta.className = "meta";
    const caret = subs.length ? `<span class="caret" aria-label="Show pages">›</span>` : "";
    meta.innerHTML = `<div class="nm">${caret}<span>${s.domain}</span>${chip(s.category)} <span class="val">· ${s.sessions} visits</span></div>` +
      `<div class="bar" style="width:${Math.max(4, s.seconds / max * 100)}%;background:${categoryColor(s.category || "Uncategorised")}"></div>`;
    maybeTag(meta.querySelector(".nm"), "site", s.domain, s.domain, s.tags, s.category);
    row.appendChild(meta);
    const val = document.createElement("div");
    val.className = "val"; val.textContent = fmt(s.seconds);
    row.appendChild(val);
    row.onclick = () => openDetail("site", s.domain, s.domain, s.category);
    wrap.appendChild(row);

    if (subs.length) {
      // Inline, collapsed list of this domain's pages/videos (≥10 min). Each page can be
      // re-categorised on its own; clicking the caret toggles the list, clicking a page's
      // category chip opens the picker for that page only (type "page" override).
      const sublist = document.createElement("div");
      sublist.className = "sublist"; sublist.hidden = true;
      for (const sp of subs) {
        const sr = document.createElement("div");
        sr.className = "subrow";
        sr.innerHTML = `<div class="nm" title="${esc(sp.page)}">${esc(sp.label)}</div>`;
        const cat = document.createElement("button");
        cat.className = "catedit sm";
        cat.innerHTML = `<span class="dot" style="background:${categoryColor(sp.category)}"></span>${sp.category}`;
        cat.onclick = (e) => { e.stopPropagation(); openCatMenu(e.currentTarget, "page", sp.page, sp.label); };
        sr.appendChild(cat);
        maybeTag(sr, "page", sp.page, sp.label, sp.tags, sp.category);
        const sv = document.createElement("div");
        sv.className = "val"; sv.textContent = fmt(sp.seconds);
        sr.appendChild(sv);
        sublist.appendChild(sr);
      }
      wrap.appendChild(sublist);
      const c = row.querySelector(".caret");
      const toggle = (e) => {
        e.stopPropagation();
        sublist.hidden = !sublist.hidden;
        c.classList.toggle("open", !sublist.hidden);
      };
      c.onclick = toggle;
    }
  }
}

// --- Week trend ---
async function loadTrend(date) {
  const t = await getJSON(`/api/trend?date=${date}`);
  destroy("trend");
  const datasets = t.categories.map((c) => ({
    label: c,
    data: t.days.map((d) => +((d.categories[c] || 0) / 60).toFixed(1)),
    backgroundColor: categoryColor(c), borderRadius: 4, borderWidth: 0,
  }));
  charts.trend = new Chart(document.getElementById("trend"), {
    type: "bar",
    data: { labels: t.days.map((d) => d.date.slice(5)), datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: "#f5f5f7", boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: { callbacks: { label: (x) => `${x.dataset.label}: ${fmt(x.raw * 60)}` } },
      },
      scales: {
        x: { stacked: true, grid: { display: false } },
        y: { stacked: true, title: { display: true, text: "minutes" }, grid: { color: "rgba(255,255,255,0.05)" } },
      },
    },
  });
}

// --- Month calendar heatmap ---
async function loadCalendar(date) {
  const month = date.slice(0, 7);
  const data = await getJSON(`/api/calendar?month=${month}`);
  const cal = document.getElementById("calendar");
  document.getElementById("month-title").textContent =
    new Date(date + "T00:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" });
  cal.innerHTML = "";
  const dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  for (const d of dows) cal.innerHTML += `<div class="dow">${d}</div>`;
  if (!data.days || !data.days.length) return;
  const max = Math.max(...data.days.map((d) => d.total), 1);
  // Leading blanks (Mon=0).
  const first = new Date(data.days[0].date + "T00:00:00");
  const lead = (first.getDay() + 6) % 7;
  for (let i = 0; i < lead; i++) cal.innerHTML += `<div class="cell empty"></div>`;
  const today = new Date().toISOString().slice(0, 10);
  for (const d of data.days) {
    if (d.date > today) { cal.innerHTML += `<div class="cell empty"></div>`; continue; }
    const cell = document.createElement("div");
    cell.className = "cell";
    const a = d.total ? 0.12 + 0.78 * (d.total / max) : 0;
    cell.style.background = d.total ? `rgba(48,209,88,${a.toFixed(2)})` : "rgba(255,255,255,0.03)";
    cell.innerHTML = `<div class="d">${+d.date.slice(8)}</div><div class="t">${d.total ? fmt(d.total) : ""}</div>`;
    cell.onmousemove = (e) => showTip(`<b>${d.date}</b><br><span class="sub">Total ${fmt(d.total)} · Focus ${fmt(d.focus)}</span>`, e);
    cell.onmouseleave = hideTip;
    cell.onclick = () => { setView("day"); setDate(d.date); };
    cal.appendChild(cell);
  }
}

// --- Drill-down drawer ---
async function openDetail(kind, key, name, category) {
  const d = await getJSON(`/api/detail?kind=${kind}&key=${encodeURIComponent(key)}&date=${currentDate}`);
  const drawer = document.getElementById("drawer");
  const avg = d.history.length ? d.history.reduce((a, h) => a + h.seconds, 0) / d.history.length : 0;
  drawer.innerHTML =
    `<div class="drawer-head">
       <span class="ico" id="d-ico"></span>
       <div><div class="nm">${name}</div>
            <button class="catedit" id="d-cat" title="Change category — applies from now on">
              <span class="dot" style="background:${categoryColor(category)}"></span>${category || "Uncategorised"} ▾
            </button></div>
       <button class="hidebtn" id="d-hide" title="Hide from tracking — removes it from all views (undo in the Hidden panel)">Hide</button>
       <button class="close" id="d-close">✕</button>
     </div>
     <div class="stat-line">
       <div class="s"><b>${fmt(d.today_seconds)}</b><span>today</span></div>
       <div class="s"><b>${fmt(avg)}</b><span>${d.history.length}-day avg</span></div>
       <div class="s"><b>${d.sessions}</b><span>${kind === "site" ? "visits" : "sessions"}</span></div>
     </div>
     <h3>Last ${d.history.length} days</h3>
     <div class="chart-sm"><canvas id="d-history"></canvas></div>
     <h3>Time of day (today)</h3>
     <div class="chart-sm"><canvas id="d-hourly"></canvas></div>`;
  document.getElementById("d-ico").replaceWith(
    Object.assign(iconEl(kind, key, name, categoryColor(category)), { className: "ico" })
  );
  document.getElementById("d-close").onclick = closeDrawer;
  document.getElementById("d-cat").onclick = (e) => { e.stopPropagation(); openCatMenu(e.currentTarget, kind, key, name); };
  document.getElementById("d-hide").onclick = () => setIgnore(kind, key, true);
  const col = categoryColor(category);
  destroy("d-history"); destroy("d-hourly");
  charts["d-history"] = new Chart(document.getElementById("d-history"), {
    type: "line",
    data: {
      labels: d.history.map((h) => h.date.slice(5)),
      datasets: [{ data: d.history.map((h) => +(h.seconds / 60).toFixed(1)), borderColor: col,
        backgroundColor: col + "22", fill: true, tension: 0.3, pointRadius: 2 }],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (x) => fmt(x.raw * 60) } } },
      scales: { x: { grid: { display: false } }, y: { title: { display: true, text: "min" }, grid: { color: "rgba(255,255,255,0.05)" } } } },
  });
  charts["d-hourly"] = new Chart(document.getElementById("d-hourly"), {
    type: "bar",
    data: {
      labels: Array.from({ length: 24 }, (_, h) => h),
      datasets: [{ data: d.hourly.map((s) => +(s / 60).toFixed(1)), backgroundColor: col, borderRadius: 3 }],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { title: (x) => `${x[0].label}:00`, label: (x) => fmt(x.raw * 60) } } },
      scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } }, y: { title: { display: true, text: "min" }, grid: { color: "rgba(255,255,255,0.05)" } } } },
  });
  document.getElementById("scrim").classList.add("open");
  drawer.classList.add("open");
}
function closeDrawer() {
  document.getElementById("drawer").classList.remove("open");
  document.getElementById("scrim").classList.remove("open");
}

// --- Settings: Focus / Leisure category roles ---
async function openSettings() {
  const [cats, roles] = await Promise.all([
    getJSON("/api/categories"),
    getJSON("/api/category_roles"),
  ]);
  const focus = new Set(roles.focus), leisure = new Set(roles.leisure);
  const roleOf = (c) => (focus.has(c) ? "focus" : leisure.has(c) ? "leisure" : "neither");
  const drawer = document.getElementById("drawer");
  drawer.innerHTML =
    `<div class="drawer-head">
       <div><div class="nm">Category roles</div></div>
       <button class="close" id="s-close">✕</button>
     </div>
     <p class="roles-help">Edit a name to rename a category everywhere (past and future data).
       Focus counts toward the green ring and deep-work blocks; Leisure toward the orange ring;
       Neither is excluded from both. New categories start as Neither.
       <b>Tags</b> enables the +tag control for that category (for broad categories like Coding
       or Leisure that aren't descriptive enough on their own).</p>
     <div class="roles-list">${cats.map((c) => `
       <div class="roles-row" data-cat="${c}">
         <input class="cat-name" value="${c}" data-orig="${c}" spellcheck="false">
         <div class="segmented sm role-seg">
           <button data-role="focus">Focus</button>
           <button data-role="leisure">Leisure</button>
           <button data-role="neither">Neither</button>
         </div>
         <label class="tag-toggle"><input type="checkbox" class="tag-chk" ${TAG_CATS.has(c) ? "checked" : ""}>Tags</label>
         <button class="del-cat" title="Delete category (its items become Uncategorised)">🗑</button>
       </div>`).join("")}</div>
     <button class="save-btn" id="s-save">Save</button>`;
  for (const row of drawer.querySelectorAll(".roles-row")) {
    const cur = roleOf(row.dataset.cat);
    for (const b of row.querySelectorAll("button")) {
      b.classList.toggle("active", b.dataset.role === cur);
      b.onclick = () => { for (const x of row.querySelectorAll("button")) x.classList.toggle("active", x === b); };
    }
  }
  // Delete a category immediately (standalone from Save): its rule, lane, roles and tags are
  // removed and pinned items are rewritten to Uncategorised. Confirm first — irreversible.
  for (const btn of drawer.querySelectorAll(".del-cat")) {
    btn.onclick = async () => {
      const name = btn.closest(".roles-row").dataset.cat;
      if (!confirm(`Delete category "${name}"? Its apps/sites become Uncategorised. This can't be undone.`)) return;
      const res = await fetch("/api/delete_category", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) { alert(`Delete "${name}" failed: ${await res.text()}`); return; }
      CATS = null;           // category picker cache is now stale
      await openSettings();  // re-render the drawer without the deleted category
      await load(currentDate);
    };
  }
  document.getElementById("s-close").onclick = closeDrawer;
  document.getElementById("s-save").onclick = async () => {
    const rows = [...drawer.querySelectorAll(".roles-row")];
    // 1. Apply renames first (one call each) so the role lists below use the new names.
    for (const row of rows) {
      const inp = row.querySelector(".cat-name");
      const orig = inp.dataset.orig, name = inp.value.trim() || orig;
      if (name === orig) continue;
      const res = await fetch("/api/rename_category", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old: orig, new: name }),
      });
      if (!res.ok) { alert(`Rename "${orig}" → "${name}" failed: ${await res.text()}`); return; }
      inp.dataset.orig = name;  // so a second save doesn't re-rename
    }
    // 2. Save Focus/Leisure roles using the (possibly renamed) names.
    const f = [], l = [];
    for (const row of rows) {
      const name = row.querySelector(".cat-name").value.trim() || row.dataset.cat;
      const active = row.querySelector("button.active").dataset.role;
      if (active === "focus") f.push(name);
      else if (active === "leisure") l.push(name);
    }
    await fetch("/api/category_roles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ focus: f, leisure: l }),
    });
    // 3. Save which categories have tagging enabled (using the possibly-renamed names).
    const tc = [];
    for (const row of rows) {
      const name = row.querySelector(".cat-name").value.trim() || row.dataset.cat;
      if (row.querySelector(".tag-chk").checked) tc.push(name);
    }
    await fetch("/api/tag_categories", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ categories: tc }),
    });
    closeDrawer();
    await load(currentDate);  // recompute rings/deep-work with the new roles + names
  };
  document.getElementById("scrim").classList.add("open");
  drawer.classList.add("open");
}

// --- Category picker (time-aware: applies from now on) ---
let CATS = null;
function closeCatMenu() { document.querySelector(".catmenu")?.remove(); }
async function openCatMenu(anchor, kind, key, name) {
  closeCatMenu();
  if (!CATS) CATS = await getJSON("/api/categories");
  const menu = document.createElement("div");
  menu.className = "catmenu";
  for (const c of CATS) {
    const it = document.createElement("div");
    it.className = "catmenu-it";
    it.innerHTML = `<span class="dot" style="background:${categoryColor(c)}"></span>${c}`;
    it.onclick = () => applyCategory(kind, key, name, c);
    menu.appendChild(it);
  }
  const nw = document.createElement("div");
  nw.className = "catmenu-it new";
  nw.textContent = "+ New category…";
  nw.onclick = () => { const v = prompt("New category name"); if (v && v.trim()) applyCategory(kind, key, name, v.trim()); };
  menu.appendChild(nw);
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 12) + "px";
  menu.style.top = (r.bottom + 6) + "px";
  setTimeout(() => document.addEventListener("click", closeCatMenu, { once: true }), 0);
}
async function applyCategory(kind, key, name, category) {
  closeCatMenu();
  await fetch("/api/categorize", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: kind, key, category }),  // "app" | "site" | "page" | "dir"
  });
  CATS = null;                 // a new category may have been created
  await load(currentDate);     // refresh totals/lists with the new mapping
  if (kind !== "page" && kind !== "dir") openDetail(kind, key, name, category);  // pages/dirs have no detail drawer
}

// --- Tag picker (multi-select, orthogonal to category; time-aware like overrides) ---
let TAGS = null;
let TAG_CATS = new Set();  // categories for which tagging is enabled (from settings)
function closeTagMenu() { document.querySelector(".tagmenu")?.remove(); }
// Append a +tag control only when the item's category is tag-enabled (settings → Tags).
function maybeTag(parent, kind, key, name, tags, category) {
  if (category && TAG_CATS.has(category)) parent.appendChild(tagControl(kind, key, name, tags));
}
// A small inline control for a row: shows current tags as pills, opens the tag menu.
function tagControl(kind, key, name, tags) {
  const b = document.createElement("button");
  b.className = "tagedit sm";
  b.innerHTML = (tags && tags.length)
    ? tags.map((t) => `<span class="tagpill" style="border-color:${categoryColor(t)};color:${categoryColor(t)}">${esc(t)}</span>`).join("")
    : `<span class="tagadd">+ tag</span>`;
  b.onclick = (e) => { e.stopPropagation(); openTagMenu(e.currentTarget, kind, key, name, tags || []); };
  return b;
}
async function openTagMenu(anchor, kind, key, name, current) {
  closeTagMenu();
  if (!TAGS) TAGS = await getJSON("/api/tags");
  const cur = new Set(current);
  const menu = document.createElement("div");
  menu.className = "catmenu tagmenu";
  for (const t of [...new Set([...TAGS, ...current])]) {
    const on = cur.has(t);
    const it = document.createElement("div");
    it.className = "catmenu-it" + (on ? " on" : "");
    it.innerHTML = `<span class="dot" style="background:${categoryColor(t)}"></span>${esc(t)}<span class="check">${on ? "✓" : ""}</span>`;
    it.onclick = () => applyTag(kind, key, t, !on);  // toggle
    menu.appendChild(it);
  }
  const nw = document.createElement("div");
  nw.className = "catmenu-it new";
  nw.textContent = "+ New tag…";
  nw.onclick = () => { const v = prompt("New tag name"); if (v && v.trim()) applyTag(kind, key, v.trim(), true); };
  menu.appendChild(nw);
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 12) + "px";
  menu.style.top = (r.bottom + 6) + "px";
  setTimeout(() => document.addEventListener("click", closeTagMenu, { once: true }), 0);
}
async function applyTag(kind, key, tag, on) {
  closeTagMenu();
  await fetch("/api/tag", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: kind, key, tag, on }),
  });
  TAGS = null;                 // a new tag may have been created
  await load(currentDate);     // refresh rings/breakdown/pills
}

// --- View + date control ---
function setView(v) {
  view = v;
  for (const b of document.querySelectorAll("#views button")) b.classList.toggle("active", b.dataset.view === v);
  document.getElementById("day-view").hidden = v !== "day";
  document.getElementById("week-view").hidden = v !== "week";
  document.getElementById("month-view").hidden = v !== "month";
  if (v === "week") loadTrend(currentDate);
  if (v === "month") loadCalendar(currentDate);
}
function setDate(iso) {
  currentDate = iso;
  const sel = document.getElementById("date");
  if ([...sel.options].some((o) => o.value === iso)) sel.value = iso;
  load(iso);
}

async function load(date) {
  currentDate = date;
  const s = await getJSON(`/api/summary?date=${date}`);
  TLP = s.timeline_prefs || { order: [], shown: [], hidden: [] };
  TAG_CATS = new Set(s.tag_categories || []);  // gate the +tag control by category
  CATS = await getJSON("/api/categories");  // full list incl. user-created — for the chip row
  rebuildCategoryColors(CATS);  // collision-free colours over the full set, before any render
  renderHero(s);
  renderSwim(s.sessions, s.day_start);
  renderCategories(s.categories, s.cat_tags);
  renderTags(s);
  renderApps(s.apps);
  renderBrowsing(s.browsing);
  renderWebsites(s.websites);
  renderHidden();
  if (view === "week") loadTrend(date);
  if (view === "month") loadCalendar(date);
}

// --- Hidden (ignored) apps/sites ---
async function setIgnore(kind, key, on) {
  await fetch("/api/ignore", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: kind === "site" ? "site" : "app", key, on }),
  });
  closeDrawer();
  await load(currentDate);  // re-pull totals with the item now hidden/restored
}
async function renderHidden() {
  const items = await getJSON("/api/ignored");
  const card = document.getElementById("hidden-card");
  const list = document.getElementById("hidden-list");
  card.style.display = items.length ? "" : "none";
  list.innerHTML = "";
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "row";
    row.appendChild(Object.assign(iconEl(it.type, it.key, it.name, "#888"), { className: "ico" }));
    const nm = document.createElement("div");
    nm.className = "nm"; nm.textContent = it.name;
    const btn = document.createElement("button");
    btn.className = "hidebtn"; btn.textContent = "Unhide";
    btn.onclick = () => setIgnore(it.type, it.key, false);
    row.append(nm, btn);
    list.appendChild(row);
  }
}

async function init() {
  const dates = await getJSON("/api/dates");
  const sel = document.getElementById("date");
  sel.innerHTML = dates.map((d) => `<option value="${d}">${d}</option>`).join("");
  sel.onchange = () => setDate(sel.value);
  document.getElementById("prev").onclick = () =>
    setDate(view === "month" ? addMonths(currentDate, -1) : addDays(currentDate, view === "week" ? -7 : -1));
  document.getElementById("next").onclick = () =>
    setDate(view === "month" ? addMonths(currentDate, 1) : addDays(currentDate, view === "week" ? 7 : 1));
  // Hard refresh: full page reload so the page re-pulls the latest DB state.
  document.getElementById("refresh").onclick = () => location.reload();
  document.getElementById("settings").onclick = openSettings;
  for (const b of document.querySelectorAll("#views button")) b.onclick = () => setView(b.dataset.view);
  for (const b of document.querySelectorAll("#app-filter button")) b.onclick = () => {
    APP_FILTER = b.dataset.src;
    for (const x of document.querySelectorAll("#app-filter button")) x.classList.toggle("active", x === b);
    applyAppFilter();
  };
  document.getElementById("scrim").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  if (dates.length) { currentDate = dates[0]; sel.value = dates[0]; load(dates[0]); }
}

init();
