// Dark Track web UI: live fused picture + replay player over the api/ JSON.
"use strict";

const LIVE_TRACKS_MS = 2000;
const LIVE_SLOW_MS = 5000;
// Live view: a track counts as ADS-B-corroborated if its last ADS-B fix is
// at most this old relative to its own latest update. Display only -- the
// went-dark *alert* comes from anomaly/, with cadence-scaled thresholds.
const LIVE_ADSB_FRESH_S = 90;
const WINDOW_MIN = 15;

const $ = (id) => document.getElementById(id);
const fmtTime = (secs) => new Date(secs * 1000).toISOString().slice(11, 19) + " UTC";
const ts = (iso) => (iso ? Date.parse(iso) / 1000 : null);
const kindLabel = { went_dark: "went dark", kinematic_inconsistency: "maneuver" };
const stateLabel = { adsb: "ADS-B + radar", radar: "radar only", dark: "went dark", stale: "ADS-B quiet" };

async function getJSON(url) {
  const res = await fetch(url);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// ---------------------------------------------------------------- map

const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([25.3, 53.3], 7);
// Standard OSM tiles (no API key), darkened in style.css.
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 14,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

// The map's box changes with the layout (phone breakpoint, panels), not
// only on window resize -- Leaflet has to be told, or tiles go missing.
// A fit made while the map has no size (page loaded in a background tab,
// pane still laying out) lands at zoom 0 -- the whole world -- so remember
// the intended view and apply it once the map actually has a size.
let fitTarget = null;
let mapHadSize = false;
function fitTo(bounds) {
  fitTarget = bounds;
  if (mapHadSize) map.fitBounds(bounds);
}
new ResizeObserver(() => {
  const el = $("map");
  const hasSize = el.clientWidth > 0 && el.clientHeight > 0;
  map.invalidateSize();
  if (hasSize && !mapHadSize && fitTarget) map.fitBounds(fitTarget);
  mapHadSize = hasSize;
}).observe($("map"));

const markers = new Map(); // track id -> { marker, state, heading }
const trailLayer = L.layerGroup().addTo(map);
let selectedId = null;
let fittedOnce = false;

function arrowSvg(heading) {
  const shape = heading == null
    ? '<path d="M0,-5 A5,5 0 1,1 0,5 A5,5 0 1,1 0,-5 Z"/>'
    : `<path d="M0,-8 L5.5,6 L0,3 L-5.5,6 Z" transform="rotate(${heading})"/>`;
  return `<svg viewBox="-9 -9 18 18">${shape}</svg>`;
}

// tracks: [{id, lat, lon, heading, state, label, watched}]
function renderTracks(tracks) {
  const seen = new Set();
  for (const t of tracks) {
    seen.add(t.id);
    let m = markers.get(t.id);
    if (!m) {
      const marker = L.marker([t.lat, t.lon], {
        icon: L.divIcon({ className: "trk", html: arrowSvg(t.heading), iconSize: [18, 18] }),
        keyboard: false,
      }).addTo(map);
      marker.on("click", () => select(t.id));
      marker.bindTooltip("", { direction: "top", offset: [0, -8] });
      m = { marker, state: null, heading: t.heading, label: null, watched: false };
      markers.set(t.id, m);
    } else {
      m.marker.setLatLng([t.lat, t.lon]);
    }
    const el = m.marker.getElement();
    if (m.heading !== t.heading && el) {
      if ((m.heading == null) !== (t.heading == null)) el.innerHTML = arrowSvg(t.heading);
      else el.querySelector("path").setAttribute("transform", `rotate(${t.heading})`);
      m.heading = t.heading;
    }
    if (el && m.state !== t.state) {
      if (m.state) el.classList.remove(m.state);
      el.classList.add(t.state);
      m.state = t.state;
    }
    if (el && m.watched !== !!t.watched) {
      el.classList.toggle("watched", !!t.watched);
      m.watched = !!t.watched;
    }
    if (el) el.classList.toggle("selected", t.id === selectedId);
    const label = `${t.label} · ${stateLabel[t.state]}`;
    if (m.label !== label) {
      m.marker.setTooltipContent(label);
      m.label = label;
    }
  }
  for (const [id, m] of markers) {
    if (!seen.has(id)) {
      m.marker.remove();
      markers.delete(id);
    }
  }
  if (!fittedOnce && tracks.length) {
    fitTo(L.latLngBounds(tracks.map((t) => [t.lat, t.lon])).pad(0.1));
    fittedOnce = true;
  }
}

function clearTracks() {
  for (const m of markers.values()) m.marker.remove();
  markers.clear();
  trailLayer.clearLayers();
}

// ---------------------------------------------------------------- inspector

function select(id) {
  selectedId = id;
  for (const [tid, m] of markers) m.marker.getElement()?.classList.toggle("selected", tid === id);
  document.querySelectorAll("#alert-list li").forEach((li) =>
    li.classList.toggle("selected", Number(li.dataset.track) === id));
  $("inspector").hidden = id == null;
  trailLayer.clearLayers();
  if (id == null) return;
  if (mode === "live") live.inspect(id);
  else player.inspect();
}

function showFields(title, fields, note = "") {
  $("insp-title").textContent = title;
  $("insp-fields").replaceChildren(...fields.flatMap(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v ?? "—";
    return [dt, dd];
  }));
  $("insp-note").textContent = note;
}

