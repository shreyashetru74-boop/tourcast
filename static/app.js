/* Tourcast frontend: streams the agent's events and draws the plan. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const TOOL_LABELS = {
  resolve_artists: "Looking up the artists in Qloo",
  find_similar_artists: "Finding artists with overlapping fans",
  map_fan_cities: "Ranking every market for this sound",
  plan_route: "Choosing cities and ordering the route",
  city_deep_dive: "Zooming into",
  fan_crossover: "Finding what else these fans love",
  finalize_plan: "Writing the tour plan",
};

let map, heatLayer, routeLayer, spotLayer, hiddenLayer;
let plan = {}, selectedCity = null, editable = true, busy = false, nextFresh = false;

// ---------------------------------------------------------------- setup
async function init() {
  map = L.map("map", { zoomControl: true, worldCopyJump: true }).setView([30, -40], 2);
  const esri = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/";
  L.tileLayer(esri + "World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Tiles &copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors", maxZoom: 16,
  }).addTo(map);
  L.tileLayer(esri + "World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 16, pane: "overlayPane", opacity: 0.85,
  }).addTo(map);
  hiddenLayer = L.layerGroup().addTo(map);
  routeLayer = L.layerGroup().addTo(map);
  spotLayer = L.layerGroup().addTo(map);

  const cfg = await fetch("/api/config").then((r) => r.json()).catch(() => null);
  if (cfg) {
    $("country").innerHTML = cfg.countries.map((c) => `<option>${esc(c)}</option>`).join("");
    $("age").innerHTML = Object.entries(cfg.ages)
      .map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
    window.APP_MOCK = cfg.mock;
    if (cfg.mock) {
      $("dataBadge").hidden = false;
      $("dataBadge").textContent = "Demo data: no Qloo key set";
    }
  }
  document.querySelectorAll(".ex").forEach((b) => b.addEventListener("click", () => {
    $("artists").value = b.dataset.a;
    $("artistLabel").value = b.dataset.l || "";
    $("country").value = b.dataset.c;
    $("home").value = b.dataset.h;
    $("planForm").requestSubmit();
  }));
  $("planForm").addEventListener("submit", onSubmit);
  $("copyBtn").addEventListener("click", copyPlan);
  $("shareBtn").addEventListener("click", copyLink);
  $("posterBtn").addEventListener("click", showPoster);
  $("posterClose").addEventListener("click", () => $("posterDlg").close());
  $("printBtn").addEventListener("click", () => window.print());
  $("blendRange").addEventListener("input", updateBlendLabel);
  $("blendRange").addEventListener("change", applyBlend);

  loadEvidence();
  const shared = location.pathname.match(/^\/p\/([0-9a-f]{12})$/);
  if (shared) loadShared(shared[1]);
}

// ---------------------------------------------------------------- run
async function onSubmit(e) {
  e.preventDefault();
  if (busy) return;
  const artists = $("artists").value.split(",").map((s) => s.trim()).filter(Boolean).slice(0, 4);
  if (!artists.length) return;
  const body = {
    artists, artist_label: $("artistLabel").value.trim(), country: $("country").value,
    home_city: $("home").value.trim(), age: $("age").value,
    num_shows: Math.max(2, Math.min(10, parseInt($("shows").value, 10) || 5)),
    fresh: nextFresh,
  };
  nextFresh = false;
  resetUI();
  busy = true;
  $("goBtn").disabled = true;
  $("goBtn").textContent = "Planning…";

  try {
    const res = await fetch("/api/plan", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`The server returned ${res.status}. Check the inputs and try again.`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) handle(JSON.parse(line));
      }
    }
  } catch (err) {
    addTrace("note", `The plan couldn't finish: ${err.message}`);
  } finally {
    busy = false;
    $("goBtn").disabled = false;
    $("goBtn").textContent = "Plan my tour";
  }
}

function resetUI() {
  plan = {}; selectedCity = null; editable = true;
  $("trace").innerHTML = "";
  ["results", "crossSection", "compareSection", "provSection", "hiddenSection", "insight", "sharedNote"]
    .forEach((id) => ($(id).hidden = true));
  $("cmpGrid").innerHTML = ""; $("cmpSummary").textContent = ""; $("editStatus").textContent = "";
  routeLayer.clearLayers(); spotLayer.clearLayers(); hiddenLayer.clearLayers();
  if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
}

function handle({ event, data }) {
  if (event === "trace") return onTrace(data);
  if (event === "state") {
    Object.assign(plan, data);
    if (data.heat) drawHeat(data.heat, true);
    // Show the tour as soon as the route exists; notes fill in when the agent finishes.
    if (data.route?.stops?.length) renderTour();
    else if (data.route) drawRoute(data.route, true);
    return;
  }
  if (event === "city") {
    plan.city_details = { ...(plan.city_details || {}), [data.city]: data };
    if (data.city === selectedCity) selectCity(data.city, false);
    return;
  }
  if (event === "done") {
    plan = data;
    if (plan.missing?.length) addTrace("note", `Not found in Qloo: ${plan.missing.join(", ")}.`);
    if (plan.replayed) {
      addTrace("note", `Loaded instantly. Originally planned in ${plan.seconds}s` +
        (plan.agent === "llm" ? " by the AI agent." : "."));
      const li = document.createElement("li");
      li.className = "note";
      li.innerHTML = '<button type="button" class="relive">Plan it live again</button>';
      li.querySelector("button").addEventListener("click", () => { nextFresh = true; $("planForm").requestSubmit(); });
      $("trace").appendChild(li);
    } else {
      addTrace("note", `Done in ${plan.seconds}s using ${plan.qloo_calls} Qloo requests` +
        (plan.agent === "llm" ? ", planned by the AI agent." : "."));
    }
    if (plan.plan_id && plan.route?.stops?.length) history.replaceState(null, "", `/p/${plan.plan_id}`);
    return renderAll();
  }
  if (event === "error") addTrace("note", data.message);
}

async function loadShared(id) {
  try {
    const r = await fetch(`/api/plan/${id}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "This plan link has expired.");
    plan = data;
    editable = data.editable !== false;
    $("artists").value = (plan.seeds || []).map((s) => s.name).join(", ");
    $("artistLabel").value = plan.artist_label || "";
    $("country").value = plan.country;
    $("home").value = plan.home_city || "";
    $("trace").innerHTML = "";
    addTrace("note", "Showing a saved plan. Plan it again for fresh results.");
    drawHeat(plan.heat || [], true);
    renderAll();
    if (!editable) {
      $("sharedNote").hidden = false;
      $("sharedNote").textContent = "This is a saved copy. To edit or re-blend it, plan the tour again.";
    }
  } catch (err) {
    $("trace").innerHTML = "";
    addTrace("note", `${err.message} Enter an artist to plan a new tour.`);
    history.replaceState(null, "", "/");
  }
}

// ---------------------------------------------------------------- trace
function onTrace(d) {
  if (d.kind === "tool") {
    let label = TOOL_LABELS[d.name] || d.name;
    if (d.name === "city_deep_dive") label += ` ${d.args.city}`;
    addTrace("tool", label, d.id);
  } else if (d.kind === "result") {
    const li = [...$("trace").querySelectorAll("li.tool")].find((x) => x.dataset.id === d.id);
    if (li && d.qloo_calls) li.insertAdjacentHTML("beforeend",
      ` <span class="q">${d.qloo_calls} Qloo call${d.qloo_calls > 1 ? "s" : ""}</span>`);
  } else {
    addTrace(d.kind === "think" ? "think" : "note", d.text);
  }
}
function addTrace(cls, text, id) {
  const li = document.createElement("li");
  li.className = cls;
  if (id) li.dataset.id = id;
  li.textContent = text;
  $("trace").appendChild(li);
  $("trace").scrollTop = $("trace").scrollHeight;
}

// ---------------------------------------------------------------- map
function drawHeat(points, fit) {
  $("mapEmpty").hidden = true;
  $("legend").hidden = false;
  if (heatLayer) map.removeLayer(heatLayer);
  if (!points.length) return;
  const weighted = points.map(([lat, lng, a]) => [lat, lng, Math.pow(a, 3)]);
  heatLayer = L.heatLayer(weighted, {
    radius: 16, blur: 18, maxZoom: 9, minOpacity: 0.25,
    gradient: { 0.2: "#2E2A6B", 0.45: "#5B2A86", 0.7: "#FF5E8A", 0.9: "#FFB547", 1: "#FFE3A6" },
  }).addTo(map);
  if (fit) map.fitBounds(coreBounds(points), { padding: [30, 30] });
}

// Fit to where most fans are, ignoring far-off outliers (Qloo's US heatmap
// includes Guam, Puerto Rico and Alaska, which would zoom the map out to the Pacific).
function coreBounds(points) {
  const q = (arr, f) => arr[Math.min(arr.length - 1, Math.max(0, Math.floor(f * (arr.length - 1))))];
  const lats = points.map((p) => p[0]).sort((a, b) => a - b);
  const lngs = points.map((p) => p[1]).sort((a, b) => a - b);
  return L.latLngBounds([q(lats, 0.03), q(lngs, 0.03)], [q(lats, 0.97), q(lngs, 0.97)]);
}

function drawRoute(route, fit) {
  routeLayer.clearLayers();
  const stops = route.stops || [];
  const path = [];
  if (route.home) {
    path.push([route.home.lat, route.home.lng]);
    L.marker([route.home.lat, route.home.lng], {
      icon: L.divIcon({ className: "", html: '<div class="home-pin"></div>', iconSize: [14, 14] }),
      title: `Home: ${route.home.city}`,
    }).addTo(routeLayer);
  }
  stops.forEach((s, i) => {
    path.push([s.lat, s.lng]);
    L.marker([s.lat, s.lng], {
      icon: L.divIcon({ className: "", html: `<div class="stop-pin">${i + 1}</div>`, iconSize: [28, 28] }),
      title: s.city,
    }).on("click", () => selectCity(s.city)).addTo(routeLayer);
  });
  if (path.length > 1) {
    L.polyline(path, { color: "#FFB547", weight: 3, opacity: 0.9, dashArray: "6 6" }).addTo(routeLayer);
  }
  if (fit && path.length) map.fitBounds(L.latLngBounds(path), { padding: [50, 50], maxZoom: 6 });
}

function drawHidden(list) {
  hiddenLayer.clearLayers();
  (list || []).forEach((h) => {
    L.circleMarker([h.lat, h.lng], {
      radius: 15, color: "#FF5E8A", weight: 2, dashArray: "3 4", fill: false,
    }).bindTooltip(`Hidden market: ${h.city}, #${h.fit_rank} for this sound, #${h.size_rank} by size`)
      .addTo(hiddenLayer);
  });
}

// ---------------------------------------------------------------- results
const rankText = (c) => (c?.fit_rank ? `#${c.fit_rank}` : `${c?.match ?? ""}%`);

function renderAll(keepCity) {
  renderTour(keepCity);
  renderDecisions();
  renderInsight();
  renderHidden();
  renderBlender();
  renderCompare();
  renderCross();
  renderProvenance();
}

function renderTour(keepCity) {
  const stops = plan.route?.stops || [];
  plan._notes = Object.fromEntries((plan.final?.city_notes || []).map((n) => [n.city, n]));
  $("headline").textContent = plan.final?.headline || "";
  $("results").hidden = false;

  if (!stops.length) {
    $("tourList").innerHTML = "";
    $("altWrap").hidden = true;
    $("tourTools").hidden = true;
    $("blender").hidden = true;
    $("cityPanel").innerHTML = `<p class="empty-note">${esc(plan.final?.headline ||
      "No route could be planned. Try different artists or another country.")}</p>`;
    return;
  }
  const ready = !!plan.plan_id;
  $("tourTools").hidden = !ready;
  if (!ready) $("headline").textContent = "Route ready. The agent is writing notes for each city…";
  $("tourList").innerHTML = stops.map((s) => {
    const [name, region] = s.city.split(",").map((x) => x.trim());
    return `
    <li>
      <button type="button" class="stop" data-city="${esc(s.city)}" aria-pressed="false">
        <span class="city">${esc(name)}</span>
        <span class="pct">${region ? `<span class="region">${esc(region)}</span> ` : ""}${rankText(s)}${
          s.top_pct ? `<span class="toppct"> top ${s.top_pct}%</span>` : ""}${
          s.hidden ? '<span class="hm">hidden market</span>' : ""}</span>
      </button>
      ${editable && ready ? `<button type="button" class="remove" data-remove="${esc(s.city)}"
        aria-label="Remove ${esc(s.city)} from the tour" title="Remove from tour"
        ${stops.length <= 2 ? "disabled" : ""}>×</button>` : ""}
    </li>`;
  }).join("");
  $("tourList").querySelectorAll(".stop").forEach((b) =>
    b.addEventListener("click", () => selectCity(b.dataset.city)));
  $("tourList").querySelectorAll(".remove").forEach((b) =>
    b.addEventListener("click", () => editStops(stops.map((s) => s.city).filter((c) => c !== b.dataset.remove))));

  const alts = editable && ready ? plan.alternatives || [] : [];
  $("altWrap").hidden = !alts.length;
  $("alts").innerHTML = alts.map((a) =>
    `<button type="button" class="alt" data-add="${esc(a.city)}" ${stops.length >= 10 ? "disabled" : ""}>
      + ${esc(a.city)} <span>${rankText(a)}</span></button>`).join("");
  $("alts").querySelectorAll(".alt").forEach((b) =>
    b.addEventListener("click", () => editStops([...stops.map((s) => s.city), b.dataset.add])));

  const markets = plan.markets ? ` Ranks are Fan Fit among ${plan.markets} markets Qloo scored for this sound.` : "";
  $("totalKm").textContent = `${stops.length} shows, about ${plan.route.total_km.toLocaleString()} km` +
    (plan.route.home ? ` from ${plan.route.home.city}` : "") + "." + markets;
  const first = keepCity && stops.some((s) => s.city === keepCity) ? keepCity
    : selectedCity && stops.some((s) => s.city === selectedCity) ? selectedCity : stops[0].city;
  drawRoute(plan.route, !keepCity);
  drawHidden(plan.hidden_markets);
  selectCity(first, false);
}

function renderInsight() {
  const ins = plan.compare?.insight;
  if (!ins) { $("insight").hidden = true; return; }
  const short = (c) => c.split(",")[0];
  $("insight").innerHTML =
    `<span class="skip">Skip ${esc(short(ins.skip.city))}</span> <span class="rk">#${ins.skip.rank} of ${ins.markets} for this sound</span>` +
    `<span class="sep" aria-hidden="true">/</span>` +
    `<span class="play">Play ${esc(short(ins.play.city))}</span> <span class="rk">#${ins.play.fit_rank}, though only #${ins.play.size_rank} by size</span>`;
  $("insight").hidden = false;
}

function renderHidden() {
  const list = plan.hidden_markets || [];
  if (!list.length) { $("hiddenSection").hidden = true; return; }
  const stops = (plan.route?.stops || []).map((s) => s.city);
  $("hiddenList").innerHTML = list.map((h) => `
    <li class="hcard">
      <p class="hcity">${esc(h.city)}</p>
      <p class="hstats"><b>#${h.fit_rank}</b> for this sound<br><span>#${h.size_rank} by size</span></p>
      ${h.on_tour ? '<p class="hon">On your tour</p>'
        : editable ? `<button type="button" class="hadd" data-add="${esc(h.city)}" ${stops.length >= 10 ? "disabled" : ""}>Add to tour</button>` : ""}
    </li>`).join("");
  $("hiddenList").querySelectorAll(".hadd").forEach((b) =>
    b.addEventListener("click", () => editStops([...stops, b.dataset.add])));
  $("hiddenSection").hidden = false;
}

function renderDecisions() {
  const list = plan.final?.decisions || [];
  if (!list.length || !plan.route?.stops?.length) { $("decisions").hidden = true; return; }
  const byAgent = list.some((d) => d.by === "agent");
  $("decisionsTitle").textContent = byAgent ? "How the agent decided" : "How the planner decided";
  $("decisionList").innerHTML = list.map((d) =>
    `<li class="${d.action}"><b>${d.action === "picked" ? "Picked" : "Skipped"} ${esc(d.city.split(",")[0])}.</b> ${esc(d.reason)}</li>`).join("");
  $("decisions").hidden = false;
}

// ---------------------------------------------------------------- blind test
let evidence = null;
async function loadEvidence() {
  try {
    const r = await fetch("/static/evidence.json", { cache: "no-cache" });
    if (r.ok) evidence = await r.json();
  } catch {}
}
function renderBlind() {
  const e = evidence;
  // Demo-data results are never shown on a live site.
  if (!e?.summary?.artists || (e.summary.demo_data && !window.APP_MOCK)) { $("blind").hidden = true; return; }
  const s = e.summary;
  $("blindSummary").innerHTML =
    `Planned blind for <b>${s.artists} artists</b> from their sounds-like acts only, Tourcast's cities ranked a median ` +
    `<b>#${s.tourcast_median}</b> in each artist's real fan map. A biggest-cities tour: <b>#${s.biggest_median}</b>. ` +
    `A random tour: <b>#${s.random_median}</b>. ${s.top20_hits} of ${s.top20_total} blind picks were in the artist's own top 20 markets.`;
  $("blindTable").innerHTML = `<thead><tr><th>Artist</th><th>Planned from</th><th>Tourcast</th><th>Biggest cities</th><th>Random</th></tr></thead><tbody>` +
    e.rows.map((r) => `<tr><td>${esc(r.artist)} <span class="sub">(${esc(r.country)})</span></td><td>${esc(r.planned_from.join(", "))}</td>` +
      `<td><b>#${r.tourcast_median}</b></td><td>#${r.biggest_median}</td><td>#${r.random_median}</td></tr>`).join("") + "</tbody>";
  $("blind").hidden = false;
}

// ---------------------------------------------------------------- blender
function renderBlender() {
  const seeds = plan.seeds || [];
  if (!editable || seeds.length !== 2 || !plan.route?.stops?.length) { $("blender").hidden = true; return; }
  $("blendA").textContent = seeds[0].name;
  $("blendB").textContent = seeds[1].name;
  const w = plan.blend ? Math.round((plan.blend[1] / (plan.blend[0] + plan.blend[1])) * 100) : 50;
  $("blendRange").value = w;
  updateBlendLabel();
  $("blender").hidden = false;
}
function updateBlendLabel() {
  const v = +$("blendRange").value;
  const seeds = plan.seeds || [];
  $("blendValue").textContent = v === 50 ? "An even mix"
    : `${100 - v}% ${seeds[0]?.name ?? ""} / ${v}% ${seeds[1]?.name ?? ""}`;
}
async function applyBlend() {
  const v = +$("blendRange").value;
  await callEdit(`/api/plan/${plan.plan_id}/blend`, { weights: [100 - v, v] },
    "Re-ranking every market for this mix…", "Tour re-planned for this mix.", true);
}

// ---------------------------------------------------------------- editing
async function editStops(cities) {
  if (cities.length < 2) return;
  await callEdit(`/api/plan/${plan.plan_id}/stops`, { cities }, "Updating the route…", "Route updated.", false);
}

async function callEdit(url, body, working, doneText, redrawHeat) {
  if (!plan.plan_id || busy) return;
  busy = true;
  document.querySelectorAll("#tourList button, #alts button, .hadd, #blendRange").forEach((b) => (b.disabled = true));
  $("editStatus").textContent = working;
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `The server returned ${r.status}.`);
    plan = { ...plan, ...data };
    if (redrawHeat) drawHeat(plan.heat || [], false);
    renderAll(redrawHeat ? null : selectedCity);
    $("editStatus").textContent = doneText;
  } catch (err) {
    $("editStatus").textContent = err.message;
    renderAll(selectedCity);
  } finally {
    busy = false;
    $("blendRange").disabled = false;
  }
}

// ---------------------------------------------------------------- city panel
function bar(label, rank, markets) {
  if (!rank || !markets) return "";
  const pct = Math.max(3, Math.round((1 - (rank - 1) / markets) * 100));
  return `<div class="ev"><span class="evl">${label}</span>
    <span class="evbar"><span style="width:${pct}%"></span></span>
    <span class="evv">#${rank} of ${markets}</span></div>`;
}

function selectCity(city, zoom = true) {
  selectedCity = city;
  document.querySelectorAll("#tourList .stop").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.city === city)));
  const stop = (plan.route?.stops || []).find((s) => s.city === city) || {};
  const d = { ...stop, ...(plan.city_details?.[city] || {}) };
  const n = plan._notes?.[city] || {};
  const loading = !plan.plan_id && !plan.city_details?.[city];
  const co = (d.co_headliners || []).map((c) => `<div><span>${esc(c.name)}${
    c.rising ? '<span class="rising">rising</span>' : ""}</span><span>${c.local_match}% local fan strength</span></div>`).join("");
  const venues = (d.venues || []).map((v) => `<li>${esc(v.name)}</li>`).join("");

  $("cityPanel").innerHTML = `
    <h2>${esc(city)}${d.hidden ? ' <span class="hm big">hidden market</span>' : ""}</h2>
    <div class="evidence">
      ${bar("Fit for this sound", d.fit_rank, d.markets)}
      ${bar("Size", d.size_rank, d.markets)}
    </div>
    ${n.why ? `<p class="why">${esc(n.why)}</p>` : ""}
    ${n.promo ? `<p class="promo"><b>Promo idea:</b> ${esc(n.promo)}</p>` : ""}
    <h3>Share the bill with</h3>
    ${co ? `<div class="co">${co}</div>` : `<p class="empty-note">${loading ? "Loading…" : "No local co-headliner data for this city."}</p>`}
    <h3>Places these fans like</h3>
    ${venues ? `<ul class="chips">${venues}</ul>` : `<p class="empty-note">${loading ? "Loading…" : "Qloo has no place data for this city in the hackathon dataset."}</p>`}
    <p class="sub small">${(d.hotspots || []).length} fan hotspots marked on the map in pink.</p>
  `;

  spotLayer.clearLayers();
  (d.hotspots || []).forEach((h, i) => {
    L.marker([h.lat, h.lng], {
      icon: L.divIcon({ className: "", html: '<div class="spot-pin"></div>', iconSize: [14, 14] }),
      title: `Fan hotspot ${i + 1}`,
    }).bindPopup(`Fan hotspot ${i + 1}<br>${h.match}% affinity`).addTo(spotLayer);
  });
  if (zoom && d.hotspots?.length) {
    map.flyToBounds(L.latLngBounds(d.hotspots.map((h) => [h.lat, h.lng])).pad(1.2), { maxZoom: 12, duration: 0.8 });
  }
}

function renderCross() {
  const c = plan.crossover || {};
  const col = (title, items) => `<div><h3>${title}</h3><ul>${(items || []).slice(0, 6).map((e) =>
    `<li><span>${esc(e.name)}</span><span>${e.affinity != null ? Math.round(e.affinity * 100) + "%" : ""}</span></li>`
  ).join("") || '<li><span class="empty-note">No data</span></li>'}</ul></div>`;
  $("cross").innerHTML = col("Brands", c.brands) + col("Films", c.movies) + col("TV shows", c.tv_shows);
  $("crossSection").hidden = false;
}

// ---------------------------------------------------------------- comparison
function renderCompare() {
  renderBlind();
  const c = plan.compare;
  if (!c?.tourcast) { $("compareSection").hidden = true; return; }
  const cols = [["tourcast", "Tourcast"], ["biggest", "Biggest cities"], ["ai_only", "AI without Qloo"]]
    .filter(([k]) => c[k]);
  $("cmpGrid").style.setProperty("--cols", cols.length);
  $("cmpGrid").innerHTML = cols.map(([k, title]) => {
    const p = c[k];
    return `<div class="cmp-col ${k === "tourcast" ? "win" : ""}">
      <h3>${title}</h3>
      <p class="cmp-avg">${p.median_rank ? "#" + p.median_rank : "–"}<span>median Fan Fit rank</span></p>
      <ol>${p.rows.map((r) => `<li><span>${esc(r.city)}</span><span>${
        r.rank == null ? "no fan signal" : "#" + r.rank}</span></li>`).join("")}</ol>
    </div>`;
  }).join("");
  const t = c.tourcast, b = c.biggest;
  let text = `All three plans scored with the same Qloo heatmap across ${c.markets_scored} markets (lower rank is better). ` +
    `Tourcast's stops have a median rank of #${t.median_rank}; a biggest-cities tour, #${b.median_rank ?? "–"}.`;
  if (c.ai_only?.median_rank) text += ` The same AI picking cities without Qloo: #${c.ai_only.median_rank}.`;
  $("cmpSummary").textContent = text;
  $("compareSection").hidden = false;
}

// ---------------------------------------------------------------- export
function planAsText() {
  const stops = plan.route?.stops || [];
  const who = plan.artist_label || (plan.seeds || []).map((s) => s.name).join(", ");
  const lines = [`TOURCAST PLAN: ${who} (${plan.country})`, plan.final?.headline || "", ""];
  const ins = plan.compare?.insight;
  if (ins) lines.push(`Key finding: ${ins.play.city} ranks #${ins.play.fit_rank} of ${ins.markets} for this sound; ${ins.skip.city} ranks #${ins.skip.rank}.`, "");
  stops.forEach((s, i) => {
    const d = plan.city_details?.[s.city] || {};
    const n = plan._notes?.[s.city] || {};
    lines.push(`${i + 1}. ${s.city}: Fan Fit #${s.fit_rank ?? "?"} of ${s.markets ?? "?"}${s.hidden ? " (hidden market)" : ""}` +
      `${s.leg_km ? `, ${s.leg_km} km from the previous stop` : ""}`);
    if (n.why) lines.push(`   Why: ${n.why}`);
    if (n.promo) lines.push(`   Promo: ${n.promo}`);
    if (d.co_headliners?.length) lines.push(`   Share the bill with: ${d.co_headliners.map((c) => c.name).join(", ")}`);
    if (d.venues?.length) lines.push(`   Places fans like: ${d.venues.map((v) => v.name).join(", ")}`);
    lines.push("");
  });
  const c = plan.crossover || {};
  const names = (xs) => (xs || []).slice(0, 5).map((x) => x.name).join(", ");
  if (c.brands?.length) lines.push(`Sponsor leads (brands these fans love): ${names(c.brands)}`);
  if (c.movies?.length) lines.push(`Films these fans love: ${names(c.movies)}`);
  if (plan.plan_id) lines.push("", `Open the plan: ${location.origin}/p/${plan.plan_id}`);
  lines.push(`About ${plan.route?.total_km} km in total. Taste data from Qloo; affinity is aggregate taste, not ticket demand.`);
  return lines.join("\n");
}

async function copyText(text, ok) {
  try {
    await navigator.clipboard.writeText(text);
    $("editStatus").textContent = ok;
  } catch {
    $("editStatus").textContent = "This browser blocked copying. Use Print instead.";
  }
}
const copyPlan = () => copyText(planAsText(), "Plan copied. Paste it into an email or message.");
const copyLink = () => copyText(`${location.origin}/p/${plan.plan_id}`, "Link copied.");

// ---------------------------------------------------------------- provenance
function renderProvenance() {
  const calls = plan.calls || [];
  $("provSummary").textContent = `${calls.length} Qloo requests behind this plan`;
  $("calls").innerHTML = calls.map((c) => {
    const qs = Object.entries(c.params || {}).map(([k, v]) => `${esc(k)}=${esc(v)}`).join("&");
    return `<div><b>GET ${esc(c.path)}</b>?${qs} <span>(${esc(c.source)})</span></div>`;
  }).join("");
  $("caveats").innerHTML = (plan.final?.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");
  $("provSection").hidden = false;
}

// ---------------------------------------------------------------- poster
// A gig poster drawn from the plan's own Qloo results: every number on it is
// a Fan Fit rank from the heatmap.
async function makePoster() {
  const W = 1080, H = 1350, P = 72;
  const cv = document.createElement("canvas");
  cv.width = W; cv.height = H;
  const g = cv.getContext("2d");
  const D = '"Big Shoulders Display", "Arial Narrow", sans-serif';
  const B = '"Instrument Sans", Arial, sans-serif';
  try { await Promise.all([document.fonts.load(`900 100px ${D}`), document.fonts.load(`400 20px ${B}`)]); } catch {}
  const C = { night: "#0F1A2C", amber: "#FFB547", pink: "#FF5E8A", paper: "#EAE3D2", muted: "#8391AB", line: "#24354F" };
  const stops = plan.route?.stops || [];

  g.fillStyle = C.night; g.fillRect(0, 0, W, H);

  // Route silhouette, top right
  const pts = stops.map((s) => [s.lng, s.lat]);
  if (pts.length > 1) {
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const box = { x: W - P - 360, y: P, w: 360, h: 260 };
    const sx = box.w / Math.max(1e-6, Math.max(...xs) - Math.min(...xs));
    const sy = box.h / Math.max(1e-6, Math.max(...ys) - Math.min(...ys));
    const k = Math.min(sx, sy);
    const at = ([x, y]) => [box.x + (x - Math.min(...xs)) * k, box.y + (Math.max(...ys) - y) * k];
    g.strokeStyle = C.amber; g.lineWidth = 3; g.setLineDash([10, 10]);
    g.beginPath(); pts.forEach((p, i) => { const [x, y] = at(p); i ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke();
    g.setLineDash([]);
    stops.forEach((s, i) => {
      const [x, y] = at(pts[i]);
      g.fillStyle = s.hidden ? C.pink : C.amber;
      g.beginPath(); g.arc(x, y, 14, 0, Math.PI * 2); g.fill();
      g.fillStyle = C.night; g.font = `900 20px ${D}`; g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(String(i + 1), x, y + 1);
    });
  }

  // Title block
  g.textAlign = "left"; g.textBaseline = "alphabetic";
  const who = (plan.artist_label || (plan.seeds || []).map((s) => s.name).join(" + ")).toUpperCase();
  let size = 130; g.font = `900 ${size}px ${D}`;
  while (g.measureText(who).width > W - 2 * P - 380 && size > 56) { size -= 6; g.font = `900 ${size}px ${D}`; }
  g.fillStyle = C.amber; g.fillText(who, P, P + size * 0.85);
  let y = P + size * 0.85 + 58;
  g.fillStyle = C.paper; g.font = `700 52px ${D}`;
  g.fillText(`${(plan.country || "").toUpperCase()} TOUR`, P, y);
  y += 40;
  if (plan.artist_label && plan.seeds?.length) {
    g.fillStyle = C.muted; g.font = `400 24px ${B}`;
    g.fillText(`For fans of ${plan.seeds.map((s) => s.name).join(" and ")}`, P, y);
  }

  // The finding
  const ins = plan.compare?.insight;
  y = Math.max(y + 90, P + 300);
  if (ins) {
    const sk = `SKIP ${ins.skip.city.split(",")[0].toUpperCase()}`;
    g.font = `900 64px ${D}`; g.fillStyle = C.muted; g.fillText(sk, P, y);
    const w = g.measureText(sk).width;
    g.fillRect(P, y - 22, w, 5);
    g.font = `400 22px ${B}`; g.fillText(`#${ins.skip.rank} of ${ins.markets} for this sound`, P + w + 20, y - 4);
    y += 74;
    const pl = `PLAY ${ins.play.city.split(",")[0].toUpperCase()}`;
    g.font = `900 64px ${D}`; g.fillStyle = C.pink; g.fillText(pl, P, y);
    const w2 = g.measureText(pl).width;
    g.font = `400 22px ${B}`; g.fillStyle = C.muted;
    g.fillText(`#${ins.play.fit_rank}, though #${ins.play.size_rank} by size`, P + w2 + 20, y - 4);
    y += 40;
  }
  g.fillStyle = C.line; g.fillRect(P, y, W - 2 * P, 2);
  y += 30;

  // The dates-free tour list, like the back of a band shirt
  const rowH = Math.min(118, (H - y - 150) / Math.max(1, stops.length));
  stops.forEach((s, i) => {
    const [name, region] = s.city.split(",").map((x) => x.trim());
    const base = y + rowH * (i + 1) - rowH * 0.22;
    g.fillStyle = C.muted; g.font = `700 ${Math.round(rowH * 0.3)}px ${D}`;
    g.fillText(String(i + 1).padStart(2, "0"), P, base);
    let fs = Math.round(rowH * 0.78); g.font = `900 ${fs}px ${D}`;
    const maxW = W - 2 * P - 330;
    while (g.measureText(name.toUpperCase()).width > maxW && fs > 30) { fs -= 4; g.font = `900 ${fs}px ${D}`; }
    g.fillStyle = s.hidden ? C.pink : C.paper;
    g.fillText(name.toUpperCase(), P + 70, base);
    g.textAlign = "right";
    g.fillStyle = C.amber; g.font = `700 ${Math.round(rowH * 0.36)}px ${D}`;
    g.fillText(`${region ? region + "  " : ""}#${s.fit_rank ?? "?"}`, W - P, base);
    if (s.hidden) {
      g.fillStyle = C.pink; g.font = `500 18px ${B}`;
      g.fillText("HIDDEN MARKET", W - P, base - rowH * 0.42);
    }
    g.textAlign = "left";
  });

  // Footer: the claim and its source
  const c = plan.compare;
  g.fillStyle = C.line; g.fillRect(P, H - 130, W - 2 * P, 2);
  g.fillStyle = C.paper; g.font = `400 22px ${B}`;
  if (c?.tourcast?.median_rank && c?.biggest?.median_rank) {
    g.fillText(`Median Fan Fit rank #${c.tourcast.median_rank} of ${c.markets_scored} markets. ` +
      `A biggest-cities tour: #${c.biggest.median_rank}.`, P, H - 88);
  }
  g.fillStyle = C.muted; g.font = `400 18px ${B}`;
  g.fillText("Ranks from Qloo's taste graph: aggregate fan affinity, not ticket demand.", P, H - 56);
  g.fillText("Planned with Tourcast.", P, H - 30);
  g.fillStyle = C.pink; g.textAlign = "right"; g.fillText("Pink = hidden market", W - P, H - 30); g.textAlign = "left";
  return cv;
}

async function showPoster() {
  if (!plan.route?.stops?.length) return;
  $("posterBtn").disabled = true;
  try {
    const cv = await makePoster();
    const url = cv.toDataURL("image/png");
    $("posterImg").src = url;
    $("posterDl").href = url;
    const slug = (plan.artist_label || plan.seeds?.[0]?.name || "tour").toLowerCase().replace(/[^a-z0-9]+/g, "-");
    $("posterDl").download = `${slug}-tour-poster.png`;
    $("posterDlg").showModal();
  } catch (err) {
    $("editStatus").textContent = "The poster couldn't be drawn in this browser. Use Print instead.";
  } finally {
    $("posterBtn").disabled = false;
  }
}

init();
