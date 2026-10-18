// Dark Track web UI: live fused picture + replay player over the api/ JSON,
// with a dark-aircraft panel and a follow mode for planes that switch their
// transponder off.
"use strict";

const LIVE_TRACKS_MS = 2000;
const LIVE_SLOW_MS = 5000;
// Live view: a track counts as transponder-corroborated if its last ADS-B fix
// is at most this old relative to its own latest update. Display only -- the
// went-dark *alert* comes from anomaly/, with cadence-scaled thresholds.
const LIVE_ADSB_FRESH_S = 90;
const WINDOW_MIN = 15;
const PROJECT_S = 300;        // projected path drawn this far ahead
const RING_SIGMAS = 2;        // uncertainty ring radius, in standard deviations
const TOAST_MS = 12000;
// The worked example: a US-military-flagged aircraft over the Gulf
// (docs/DEMO.md). Falls back to any Gulf window if this one isn't stored.
const EXAMPLE = { start: Date.UTC(2026, 9, 14, 3, 30) / 1000, region: "gulf", target: "YETI45" };

const $ = (id) => document.getElementById(id);
const fmtTime = (secs) => new Date(secs * 1000).toISOString().slice(11, 19) + " UTC";
const ts = (iso) => (iso ? Date.parse(iso) / 1000 : null);
const kindLabel = { went_dark: "went dark", kinematic_inconsistency: "maneuver" };
const stateLabel = {
  adsb: "Identified — transponder on",
  radar: "Radar only — never identified",
  dark: "DARK — transponder off, radar still sees it",
  stale: "Transponder quiet",
};
const shortState = { adsb: "identified", radar: "radar only", dark: "DARK", stale: "transponder quiet" };
const REGION_NAMES = { nyc: "New York", gulf: "Persian Gulf" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

async function getJSON(url) {
  const res = await fetch(url);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// ---------------------------------------------------------------- formatting

const kt = (mps) => (mps == null ? null : `${Math.round(mps * 1.94384)} kt (${Math.round(mps * 3.6)} km/h)`);
const COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];
const deg = (h) => (h == null ? null : `${Math.round(h)}° (${COMPASS[Math.round(((h % 360) + 360) % 360 / 45) % 8]})`);
function duration(s) {
  if (s == null || !isFinite(s)) return null;
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min ${s % 60} s` : `${Math.floor(m / 60)} h ${m % 60} min`;
}
const dist = (m) => (m == null ? null : m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`);
const confidenceWords = (c) =>
  c == null ? null : `${c >= 0.8 ? "High" : c >= 0.4 ? "Medium" : "Low"} (${c.toFixed(2)})`;
const latlon = (lat, lon) =>
  `${Math.abs(lat).toFixed(3)}°${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(3)}°${lon >= 0 ? "E" : "W"}`;

function haversine(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180;
  const a = Math.sin(((lat2 - lat1) * r) / 2) ** 2 +
    Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(a));
}

// Point `m` metres from (lat, lon) along `heading` degrees -- flat-earth,
// fine over the few minutes of a projected path.
function ahead(lat, lon, heading, m) {
  const r = Math.PI / 180;
  return [
    lat + (m * Math.cos(heading * r)) / 111320,
    lon + (m * Math.sin(heading * r)) / (111320 * Math.cos(lat * r)),
  ];
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
  if (hasSize && !mapHadSize && fitTarget) map.fitBounds(fitTarget, { animate: false });
  mapHadSize = hasSize;
}).observe($("map"));
// Follow mode re-centres every frame; doing that during a zoom animation
// leaves the animation half-done (stuck zoom, tiles drawn off-screen).
let zooming = false;
map.on("zoomstart", () => { zooming = true; });
map.on("zoomend", () => { zooming = false; });

const markers = new Map(); // track id -> { marker, state, heading }
const trailLayer = L.layerGroup().addTo(map);
const darkLayer = L.layerGroup().addTo(map);
const radarLayer = L.layerGroup().addTo(map); // realistic radar: site + coverage
let selectedId = null;
let followId = null;
let fittedOnce = false;
let lastTracks = [];      // the view-model tracks last drawn
let lastNow = null;       // the time they're as of (seconds)