$("insp-close").addEventListener("click", () => select(null));

const kt = (mps) => (mps == null ? null : `${Math.round(mps * 1.94384)} kt`);
const deg = (h) => (h == null ? null : `${Math.round(h)}°`);

// ---------------------------------------------------------------- alerts

function renderAlerts(alerts, { now = null, onPick }) {
  const list = $("alert-list");
  list.replaceChildren(...alerts.map((a) => {
    const li = document.createElement("li");
    li.dataset.track = a.track_id;
    if (a.resolved) li.classList.add("resolved");
    if (a.track_id === selectedId) li.classList.add("selected");
    const kind = document.createElement("span");
    kind.className = `kind ${a.kind}`;
    kind.textContent = kindLabel[a.kind] || a.kind;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = a.label;
    who.title = a.label;
    const when = document.createElement("span");
    when.className = "when";
    // Older than a day (history from an earlier run): show the date too.
    when.textContent = (now ?? Date.now() / 1000) - a.t > 86400
      ? new Date(a.t * 1000).toISOString().slice(5, 16).replace("T", " ")
      : fmtTime(a.t).slice(0, 8);
    li.append(kind, who, when);
    li.addEventListener("click", () => onPick(a));
    return li;
  }));
  $("alert-count").textContent = alerts.length ? `(${alerts.length})` : "";
  $("alert-empty").hidden = alerts.length > 0;
}

// ---------------------------------------------------------------- live mode

const live = {
  timers: [],
  tracks: new Map(),

  start() {
    this.stop();
    fittedOnce = false;
    const fast = () => this.pollTracks().catch((e) => this.error(e));
    const slow = () => Promise.all([this.pollAlerts(), this.pollStatus()]).catch((e) => this.error(e));
    fast();
    slow();
    this.timers.push(setInterval(fast, LIVE_TRACKS_MS), setInterval(slow, LIVE_SLOW_MS));
  },

  stop() {
    this.timers.forEach(clearInterval);
    this.timers = [];
    document.querySelector(".empty-map")?.remove();
  },

  error(e) {
    $("status").innerHTML = "";
    const span = document.createElement("span");
    span.className = "warn";
    span.textContent = `API unreachable: ${e.message}`;
    $("status").append(span);
  },

  state(t) {
    if (t.anomalies.includes("went_dark")) return "dark";
    if (!t.last_adsb_at) return "radar";
    return ts(t.updated_at) - ts(t.last_adsb_at) <= LIVE_ADSB_FRESH_S ? "adsb" : "stale";
  },

  async pollTracks() {
    const { tracks } = await getJSON("/api/tracks");
    if (mode !== "live") return; // switched away while the request was in flight
    this.tracks = new Map(tracks.map((t) => [t.track_id, t]));
    renderTracks(tracks.map((t) => ({
      id: t.track_id, lat: t.lat, lon: t.lon,
      heading: t.heading_deg == null ? null : Math.round(t.heading_deg),
      state: this.state(t),
      label: t.external_id || `track ${t.track_id}`,
      watched: !!t.aircraft_type,
    })));
    this.emptyState(tracks.length === 0);
    if (selectedId != null) {
      if (this.tracks.has(selectedId)) this.showTrack(selectedId);
      else showFields($("insp-title").textContent, [], "Track retired from the live picture.");
    }
  },

  emptyState(empty) {
    const existing = document.querySelector(".empty-map");
    if (!empty) return existing?.remove();
    if (existing) return;
    const div = document.createElement("div");
    div.className = "empty-map";
    div.innerHTML = `<p>No live tracks. Start the pipeline (<code>./run.sh poll</code>,
      <code>radar</code>, <code>fusion</code>, <code>anomaly</code>) or replay a stored capture.</p>
      <button class="primary">Open replay</button>`;
    div.querySelector("button").addEventListener("click", () => setMode("replay"));
    $("map").append(div);
  },

  async pollAlerts() {
    const { alerts } = await getJSON("/api/alerts?limit=50");
    if (mode !== "live") return;
    renderAlerts(alerts.map((a) => ({
      track_id: a.track_id, kind: a.kind, t: ts(a.detected_at), resolved: !!a.resolved_at || !a.live,
      label: a.external_id || `track ${a.track_id}`,
    })), {
      onPick: (a) => {
        const t = this.tracks.get(a.track_id);
        if (t) map.panTo([t.lat, t.lon]);
        select(a.track_id);
      },
    });
  },

  async pollStatus() {
    const s = await getJSON("/api/status");
    if (mode !== "live") return;
    const last = Math.max(ts(s.last_adsb_at) || 0, ts(s.last_radar_at) || 0);
    const age = last ? Date.now() / 1000 - last : null;
    $("status").textContent = age != null && age < 120
      ? `${s.tracks} tracks · data ${Math.round(age)} s old · ${s.alerts_last_hour} alerts in the last hour`
      : `pipeline idle · last data ${last ? new Date(last * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "never"}`;
  },

  showTrack(id) {
    const t = this.tracks.get(id);
    if (!t) return;
    const now = ts(t.updated_at);
    const ago = (iso) => (iso ? `${Math.round(now - ts(iso))} s before last update` : "never");
    showFields(t.external_id || `Track ${id}`, [
      ["Type", t.type_label],
      ["State", stateLabel[this.state(t)]],
      ["Speed", kt(t.speed_mps)],
      ["Heading", deg(t.heading_deg)],
      ["Altitude", t.last_adsb_alt_m == null ? "on ground / unknown" : `${Math.round(t.last_adsb_alt_m * 3.28084)} ft`],
      ["Confidence", t.confidence.toFixed(2)],
      ["Updates", `${t.adsb_hits} ADS-B · ${t.radar_hits} radar`],
      ["Last ADS-B", ago(t.last_adsb_at)],
      ["Last radar", ago(t.last_radar_at)],
    ], "Trail: raw ADS-B fixes (cyan) and radar contacts (amber) fused into this track.");
  },

  async inspect(id) {
    this.showTrack(id);
    const { history } = await getJSON(`/api/tracks/${id}/history`);
    if (selectedId !== id) return;
    trailLayer.clearLayers();
    const adsb = history.filter((h) => h.source === "adsb").map((h) => [h.lat, h.lon]);
    if (adsb.length > 1) L.polyline(adsb, { color: "#3fd0e0", weight: 1.5, opacity: 0.6 }).addTo(trailLayer);
    for (const h of history) {
      L.circleMarker([h.lat, h.lon], {
        radius: h.source === "adsb" ? 2.5 : 3, weight: 0, fillOpacity: 0.8,
        fillColor: h.source === "adsb" ? "#3fd0e0" : "#f0b429",
      }).addTo(trailLayer);
    }
  },
};

// ---------------------------------------------------------------- replay mode