function arrowSvg(heading) {
  const shape = heading == null
    ? '<path d="M0,-5 A5,5 0 1,1 0,5 A5,5 0 1,1 0,-5 Z"/>'
    : `<path d="M0,-8 L5.5,6 L0,3 L-5.5,6 Z" transform="rotate(${heading})"/>`;
  return `<svg viewBox="-9 -9 18 18">${shape}</svg>`;
}

// tracks: [{id, lat, lon, heading, speed, state, conf, unc, lastT, lastLat,
//           lastLon, label, type, watched}]; now: seconds the picture is as of
function renderTracks(tracks, now) {
  lastTracks = tracks;
  lastNow = now;
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
    const label = `${t.label} · ${shortState[t.state]}${t.type ? " · " + t.type : ""} — click for details`;
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
  drawDark(tracks, now);
  darkPanel.update(tracks, now);
  applyFollow(tracks, now);
}

function clearTracks() {
  for (const m of markers.values()) m.marker.remove();
  markers.clear();
  trailLayer.clearLayers();
  darkLayer.clearLayers();
  lastTracks = [];
  darkPanel.update([], null);
}

// ---------------------------------------------------------------- dark aircraft

// Facts about a track flying dark (or quiet): how long, how far since the
// transponder last confirmed it.
function darkFacts(t, now) {
  if (t.lastT == null || t.lastLat == null) return null;
  // Live tracks each carry the time their position is as of; measure from
  // that, so every figure about one plane agrees with the others.
  return {
    since: (t.updated ?? now) - t.lastT,
    flown: haversine(t.lastLat, t.lastLon, t.lat, t.lon),
  };
}

// Map overlays for dark aircraft: where the transponder last put it, a line
// to where radar sees it now, a ring for where it could be, and (for the
// selected or followed one) where it's heading. Redrawn a few times a second,
// not every animation frame.
let darkDrawnAt = 0;
function drawDark(tracks, now, force = false) {
  const wall = performance.now();
  if (!force && wall - darkDrawnAt < 150) return;
  darkDrawnAt = wall;
  darkLayer.clearLayers();
  for (const t of tracks) {
    const focus = t.id === selectedId || t.id === followId;
    if (t.state !== "dark" && !(focus && t.lastLat != null)) continue;
    const color = t.state === "dark" ? "#ff5d5d" : "#7d8590";
    if (t.unc) {
      L.circle([t.lat, t.lon], {
        radius: RING_SIGMAS * t.unc, color, weight: 1, dashArray: "4 4", fillColor: color, fillOpacity: 0.08,
        interactive: false,
      }).addTo(darkLayer);
    }
    if (t.lastLat != null) {
      L.polyline([[t.lastLat, t.lastLon], [t.lat, t.lon]], { color, weight: 1.5, dashArray: "2 5", interactive: false })
        .addTo(darkLayer);
      const last = L.circleMarker([t.lastLat, t.lastLon], { radius: 5, color, weight: 2, fill: false })
        .addTo(darkLayer);
      const text = `Last confirmed by transponder${t.lastT ? " at " + fmtTime(t.lastT).slice(0, 8) : ""}`;
      // Label on the side away from where the plane went, so it never sits
      // on the plane or its projected path.
      const behindEast = t.lon <= t.lastLon;
      last.bindTooltip(focus ? `${t.label}: ${text}` : text, {
        permanent: focus, direction: behindEast ? "right" : "left",
        className: "map-label dark", offset: [behindEast ? 6 : -6, 0],
      });
    }
    if (focus && t.heading != null && t.speed) {
      const end = ahead(t.lat, t.lon, t.heading, t.speed * PROJECT_S);
      L.polyline([[t.lat, t.lon], end], { color: "#e6edf3", weight: 1.5, opacity: 0.6, dashArray: "6 6", interactive: false })
        .addTo(darkLayer);
      L.circleMarker(end, { radius: 3, color: "#e6edf3", weight: 1, fillOpacity: 0.6 })
        .bindTooltip(`In ${PROJECT_S / 60} min if it holds course`, { permanent: true, direction: "right", className: "map-label", offset: [6, 0] })
        .addTo(darkLayer);
    }
  }
}

const darkPanel = {
  key: "",

  // Rebuild the list only when the set of dark aircraft changes (rebuilding
  // every frame swallowed clicks); refresh the numbers in place otherwise.
  update(tracks, now) {
    const dark = tracks.filter((t) => t.state === "dark");
    const key = dark.map((t) => t.id).sort((a, b) => a - b).join(",");
    if (key !== this.key) {
      this.key = key;
      $("dark-list").replaceChildren(...dark.map((t) => this.item(t)));
    }
    for (const t of dark) {
      const li = $("dark-list").querySelector(`li[data-track="${t.id}"]`);
      if (!li) continue;
      const f = darkFacts(t, now);
      li.querySelector(".facts").innerHTML = f
        ? `Dark for <b>${duration(f.since)}</b> · flown <b>${dist(f.flown)}</b> since<br>` +
          `Heading <b>${deg(t.heading) ?? "—"}</b> at <b>${kt(t.speed) ?? "—"}</b>`
        : `Heading <b>${deg(t.heading) ?? "—"}</b> at <b>${kt(t.speed) ?? "—"}</b>`;
      li.classList.toggle("selected", t.id === selectedId);
      const b = li.querySelector("button");
      b.textContent = t.id === followId ? "Following" : "Follow";
      b.classList.toggle("on", t.id === followId);
    }
    $("dark-count").textContent = dark.length ? `(${dark.length})` : "";
    $("dark-empty").hidden = dark.length > 0;
  },

  item(t) {
    const li = document.createElement("li");
    li.dataset.track = t.id;
    li.title = "Click to show this plane";
    li.innerHTML = `<div class="head"><span class="name"></span><button class="follow-btn"
      title="Keep this plane centred on the map">Follow</button></div>
      <div class="type"></div><div class="facts"></div>`;
    li.querySelector(".name").textContent = t.label;
    li.querySelector(".type").textContent = t.type || "Aircraft type unknown";
    li.querySelector("button").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleFollow(t.id);
    });
    li.addEventListener("click", () => {
      select(t.id);
      const cur = lastTracks.find((x) => x.id === t.id);
      if (cur) map.panTo([cur.lat, cur.lon]);
    });
    return li;
  },

  reset() {
    this.key = "";
  },
};

// ---------------------------------------------------------------- follow

function labelOf(id) {
  return lastTracks.find((t) => t.id === id)?.label ?? `track ${id}`;
}

function follow(id) {
  followId = id;
  if (selectedId !== id) select(id);
  // Zoom in and centre in one step, without animation: follow re-centres
  // every frame, and an animated zoom interrupted that way is left
  // half-done (stuck at the old zoom, tiles drawn off-screen).
  const t = lastTracks.find((x) => x.id === id);
  fitTarget = null; // a pending "fit everything" must not undo this view
  if (t) map.setView([t.lat, t.lon], Math.max(map.getZoom(), 9), { animate: false });
  $("follow-banner").hidden = false;
  refreshFollowUi();
  applyFollow(lastTracks, lastNow);
  drawDark(lastTracks, lastNow, true);
}

function stopFollow(message) {
  if (followId == null) return;
  followId = null;
  $("follow-banner").hidden = true;
  refreshFollowUi();
  drawDark(lastTracks, lastNow, true);
  if (message) toast({ text: message, kind: "info" });
}

function toggleFollow(id) {
  if (followId === id) stopFollow();
  else follow(id);
}