const player = {
  data: null,
  t: 0,
  playing: false,
  lastFrame: null,
  frameIndex: -1,
  alertsShown: 0,
  windowsLoaded: false,

  async enter() {
    $("status").textContent = this.data ? `replay · ${this.data.start.slice(0, 16).replace("T", " ")} UTC` : "replay";
    if (this.data) {
      fittedOnce = true;
      this.render(true);
      return;
    }
    renderAlerts([], { onPick: () => {} });
    if (this.windowsLoaded) return;
    try {
      const { windows } = await getJSON("/api/windows");
      const sel = $("window-select");
      sel.replaceChildren(...windows.map((w) => {
        const o = document.createElement("option");
        o.value = `${w.start}|${w.region}`;
        const start = new Date(w.start);
        const region = { nyc: "NYC", gulf: "Gulf" }[w.region] || w.region;
        o.textContent = `${start.toISOString().slice(5, 16).replace("T", " ")} UTC · ${region} · ${w.aircraft} aircraft`;
        return o;
      }));
      this.windowsLoaded = true;
      if (!windows.length) this.note("No stored captures with radar contacts yet.", true);
    } catch (e) {
      this.note(`Couldn't list captures: ${e.message}`, true);
    }
  },

  leave() {
    this.pause();
  },

  note(text, error = false) {
    $("replay-note").textContent = text;
    $("replay-note").classList.toggle("error", error);
  },

  range() {
    const [startIso, region] = $("window-select").value.split("|");
    const start = new Date(startIso);
    const end = new Date(start.getTime() + WINDOW_MIN * 60000);
    return `start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}&region=${region}`;
  },

  async loadCandidates() {
    const target = $("inject-target");
    const kind = $("inject-kind").value;
    target.disabled = !kind;
    if (!kind) {
      target.replaceChildren(new Option("—", ""));
      return;
    }
    target.replaceChildren(new Option("loading…", ""));
    try {
      const { candidates } = await getJSON(`/api/candidates?${this.range()}`);
      target.replaceChildren(...candidates.map((c) => new Option(c, c)));
    } catch (e) {
      this.note(`Couldn't list aircraft: ${e.message}`, true);
    }
  },

  async load() {
    if (!$("window-select").value) return;
    this.pause();
    const kind = $("inject-kind").value;
    const target = $("inject-target").value;
    let url = `/api/replay?${this.range()}&step=6`;
    if (kind && target) url += `&inject=${kind}&target=${encodeURIComponent(target)}`;
    $("load-replay").disabled = true;
    this.note("Running fusion + anomaly detection over the window…");
    try {
      this.data = await getJSON(url);
    } catch (e) {
      this.note(e.message, true);
      return;
    } finally {
      $("load-replay").disabled = false;
    }
    const d = this.data;
    d.t0 = d.frames[0].t;
    d.t1 = d.frames[d.frames.length - 1].t;
    d.byId = d.frames.map((f) => new Map(f.tracks.map((r) => [r[0], r])));
    const inj = d.injected;
    this.note(inj
      ? `Injected ${kindLabel[inj.kind] || inj.kind} on ${inj.external_id} at ${fmtTime(inj.injected_at)}. ADS-B interval ${Math.round(d.adsb_interval_s)} s; went-dark fires after ${Math.round(d.thresholds.silence_s)} s of silence.`
      : `Real traffic, nothing injected. ADS-B interval ${Math.round(d.adsb_interval_s)} s.`);
    $("transport").hidden = false;
    $("alert-ticks").replaceChildren(...d.alerts.map((a) => {
      const i = document.createElement("i");
      i.className = a.kind;
      i.style.left = `${((a.t - d.t0) / (d.t1 - d.t0)) * 100}%`;
      i.title = `${kindLabel[a.kind]}: ${a.external_id || "track " + a.track_id}`;
      return i;
    }));
    $("status").textContent = `replay · ${d.start.slice(0, 16).replace("T", " ")} UTC`;
    clearTracks();
    selectedId = null;
    $("inspector").hidden = true;
    // Fit the whole window's traffic, not just the first frame's.
    const pts = d.frames.filter((_, i) => i % 10 === 0).flatMap((f) => f.tracks.map((r) => [r[1], r[2]]));
    if (pts.length) fitTo(L.latLngBounds(pts).pad(0.05));
    fittedOnce = true;
    // With an injection, start shortly before it rather than at the window start.
    this.t = inj ? Math.max(d.t0, inj.injected_at - 90) : d.t0;
    this.frameIndex = -1;
    this.render(true);
    if (inj) {
      const id = Object.entries(d.tracks).find(([, m]) => m.external_id === inj.external_id)?.[0];
      if (id) select(Number(id));
    }
    this.play();
  },

  label(id) {
    const m = this.data.tracks[id] || {};
    return m.external_id || `track ${id}`;
  },

  // Fused picture at time t, linearly interpolated between sampled frames.
  frameAt(t) {
    const d = this.data;
    let i = Math.min(d.frames.length - 1, Math.max(0, Math.floor((t - d.t0) / d.step_s)));
    while (i > 0 && d.frames[i].t > t) i--;
    while (i < d.frames.length - 1 && d.frames[i + 1].t <= t) i++;
    const a = d.frames[i];
    const b = d.frames[i + 1];
    const f = b ? (t - a.t) / (b.t - a.t) : 0;
    const nextRows = b ? d.byId[i + 1] : null;
    return {
      index: i,
      tracks: a.tracks.map(([id, lat, lon, heading, speed, state, conf]) => {
        const n = nextRows?.get(id);
        return {
          id, heading, speed, state, conf,
          lat: n ? lat + (n[1] - lat) * f : lat,
          lon: n ? lon + (n[2] - lon) * f : lon,
          label: this.label(id),
          watched: !!(d.tracks[id] && d.tracks[id].type),
        };
      }),
    };
  },

  render(force = false) {
    const d = this.data;
    if (!d) return;
    const frame = this.frameAt(this.t);
    renderTracks(frame.tracks);
    $("clock").textContent = fmtTime(this.t);
    $("scrubber").value = Math.round(((this.t - d.t0) / (d.t1 - d.t0)) * 1000);
    if (force || frame.index !== this.frameIndex) {
      this.frameIndex = frame.index;
      const seen = d.alerts.filter((a) => a.t <= this.t).reverse();
      if (force || seen.length !== this.alertsShown) this.renderAlerts(seen);
      if (selectedId != null) this.inspect(frame);
    }
  },

  // Only when the set changes: rebuilding the list every frame swallowed clicks.
  renderAlerts(seen) {
    this.alertsShown = seen.length;
    renderAlerts(seen.map((a) => ({ ...a, label: a.external_id || `track ${a.track_id}` })), {
      now: this.t,
      onPick: (a) => {
        this.pause();
        this.seek(a.t);
        select(a.track_id);
      },
    });
  },

  inspect(frame = this.frameAt(this.t)) {
    const d = this.data;
    const id = selectedId;
    const row = frame.tracks.find((r) => r.id === id);
    const meta = d.tracks[id] || {};
    const alerts = d.alerts.filter((a) => a.track_id === id && a.t <= this.t);
    showFields(this.label(id), row ? [
      ["Type", meta.type],
      ["State", stateLabel[row.state]],
      ["Speed", kt(row.speed)],
      ["Heading", deg(row.heading)],
      ["Confidence", row.conf.toFixed(2)],
      ["Alerts", alerts.map((a) => `${kindLabel[a.kind]} ${fmtTime(a.t).slice(0, 8)}`).join(", ") || "none"],
    ] : [], row ? "Trail: fused position so far (red where it was flagged went dark)." : "Not in the fused picture at this time.");
    trailLayer.clearLayers();
    const pts = [];
    for (let i = 0; i <= frame.index; i++) {
      const r = d.byId[i].get(id);
      if (r) pts.push(r);
    }
    if (pts.length > 1) L.polyline(pts.map((r) => [r[1], r[2]]), { color: "#e6edf3", weight: 1.5, opacity: 0.5 }).addTo(trailLayer);
    for (const r of pts.filter((r) => r[5] === "dark")) {
      L.circleMarker([r[1], r[2]], { radius: 3, weight: 0, fillOpacity: 0.9, fillColor: "#ff5d5d" }).addTo(trailLayer);
    }
  },

  seek(t) {
    this.t = Math.min(this.data.t1, Math.max(this.data.t0, t));
    this.render(true);
  },

  play() {
    if (!this.data || this.playing) return;
    if (this.t >= this.data.t1) this.t = this.data.t0;
    this.playing = true;
    $("play").textContent = "❚❚";
    $("play").setAttribute("aria-label", "Pause");
    this.lastFrame = performance.now();
    const step = (now) => {
      if (!this.playing) return;
      this.t += ((now - this.lastFrame) / 1000) * Number($("speed").value);
      this.lastFrame = now;
      if (this.t >= this.data.t1) {
        this.t = this.data.t1;
        this.render();
        return this.pause();
      }
      this.render();
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  },

  pause() {
    this.playing = false;
    $("play").textContent = "▶";
    $("play").setAttribute("aria-label", "Play");
  },
};

$("window-select").addEventListener("change", () => player.loadCandidates());
$("inject-kind").addEventListener("change", () => player.loadCandidates());
$("load-replay").addEventListener("click", () => player.load());
$("play").addEventListener("click", () => (player.playing ? player.pause() : player.play()));
$("scrubber").addEventListener("input", (e) => {
  const d = player.data;
  if (d) player.seek(d.t0 + (Number(e.target.value) / 1000) * (d.t1 - d.t0));
});

// ---------------------------------------------------------------- modes

let mode = null;

function setMode(next) {
  if (next === mode) return;
  if (mode === "live") live.stop();
  if (mode === "replay") player.leave();
  mode = next;
  document.querySelectorAll(".modes button").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.mode === next)));
  $("replay-panel").hidden = next !== "replay";
  select(null);
  clearTracks();
  if (next === "live") live.start();
  else player.enter();
}

document.querySelectorAll(".modes button").forEach((b) =>
  b.addEventListener("click", () => setMode(b.dataset.mode)));

setMode("live");