function refreshFollowUi() {
  const b = $("insp-follow");
  b.hidden = selectedId == null;
  const on = selectedId != null && selectedId === followId;
  b.textContent = on ? "Stop following" : "Follow this plane";
  b.classList.toggle("on", on);
  darkPanel.update(lastTracks, lastNow);
}

function applyFollow(tracks, now) {
  if (followId == null) return;
  const t = tracks.find((x) => x.id === followId);
  if (!t) {
    const name = $("follow-text").dataset.label || "The plane";
    return stopFollow(`${name} is no longer tracked — the radar has lost it.`);
  }
  if (!zooming) map.panTo([t.lat, t.lon], { animate: false });
  const f = darkFacts(t, now);
  $("follow-text").dataset.label = t.label;
  $("follow-text").textContent = t.state === "dark" && f
    ? `Following ${t.label} — dark for ${duration(f.since)}, flown ${dist(f.flown)} since`
    : `Following ${t.label} — ${shortState[t.state]}`;
}

$("follow-stop").addEventListener("click", () => stopFollow());
$("insp-follow").addEventListener("click", () => selectedId != null && toggleFollow(selectedId));
map.on("dragstart", () => stopFollow(followId != null ? "Stopped following because you moved the map." : null));

// ---------------------------------------------------------------- pop-ups

function toast({ text, kind = "dark", actions = [] }) {
  const div = document.createElement("div");
  div.className = `toast ${kind}`;
  const p = document.createElement("div");
  p.textContent = text;
  div.append(p);
  if (actions.length) {
    const row = document.createElement("div");
    row.className = "actions";
    for (const a of actions) {
      const b = document.createElement("button");
      b.textContent = a.label;
      if (a.primary) b.className = "follow-btn";
      b.addEventListener("click", () => {
        a.fn();
        div.remove();
      });
      row.append(b);
    }
    const close = document.createElement("button");
    close.className = "ghost";
    close.textContent = "Dismiss";
    close.addEventListener("click", () => div.remove());
    row.append(close);
    div.append(row);
  }
  $("toasts").append(div);
  while ($("toasts").children.length > 3) $("toasts").firstChild.remove();
  setTimeout(() => div.remove(), TOAST_MS);
}

function alertToast(a) {
  const who = a.label;
  if (a.kind === "went_dark") {
    toast({
      text: `${who} switched its transponder off — the radar still sees it.`,
      actions: [{ label: "Follow it", primary: true, fn: () => follow(a.track_id) }],
    });
  } else {
    toast({
      kind: "maneuver",
      text: `${who} is moving in a way it shouldn't (sudden maneuver).`,
      actions: [{ label: "Show me", fn: () => select(a.track_id) }],
    });
  }
}

// ---------------------------------------------------------------- inspector

function select(id) {
  selectedId = id;
  for (const [tid, m] of markers) m.marker.getElement()?.classList.toggle("selected", tid === id);
  document.querySelectorAll("#alert-list li").forEach((li) =>
    li.classList.toggle("selected", Number(li.dataset.track) === id));
  $("inspector").hidden = id == null;
  trailLayer.clearLayers();
  refreshFollowUi();
  drawDark(lastTracks, lastNow, true);
  if (id == null) return;
  if (mode === "live") live.inspect(id);
  else player.inspect();
}

function showFields(title, fields, note = "") {
  $("insp-title").textContent = title;
  $("insp-fields").replaceChildren(...fields.filter(([, v]) => v !== undefined).flatMap(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v ?? "—";
    return [dt, dd];
  }));
  $("insp-note").textContent = note;
}

// The details every track gets, plus the dark-plane ones when they apply.
function trackFields(t, now, extra = []) {
  const f = darkFacts(t, now);
  return [
    ["Aircraft type", t.type || "Unknown (civil or not on the watch list)"],
    ["Status", stateLabel[t.state]],
    ["Speed", kt(t.speed)],
    ["Heading", deg(t.heading)],
    ...(f ? [
      ["Transponder last heard", `${fmtTime(t.lastT)} (${duration(f.since)} ago)`],
      ["Last confirmed at", latlon(t.lastLat, t.lastLon)],
      ["Flown since then", dist(f.flown)],
    ] : []),
    ["Could be within", t.unc ? `${dist(RING_SIGMAS * t.unc)} of the arrow` : undefined],
    ["How sure we are", confidenceWords(t.conf)],
    ...extra,
  ];
}

$("insp-close").addEventListener("click", () => select(null));

// ---------------------------------------------------------------- alerts

function renderAlerts(alerts, { now = null, onPick }) {
  const list = $("alert-list");
  list.replaceChildren(...alerts.map((a) => {
    const li = document.createElement("li");
    li.dataset.track = a.track_id;
    li.title = "Click to show this plane";
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
  seenAlerts: null, // alert ids already shown (null until the first poll)

  start() {
    this.stop();
    fittedOnce = false;
    this.seenAlerts = null;
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
    span.textContent = `Can't reach the Dark Track server (${e.message})`;
    $("status").append(span);
  },

  state(t) {
    if (t.anomalies.includes("went_dark")) return "dark";
    if (!t.last_adsb_at) return "radar";
    return ts(t.updated_at) - ts(t.last_adsb_at) <= LIVE_ADSB_FRESH_S ? "adsb" : "stale";
  },

  view(t) {
    const state = this.state(t);
    const quiet = state === "dark" || state === "stale";
    return {
      id: t.track_id, lat: t.lat, lon: t.lon,
      heading: t.heading_deg == null ? null : Math.round(t.heading_deg),
      speed: t.speed_mps, state, conf: t.confidence, unc: t.position_std_m,
      lastT: quiet ? ts(t.last_adsb_at) : null,
      lastLat: quiet ? t.last_adsb_lat : null,
      lastLon: quiet ? t.last_adsb_lon : null,
      label: t.external_id || `track ${t.track_id}`,
      type: t.type_label, watched: !!t.aircraft_type,
      updated: ts(t.updated_at),
    };
  },

  async pollTracks() {
    const { tracks } = await getJSON("/api/tracks");
    if (mode !== "live") return; // switched away while the request was in flight
    this.tracks = new Map(tracks.map((t) => [t.track_id, t]));
    const views = tracks.map((t) => this.view(t));
    const now = Math.max(...views.map((v) => v.updated), 0) || Date.now() / 1000;
    renderTracks(views, now);
    this.emptyState(tracks.length === 0);
    if (selectedId != null) {
      if (this.tracks.has(selectedId)) this.showTrack(selectedId);
      else showFields($("insp-title").textContent, [], "This plane has left the live picture.");
    }
  },

  emptyState(empty) {
    const existing = document.querySelector(".empty-map");
    if (!empty) return existing?.remove();
    if (existing) return;
    const div = document.createElement("div");
    div.className = "empty-map";
    div.innerHTML = `<p><b>Nothing live right now.</b> The live feed isn't running, so there are no
      planes to show yet.</p>
      <button class="primary">Watch a recording instead</button>
      <details class="tech"><summary>How to start the live feed</summary>
      <p>In four terminals, in the project folder: <code>./run.sh poll --region nyc --quiet</code>,
      <code>./run.sh radar</code>, <code>./run.sh fusion --region nyc</code>,
      <code>./run.sh anomaly</code>. Planes appear here within a minute.</p></details>`;
    div.querySelector("button").addEventListener("click", () => setMode("replay"));
    $("map").append(div);
  },

  async pollAlerts() {
    const { alerts } = await getJSON("/api/alerts?limit=50");
    if (mode !== "live") return;
    const rows = alerts.map((a) => ({
      id: a.id, track_id: a.track_id, kind: a.kind, t: ts(a.detected_at),
      resolved: !!a.resolved_at || !a.live, live: a.live,
      label: a.external_id || `track ${a.track_id}`,
    }));
    // Pop-ups only for alerts that arrive while you're watching.
    if (this.seenAlerts) {
      for (const a of rows) if (!this.seenAlerts.has(a.id) && a.live) alertToast(a);
    }
    this.seenAlerts = new Set(rows.map((a) => a.id));
    renderAlerts(rows, {
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
      ? `Live · ${s.tracks} aircraft · updated ${Math.round(age)} s ago · ${s.alerts_last_hour} alerts in the last hour`
      : `Live feed not running · last data ${last ? new Date(last * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "never"}`;
  },

  showTrack(id) {
    const t = this.tracks.get(id);
    if (!t) return;
    const v = this.view(t);
    showFields(v.label, trackFields(v, v.updated, [
      ["Altitude", t.last_adsb_alt_m == null ? "on the ground / unknown" : `${Math.round(t.last_adsb_alt_m * 3.28084)} ft`],
      ["Updates", `${t.adsb_hits} transponder · ${t.radar_hits} radar`],
    ]), "Trail: the plane's own transponder fixes (cyan) and radar contacts (amber).");
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

function windowLabel(w) {
  const s = new Date(w.start);
  const e = new Date(s.getTime() + WINDOW_MIN * 60000);
  const hm = (d) => d.toISOString().slice(11, 16);
  const region = REGION_NAMES[w.region] || w.region;
  return `${s.getUTCDate()} ${MONTHS[s.getUTCMonth()]} · ${hm(s)}–${hm(e)} UTC · ${region} · ${w.aircraft} aircraft`;
}

const player = {
  data: null,
  t: 0,
  playing: false,
  lastFrame: null,
  frameIndex: -1,
  alertsShown: 0,
  toasted: new Set(),
  autoFollow: null,  // callsign to follow automatically once it goes dark
  windows: null,

  async enter() {
    $("status").textContent = this.data ? `Recording · ${windowLabel({ ...this.data.window })}` : "Recordings";
    if (this.data) {
      fittedOnce = true;
      this.render(true);
      return;
    }
    renderAlerts([], { onPick: () => {} });
    await this.ensureWindows();
  },

  async ensureWindows() {
    if (this.windows) return this.windows;
    try {
      const { windows } = await getJSON("/api/windows");
      this.windows = windows;
      $("window-select").replaceChildren(...windows.map((w) => {
        const o = document.createElement("option");
        o.value = `${w.start}|${w.region}`;
        o.textContent = windowLabel(w);
        return o;
      }));
      if (!windows.length) this.note("No recordings yet — run the live feed for a while to record some.", true);
    } catch (e) {
      this.note(`Couldn't list recordings: ${e.message}`, true);
    }
    return this.windows || [];
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
    $("target-label").hidden = !kind;
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
    stopFollow();
    const kind = $("inject-kind").value;
    const target = $("inject-target").value;
    let url = `/api/replay?${this.range()}&step=6`;
    if (kind && target) url += `&inject=${kind}&target=${encodeURIComponent(target)}`;
    else this.autoFollow = null;
    url += `&radar=${$("radar-kind").value}`;
    $("load-replay").disabled = true;
    this.note("Loading the recording and checking every plane… (a few seconds)");
    try {
      this.data = await getJSON(url);
    } catch (e) {
      this.note(e.message, true);
      return;
    } finally {
      $("load-replay").disabled = false;
    }
    const d = this.data;
    drawRadarSite(d.radar_site);
    const [startIso, region] = $("window-select").value.split("|");
    d.window = { start: startIso, region, aircraft: ($("window-select").selectedOptions[0]?.textContent.match(/(\d+) aircraft/) || [])[1] };
    d.t0 = d.frames[0].t;
    d.t1 = d.frames[d.frames.length - 1].t;
    d.byId = d.frames.map((f) => new Map(f.tracks.map((r) => [r[0], r])));
    const inj = d.injected;
    this.note(inj
      ? inj.kind === "went_dark"
        ? `${inj.external_id} switches its transponder off at ${fmtTime(inj.injected_at)}. ` +
          `It turns red once it's been silent ${Math.round(d.thresholds.silence_s)} s while the radar still sees it.`
        : `${inj.external_id} starts a sudden maneuver at ${fmtTime(inj.injected_at)}.`
      : "Real traffic — nothing simulated. Click any plane for details.");
    $("transport").hidden = false;
    $("alert-ticks").replaceChildren(...d.alerts.map((a) => {
      const i = document.createElement("i");
      i.className = a.kind;
      i.style.left = `${((a.t - d.t0) / (d.t1 - d.t0)) * 100}%`;
      i.title = `${kindLabel[a.kind]}: ${a.external_id || "track " + a.track_id}`;
      return i;
    }));
    $("status").textContent = `Recording · ${windowLabel(d.window)}`;
    clearTracks();
    darkPanel.reset();
    selectedId = null;
    this.toasted = new Set();
    $("inspector").hidden = true;
    // Fit the whole window's traffic, not just the first frame's.
    const pts = d.frames.filter((_, i) => i % 10 === 0).flatMap((f) => f.tracks.map((r) => [r[1], r[2]]));
    if (pts.length) fitTo(L.latLngBounds(pts).pad(0.05));
    fittedOnce = true;
    // With an event, start shortly before it rather than at the window start.
    this.t = inj ? Math.max(d.t0, inj.injected_at - 90) : d.t0;
    // Alerts already behind the start time don't pop up.
    for (const a of d.alerts) if (a.t <= this.t) this.toasted.add(a);
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
      tracks: a.tracks.map(([id, lat, lon, heading, speed, state, conf, unc, lastT, lastLat, lastLon]) => {
        const n = nextRows?.get(id);
        return {
          id, heading, speed, state, conf, unc, lastT, lastLat, lastLon,
          lat: n ? lat + (n[1] - lat) * f : lat,
          lon: n ? lon + (n[2] - lon) * f : lon,
          label: this.label(id),
          type: d.tracks[id]?.type,
          watched: !!(d.tracks[id] && d.tracks[id].type),
        };
      }),
    };
  },

  render(force = false) {
    const d = this.data;
    if (!d) return;
    const frame = this.frameAt(this.t);
    renderTracks(frame.tracks, this.t);
    $("clock").textContent = fmtTime(this.t);
    $("scrubber").value = Math.round(((this.t - d.t0) / (d.t1 - d.t0)) * 1000);
    if (force || frame.index !== this.frameIndex) {
      this.frameIndex = frame.index;
      const seen = d.alerts.filter((a) => a.t <= this.t).reverse();
      if (force || seen.length !== this.alertsShown) this.renderAlerts(seen);
      // Pop-ups for alerts reached while playing (not by dragging the slider).
      if (this.playing) {
        for (const a of seen) {
          if (this.toasted.has(a)) continue;
          this.toasted.add(a);
          const label = a.external_id || `track ${a.track_id}`;
          if (a.kind === "went_dark" && this.autoFollow && a.external_id === this.autoFollow) {
            follow(a.track_id);
            toast({ text: `${label} switched its transponder off. Following it — watch the red ring and dashed lines.` });
            this.autoFollow = null;
          } else {
            alertToast({ ...a, label });
          }
        }
      }
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
    const alerts = d.alerts.filter((a) => a.track_id === id && a.t <= this.t);
    showFields(this.label(id), row ? trackFields(row, this.t, [
      ["Alerts", alerts.map((a) => `${kindLabel[a.kind]} ${fmtTime(a.t).slice(0, 8)}`).join(", ") || "none"],
    ]) : [], row
      ? row.state === "dark"
        ? "On the map: the hollow circle is where its transponder last put it, the ring is where it could be now, and the dashed white line is where it's heading."
        : "Trail: where it has flown so far (red where it was flying dark)."
      : "Not being tracked at this moment.");
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
    // Jumping past alerts: no pop-ups for them.
    for (const a of this.data.alerts) if (a.t <= this.t) this.toasted.add(a);
    this.render(true);
  },

  play() {
    if (!this.data || this.playing) return;
    if (this.t >= this.data.t1) {
      this.t = this.data.t0;
      this.toasted = new Set();
    }
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

// ---------------------------------------------------------------- welcome / example

const WELCOME_KEY = "darktrack.welcomeSeen";

function openWelcome() {
  $("welcome").hidden = false;
  $("welcome-example").focus();
}

function closeWelcome() {
  $("welcome").hidden = true;
  try {
    localStorage.setItem(WELCOME_KEY, "1");
  } catch (e) { /* private window: show it again next time, that's fine */ }
}

async function runExample() {
  closeWelcome();
  setMode("replay");
  const windows = await player.ensureWindows();
  if (!windows.length) return;
  const sel = $("window-select");
  const opts = [...sel.options];
  const pick = opts.find((o) => {
    const [startIso, region] = o.value.split("|");
    return ts(startIso) === EXAMPLE.start && region === EXAMPLE.region;
  }) || opts.find((o) => o.value.endsWith(`|${EXAMPLE.region}`)) || opts[0];
  sel.value = pick.value;
  $("inject-kind").value = "went_dark";
  await player.loadCandidates();
  const tg = $("inject-target");
  if ([...tg.options].some((o) => o.value === EXAMPLE.target)) tg.value = EXAMPLE.target;
  player.autoFollow = tg.value || null;
  await player.load();
}

$("help-open").addEventListener("click", openWelcome);
$("welcome-close").addEventListener("click", closeWelcome);
$("welcome-example").addEventListener("click", runExample);
$("welcome").addEventListener("click", (e) => { if (e.target === $("welcome")) closeWelcome(); });

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("welcome").hidden) return closeWelcome();
  const typing = ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(document.activeElement?.tagName);
  if (e.key === " " && !typing && mode === "replay" && player.data) {
    e.preventDefault();
    player.playing ? player.pause() : player.play();
  }
});

// ---------------------------------------------------------------- modes

let mode = null;

// The realistic radar's site and how far it reaches: planes outside the
// ring (or low and far away, below the horizon) aren't seen by radar.
function drawRadarSite(site) {
  radarLayer.clearLayers();
  if (!site) return;
  L.circle([site.lat, site.lon], {
    radius: site.max_range_m, color: "#8b949e", weight: 1, dashArray: "2 6",
    fill: false, interactive: false,
  }).addTo(radarLayer);
  L.circleMarker([site.lat, site.lon], {
    radius: 5, color: "#0d1117", weight: 1, fillColor: "#e6edf3", fillOpacity: 1,
  }).bindTooltip(`${site.name}: sees out to ${Math.round(site.max_range_m / 1852)} nmi`)
    .addTo(radarLayer);
}

function setMode(next) {
  if (next === mode) return;
  if (mode === "live") live.stop();
  if (mode === "replay") player.leave();
  stopFollow();
  mode = next;
  document.querySelectorAll(".modes button").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.mode === next)));
  $("replay-panel").hidden = next !== "replay";
  select(null);
  clearTracks();
  darkPanel.reset();
  radarLayer.clearLayers();
  if (next === "live") live.start();
  else player.enter();
}

document.querySelectorAll(".modes button").forEach((b) =>
  b.addEventListener("click", () => setMode(b.dataset.mode)));

// Start where there's something to see: Live if the feed is running,
// Recordings otherwise. First visit: the welcome guide on top.
(async function init() {
  let liveRunning = false;
  try {
    const s = await getJSON("/api/status");
    const last = Math.max(ts(s.last_adsb_at) || 0, ts(s.last_radar_at) || 0);
    liveRunning = s.tracks > 0 && Date.now() / 1000 - last < 120;
  } catch (e) { /* server unreachable: Recordings will say so */ }
  setMode(liveRunning ? "live" : "replay");
  let seen = false;
  try {
    seen = localStorage.getItem(WELCOME_KEY) === "1";
  } catch (e) { /* no storage: treat as a first visit */ }
  if (!seen) openWelcome();
})();
