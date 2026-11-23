// ---------- карта ----------
// Система координат датчиків — метри (схід, північ) від точки (0, 0). cfg.origin каже, де ця точка лежить на Землі.
import { COLORS, LOST_SHORT, S, SIDE, sensorState, trails } from "./core.js";
import { select } from "./dossier.js";
import { addSensor, scheduleCoverage, setupSensorEditing } from "./editor.js";

export const M_PER_DEG = 111320;
export function toLL(x, y) {
  const o = S.cfg.origin;
  return [o.lon + x / (M_PER_DEG * Math.cos(o.lat * Math.PI / 180)), o.lat + y / M_PER_DEG];
}
function circleRing(cx, cy, r, n = 72) {
  const ring = [];
  for (let i = 0; i <= n; i++) { const a = (i / n) * 2 * Math.PI; ring.push(toLL(cx + r * Math.cos(a), cy + r * Math.sin(a))); }
  return ring;
}
export const fc = features => ({ type: "FeatureCollection", features });
export const feat = (geometry, properties) => ({ type: "Feature", geometry, properties });
export const shortName = t => (t.model_name || "").split(" (")[0];

const GLYPHS = "/static/fonts/{fontstack}/{range}.pbf";   // шрифти підписів лежать у нас: жодних запитів назовні
const STYLE_URL = "https://tiles.openfreemap.org/styles/dark";   // ЛИШЕ з map.online_tiles: true; видає стороннім серверу, що ви розглядаєте
const FONT = ["Noto Sans Regular"];
// Порожній стиль: охоронні шари (зони, датчики, цілі) працюють і без тайлів, якщо мережі до карти немає.
const BLANK_STYLE = { version: 8, glyphs: GLYPHS, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#060a10" } }] };

// Власна карта (PMTiles зі схемою Protomaps): темний HUD-стиль і 3D-будівлі. Нічого не береться ззовні.
function offlineStyle() {
  const src = "pm", kind = list => ["in", ["get", "kind"], ["literal", list]];
  const width = (w) => ["interpolate", ["exponential", 1.6], ["zoom"], 10, w * 0.4, 14, w * 1.6, 18, w * 9];
  const road = (id, kinds, color, w, minzoom = 0) => ({ id, type: "line", source: src, "source-layer": "roads", minzoom,
    filter: ["all", kind(kinds), ["!=", ["get", "is_tunnel"], true]], layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": color, "line-width": width(w) } });
  const h = ["coalesce", ["get", "height"], 8];
  return {
    version: 8, glyphs: GLYPHS,
    sources: { [src]: { type: "vector", url: "pmtiles://" + location.origin + "/map/paris.pmtiles",
      attribution: "© OpenStreetMap contributors · Protomaps" } },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#060a10" } },
      { id: "earth", type: "fill", source: src, "source-layer": "earth", paint: { "fill-color": "#0d1a24" } },
      { id: "landuse", type: "fill", source: src, "source-layer": "landuse",
        filter: kind(["park", "nature_reserve", "forest", "wood", "grass", "garden", "cemetery", "golf_course", "playground", "pitch", "recreation_ground"]),
        paint: { "fill-color": "#0e2c30", "fill-opacity": 0.9 } },
      { id: "water", type: "fill", source: src, "source-layer": "water", paint: { "fill-color": "#0a2a3c" } },
      road("roads-path", ["path"], "#26394a", 1, 15),
      road("roads-minor", ["minor_road"], "#2d4559", 1.6, 12),
      road("roads-medium", ["medium_road"], "#3c5b73", 2.4, 10),
      road("roads-major", ["major_road"], "#4d7490", 3.2, 8),
      road("roads-highway", ["highway"], "#5f8fb0", 4.2, 6),
      { id: "building-3d", type: "fill-extrusion", source: src, "source-layer": "buildings", minzoom: 13,
        paint: { "fill-extrusion-color": ["interpolate", ["linear"], h, 0, "#14293a", 40, "#1b4a63", 120, "#2a6a8a"],
          "fill-extrusion-height": h, "fill-extrusion-base": ["coalesce", ["get", "min_height"], 0], "fill-extrusion-opacity": 0.92 } },
      { id: "road-names", type: "symbol", source: src, "source-layer": "roads", minzoom: 14, filter: ["has", "name"],
        layout: { "symbol-placement": "line", "text-field": ["get", "name"], "text-font": FONT, "text-size": 10 },
        paint: { "text-color": "#5f7a8c", "text-halo-color": "#060a10", "text-halo-width": 1.4 } },
      { id: "place-names", type: "symbol", source: src, "source-layer": "places", minzoom: 9,
        filter: kind(["locality", "neighbourhood", "macrohood"]),
        layout: { "text-field": ["get", "name"], "text-font": FONT, "text-size": 12, "text-transform": "uppercase", "text-letter-spacing": 0.08 },
        paint: { "text-color": "#7f98a8", "text-halo-color": "#060a10", "text-halo-width": 1.6 } },
    ],
  };
}

function hudStyle(style) {
  style.layers = style.layers.filter(l => l.id !== "building");
  for (const l of style.layers) if (l.id === "background") l.paint["background-color"] = "#060a10";
  const h = ["coalesce", ["get", "render_height"], 8];
  const building3d = {
    id: "building-3d", type: "fill-extrusion", source: "openmaptiles", "source-layer": "building", minzoom: 13,
    paint: {
      "fill-extrusion-color": ["interpolate", ["linear"], h, 0, "#0f1d29", 40, "#143044", 120, "#1d4a63"],
      "fill-extrusion-height": h,
      "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
      "fill-extrusion-opacity": 0.92,
    },
  };
  const firstSymbol = style.layers.findIndex(l => l.type === "symbol");   // будівлі під підписами вулиць
  style.layers.splice(firstSymbol < 0 ? style.layers.length : firstSymbol, 0, building3d);
  return style;
}

function addOverlays() {
  if (S.map.getSource("tracks")) return;
  for (const id of ["coverage", "zones", "ranges", "sensors", "handles", "trails", "vectors", "truth", "stalks", "impact", "forecast", "lost", "tracks"]) S.map.addSource(id, { type: "geojson", data: fc([]) });
  S.map.addLayer({ id: "coverage-fill", source: "coverage", type: "fill", paint: { "fill-color": ["get", "color"], "fill-opacity": 0.34, "fill-antialias": false } });
  const label = (id, source, extra) => S.map.addLayer({ id, source, type: "symbol", layout: { "text-font": FONT, "text-size": 11, "text-allow-overlap": true, ...extra.layout },
    paint: { "text-halo-color": "#060a10", "text-halo-width": 1.6, ...extra.paint } });
  S.map.addLayer({ id: "zones-fill", source: "zones", type: "fill", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": ["get", "color"], "fill-opacity": 0.09 } });
  S.map.addLayer({ id: "zones-line", source: "zones", type: "line", filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "line-color": ["get", "color"], "line-width": 1.6, "line-dasharray": [3, 2], "line-opacity": 0.85 } });
  label("zones-label", "zones", { layout: { "text-field": ["get", "name"], "text-anchor": "bottom" }, paint: { "text-color": ["get", "color"] } });
  // Дальність датчика: легка заливка + чітка лінія кольору датчика. Пеленгові (RF, акустичний) пунктиром: вони дають лише напрямок
  S.map.addLayer({ id: "ranges-fill", source: "ranges", type: "fill", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": ["get", "color"], "fill-opacity": 0.07 } });
  S.map.addLayer({ id: "ranges", source: "ranges", type: "line", filter: ["all", ["==", ["geometry-type"], "Polygon"], ["==", ["get", "bearing"], false]],
    paint: { "line-color": ["get", "color"], "line-width": 2, "line-opacity": 0.9 } });
  S.map.addLayer({ id: "ranges-dash", source: "ranges", type: "line", filter: ["all", ["==", ["geometry-type"], "Polygon"], ["==", ["get", "bearing"], true]],
    paint: { "line-color": ["get", "color"], "line-width": 2.2, "line-opacity": 0.95, "line-dasharray": [2.5, 1.5] } });
  label("ranges-label", "ranges", { layout: { "text-field": ["get", "name"], "text-anchor": "bottom", "text-size": 11.5 }, paint: { "text-color": ["get", "color"] } });
  S.map.addLayer({ id: "trails", source: "trails", type: "line", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 2, "line-opacity": ["get", "opacity"] } });
  S.map.addLayer({ id: "vectors", source: "vectors", type: "line", paint: { "line-color": ["get", "color"], "line-width": 1.6, "line-opacity": 0.9 } });
  S.map.addLayer({ id: "truth", source: "truth", type: "circle", paint: { "circle-radius": 8, "circle-opacity": 0, "circle-stroke-color": "#fff", "circle-stroke-width": 1, "circle-stroke-opacity": 0.55 } });
  S.map.addLayer({ id: "sensors-dot", source: "sensors", type: "circle",
    paint: { "circle-radius": 6, "circle-color": ["get", "color"], "circle-stroke-color": "#060a10", "circle-stroke-width": 2 } });
  S.map.addLayer({ id: "handles-dot", source: "handles", type: "circle",   // ручка повороту секторного датчика (лише в редакторі)
    paint: { "circle-radius": 7, "circle-color": "#eef5f9", "circle-stroke-color": ["get", "color"], "circle-stroke-width": 3 } });
  label("sensors-label", "sensors", { layout: { "text-field": ["get", "name"], "text-offset": [0, 1.1], "text-anchor": "top" }, paint: { "text-color": ["get", "color"] } });
  S.map.addLayer({ id: "impact-line", source: "impact", type: "line", filter: ["==", ["geometry-type"], "LineString"],
    paint: { "line-color": "#ff3b63", "line-width": 2, "line-dasharray": [2, 2] } });
  S.map.addLayer({ id: "impact-pt", source: "impact", type: "circle", filter: ["==", ["geometry-type"], "Point"],
    paint: { "circle-radius": 9, "circle-color": "#ff3b63", "circle-opacity": 0.25, "circle-stroke-color": "#ff3b63", "circle-stroke-width": 2 } });
  label("impact-label", "impact", { layout: { "text-field": ["get", "text"], "text-anchor": "top", "text-offset": [0, 1.2] }, paint: { "text-color": "#ff7a93" } });
  // Прогноз руху: коло розкиду на кожному горизонті, пунктир шляху, підписи часу. Це оцінка за поточною швидкістю
  S.map.addLayer({ id: "forecast-fill", source: "forecast", type: "fill", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": ["get", "color"], "fill-opacity": ["get", "fill"] } });
  S.map.addLayer({ id: "forecast-ring", source: "forecast", type: "line", filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "line-color": ["get", "color"], "line-width": 1.2, "line-opacity": 0.7, "line-dasharray": [2, 2] } });
  S.map.addLayer({ id: "forecast-path", source: "forecast", type: "line", filter: ["==", ["geometry-type"], "LineString"],
    paint: { "line-color": ["get", "color"], "line-width": 1.4, "line-opacity": 0.75, "line-dasharray": [1, 2] } });
  label("forecast-label", "forecast", { layout: { "text-field": ["get", "text"], "text-anchor": "top", "text-offset": [0, 0.5], "text-size": 10.5 }, paint: { "text-color": ["get", "color"] } });
  // Зниклі цілі: останнє місце (×), відрізок «куди могла полетіти», коло пошуку, що росте з часом; з віком блякнуть
  S.map.addLayer({ id: "lost-fill", source: "lost", type: "fill", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": ["get", "color"], "fill-opacity": ["*", 0.12, ["get", "fade"]] } });
  S.map.addLayer({ id: "lost-ring", source: "lost", type: "line", filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "line-color": ["get", "color"], "line-width": 1.6, "line-opacity": ["get", "fade"], "line-dasharray": [3, 2] } });
  S.map.addLayer({ id: "lost-path", source: "lost", type: "line", filter: ["==", ["geometry-type"], "LineString"],
    paint: { "line-color": ["get", "color"], "line-width": 1.4, "line-opacity": ["get", "fade"], "line-dasharray": [1, 2] } });
  S.map.addLayer({ id: "lost-dot", source: "lost", type: "circle", filter: ["==", ["get", "part"], "last"],
    paint: { "circle-radius": 5, "circle-color": "#060a10", "circle-stroke-color": ["get", "color"], "circle-stroke-width": 2, "circle-opacity": ["get", "fade"], "circle-stroke-opacity": ["get", "fade"] } });
  label("lost-label", "lost", { layout: { "text-field": ["get", "text"], "text-anchor": "top", "text-offset": [0, 0.9], "text-size": 11 }, paint: { "text-color": ["get", "color"], "text-opacity": ["get", "fade"] } });
  S.map.addLayer({ id: "tracks-halo", source: "tracks", type: "circle",   // кільце = зона загрози, а не приналежність
    paint: { "circle-radius": 13, "circle-opacity": 0, "circle-stroke-color": ["get", "halo"], "circle-stroke-width": ["get", "haloW"], "circle-stroke-opacity": 0.95 } });
  S.map.addLayer({ id: "tracks-dot", source: "tracks", type: "circle",
    paint: { "circle-radius": 6.5, "circle-color": ["get", "color"], "circle-opacity": ["get", "opacity"], "circle-stroke-color": "#04100d", "circle-stroke-width": 1.5 } });
  S.map.addLayer({ id: "tracks-sel", source: "tracks", type: "circle", filter: ["==", ["get", "selected"], true],
    paint: { "circle-radius": 17, "circle-opacity": 0, "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 } });
  label("tracks-label", "tracks", { layout: { "text-field": ["get", "label"], "text-variable-anchor": ["left", "right", "top", "bottom"], "text-radial-offset": 1.1,
            "text-size": 12, "text-allow-overlap": false, "symbol-sort-key": ["get", "prio"] },   // підписи не накладаються; важливіші цілі мають пріоритет
    paint: { "text-color": "#eef5f9", "text-opacity": ["get", "opacity"] } });
  S.map.addLayer({ id: "tracks-hit", source: "tracks", type: "circle", paint: { "circle-radius": 20, "circle-opacity": 0.01 } });   // велика зона для кліку
  // Висота цілі: тонкий стовпчик від землі й «голова» на висоті дрона (справжня 3D-висота, видно при нахилі карти).
  // Додаються ОСТАННІМИ: прозорі 3D-шари, що стоять раніше, глушать 2D-шари (точки, підписи) після себе.
  S.map.addLayer({ id: "stalks", source: "stalks", type: "fill-extrusion", filter: ["==", ["get", "part"], "stalk"],
    paint: { "fill-extrusion-color": ["get", "color"], "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.55 } });
  S.map.addLayer({ id: "heads", source: "stalks", type: "fill-extrusion", filter: ["==", ["get", "part"], "head"],
    paint: { "fill-extrusion-color": ["get", "color"], "fill-extrusion-height": ["get", "top"], "fill-extrusion-base": ["get", "base"], "fill-extrusion-opacity": 0.95 } });
  pushData();
}

const SENSOR_COLOR = { radar: "#4fc3ff", rf: "#b48cff", acoustic: "#ffb86b" };

export const isSector = s => s.fov_deg != null && s.fov_deg < 359.9;

// Контур сектора огляду: центр, дуга від heading - fov/2 до heading + fov/2, назад у центр. Кути компасні (північ = 0, схід = 90).
function sectorRing(s) {
  const pts = [toLL(s.pos[0], s.pos[1])], n = 28, a0 = s.heading_deg - s.fov_deg / 2;
  for (let i = 0; i <= n; i++) {
    const a = (a0 + s.fov_deg * i / n) * Math.PI / 180;
    pts.push(toLL(s.pos[0] + s.max_range * Math.sin(a), s.pos[1] + s.max_range * Math.cos(a)));
  }
  pts.push(pts[0]);
  return pts;
}

export function pushZones() {
  if (!S.map || !S.cfg || !S.map.getSource("zones")) return;
  S.map.getSource("zones").setData(fc(S.cfg.zones.flatMap(z => {
    const c = COLORS[z.level];
    return [feat({ type: "Polygon", coordinates: [circleRing(z.center[0], z.center[1], z.radius)] }, { color: c, name: z.name }),
            feat({ type: "Point", coordinates: toLL(z.center[0], z.center[1] + z.radius) }, { color: c, name: z.name })];
  })));
}

// Датчики окремо від решти: під час перетягування оновлюються лише вони, а не всі шари
export function pushSensors() {
  if (!S.map || !S.cfg || !S.map.getSource("sensors")) return;
  const items = S.cfg.sensors.map(s => ({ s, lost: sensorState(s.id) === "LOST", color: SENSOR_COLOR[s.label] || "#4fc3ff" }));
  S.map.getSource("ranges").setData(fc(items.flatMap(({ s, lost, color }) => {
    const c = lost ? "#ff3863" : color, bearing = s.kind === "bearing";
    return [feat({ type: "Polygon", coordinates: [isSector(s) ? sectorRing(s) : circleRing(s.pos[0], s.pos[1], s.max_range)] }, { color: c, bearing }),
            feat({ type: "Point", coordinates: toLL(s.pos[0], s.pos[1] + s.max_range) },   // підпис на північній точці кола: що це за коло
              { color: c, name: s.id + " · видно до " + s.max_range + " м" + (bearing ? " (лише напрямок)" : "") })];
  })));
  S.map.getSource("handles").setData(fc(S.editing ? items.filter(({ s }) => isSector(s)).map(({ s, color }) => {
    const a = s.heading_deg * Math.PI / 180, d = s.max_range * 0.35;   // ручка на осі огляду; тягнеш, і датчик повертається
    return feat({ type: "Point", coordinates: toLL(s.pos[0] + d * Math.sin(a), s.pos[1] + d * Math.cos(a)) }, { sid: s.id, color });
  }) : []));
  S.map.getSource("sensors").setData(fc(items.map(({ s, lost, color }) => feat({ type: "Point", coordinates: toLL(s.pos[0], s.pos[1]) },
    { color: lost ? "#ff3863" : color, sid: s.id,
      name: s.id + " · " + s.max_range + " м" + (isSector(s) ? " · " + s.fov_deg + "°" : "") + (lost ? " — НЕМАЄ ЗВʼЯЗКУ" : "") }))));
  scheduleCoverage();
}

// ---------- плавний рух ----------
// Сервер шле кадр раз на scan_dt (0,5 с), тож без згладжування цілі стрибають. Між кадрами малюємо проміжні положення:
// від того, де ціль зараз намальована, до положення, де вона має бути в момент наступного кадру (виміряна позиція + швидкість * scan_dt).
// Так рух безперервний і не відстає від реальності на цілий кадр. Кадр, що запізнився, продовжує рух по швидкості, але не довше
// MAX_COAST секунд: ціль не відлітає далеко за ненадійними даними. Під час відтворення інциденту й за prefers-reduced-motion малюємо як є.
const MAX_COAST = 1.0, SNAP_M = 150, DRAW_MS = 33;
const anim = new Map();   // id цілі -> { fx, fy (звідки), tx, ty (куди), vx, vy, t0 (мс) }
let animFrame = null;
const reduced = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const scanDt = () => (S.cfg && S.cfg.scan_dt) || 0.5;

function shown(a, now) {   // де ціль намальована в момент now
  const dt = scanDt(), u = (now - a.t0) / 1000 / dt;
  if (u <= 1) return [a.fx + (a.tx - a.fx) * Math.max(u, 0), a.fy + (a.ty - a.fy) * Math.max(u, 0)];
  const extra = Math.min((u - 1) * dt, MAX_COAST);
  return [a.tx + a.vx * extra, a.ty + a.vy * extra];
}

function setTargets(tracks, now) {
  const dt = scanDt(), ids = new Set(tracks.map(t => t.id));
  for (const id of [...anim.keys()]) if (!ids.has(id)) anim.delete(id);
  for (const t of tracks) {
    const old = anim.get(t.id), tx = t.x + t.vx * dt, ty = t.y + t.vy * dt;
    let fx = t.x, fy = t.y;   // нова ціль стартує з виміряної позиції
    if (old) {
      [fx, fy] = shown(old, now);
      if (Math.hypot(fx - t.x, fy - t.y) > SNAP_M) { fx = t.x; fy = t.y; }   // стрибок (скидання, нова розстановка): не «пливемо» через пів карти
    }
    anim.set(t.id, { fx, fy, tx, ty, vx: t.vx, vy: t.vy, t0: now });
  }
}

let animFor = null;
export function displayTracks(now) {
  const src = S.state ? S.state.tracks : [];
  if (S.replay || reduced()) return src;
  if (S.state !== animFor) { animFor = S.state; setTargets(src, now); }
  return src.map(t => { const a = anim.get(t.id); if (!a) return t; const [x, y] = shown(a, now); return { ...t, x, y }; });
}

function startAnimLoop() {
  if (animFrame !== null) return;
  let last = 0;
  const tick = now => {
    animFrame = requestAnimationFrame(tick);
    if (now - last < DRAW_MS || !S.map || !S.map.getSource("tracks") || S.replay || reduced() || !S.state) return;
    last = now;
    drawTracks(now);
  };
  animFrame = requestAnimationFrame(tick);
}

export function pushData() {
  if (!S.map || !S.cfg || !S.map.getSource("tracks")) return;
  pushZones();
  pushSensors();
  drawTracks(performance.now());
  startAnimLoop();
  const tracks = S.state ? S.state.tracks : [];
  const set = (id, features) => S.map.getSource(id).setData(fc(features));
  drawLost();
  set("trails", tracks.map(t => feat({ type: "LineString", coordinates: (trails.get(t.id) || []).map(p => toLL(p[0], p[1])) },
    { color: (SIDE[t.side] || SIDE.unknown).color, opacity: t.coasting ? 0.25 : 0.65 })).filter(f => f.geometry.coordinates.length > 1));
  const showTruth = document.getElementById("truth").checked && S.state && S.state.truth;
  set("truth", showTruth ? Object.values(S.state.truth).map(p => feat({ type: "Point", coordinates: toLL(p[0], p[1]) }, {})) : []);
}

// Усе, що залежить від положення цілі, перемальовується з проміжними координатами
function drawTracks(now) {
  if (!S.map || !S.map.getSource("tracks")) return;
  const set = (id, features) => S.map.getSource(id).setData(fc(features));
  const tracks = displayTracks(now);
  set("tracks", tracks.map(t => feat({ type: "Point", coordinates: toLL(t.x, t.y) }, {
    id: t.id, prio: t.id === S.selected ? 0 : t.diving ? 1 : t.level === "CRITICAL" ? 2 : t.level === "WARNING" ? 3 : 4,
    color: (SIDE[t.side] || SIDE.unknown).color, opacity: t.coasting ? 0.4 : 1, selected: t.id === S.selected,
    halo: t.diving ? COLORS.CRITICAL : COLORS[t.level], haloW: t.diving ? 4.5 : t.level === "CRITICAL" ? 3.5 : t.level === "WARNING" ? 2 : 0,
    label: "#" + t.id + (t.coasting ? " ?" : "") + (t.model_name ? " · " + shortName(t) : "") + (t.alt_known === false ? "" : " · " + Math.round(t.z) + " м") + (t.diving ? " ↓" : "")   // "↓" (U+2193) є у збережених діапазонах шрифту; "⬇" ні, а помилка гліфа ламає весь шар підписів,
  })));
  set("stalks", tracks.filter(t => !t.coasting && t.alt_known !== false && t.z > 1).flatMap(t => {
    const color = (SIDE[t.side] || SIDE.unknown).color, ring = half => [[-half, -half], [half, -half], [half, half], [-half, half], [-half, -half]]
      .map(([dx, dy]) => toLL(t.x + dx, t.y + dy));
    return [feat({ type: "Polygon", coordinates: [ring(1.2)] }, { part: "stalk", color, top: t.z, base: 0 }),
            feat({ type: "Polygon", coordinates: [ring(5)] }, { part: "head", color, top: t.z + 3, base: Math.max(t.z - 3, 0) })];
  }));
  set("impact", tracks.filter(t => t.diving && t.ttg).flatMap(t => {
    const ix = t.x + t.vx * t.ttg, iy = t.y + t.vy * t.ttg;   // де ціль впаде, якщо збереже курс і швидкість зниження
    return [feat({ type: "LineString", coordinates: [toLL(t.x, t.y), toLL(ix, iy)] }, {}),
            feat({ type: "Point", coordinates: toLL(ix, iy) }, { text: "падіння ~" + Math.round(t.ttg) + " с" })];
  }));
  drawForecast(tracks, set);
  set("vectors", tracks.map(t => feat({ type: "LineString", coordinates: [toLL(t.x, t.y), toLL(t.x + t.vx * 5, t.y + t.vy * 5)] },   // де буде за 5 с
    { color: (SIDE[t.side] || SIDE.unknown).color })));
}

document.getElementById("fc-on").addEventListener("change", () => pushData());

const LOST_COLOR = { enemy: "#ff7a93", friend: "#33e6c2", unknown: "#b9c6cf" };

// Прогноз: для виділеної цілі кола на 5, 10 і 15 с з підписами; для решти загрозливих (не «свій», рівень не NONE) одне коло на 10 с.
// Початок шляху береться з намальованого (плавного) положення, а точки прогнозу приходять з сервера.
function drawForecast(tracks, set) {
  const on = document.getElementById("fc-on");
  if (!on || !on.checked || S.replay) { set("forecast", []); return; }
  set("forecast", tracks.filter(t => !t.coasting && t.forecast).flatMap(t => {
    const sel = t.id === S.selected, threat = t.side !== "friend" && t.level !== "NONE";
    if (!sel && !threat) return [];
    const color = (SIDE[t.side] || SIDE.unknown).color, pts = sel ? t.forecast : [t.forecast[1]];
    return [feat({ type: "LineString", coordinates: [toLL(t.x, t.y), ...pts.map(p => toLL(p.x, p.y))] }, { color }),
      ...pts.flatMap(p => [feat({ type: "Polygon", coordinates: [circleRing(p.x, p.y, p.r)] }, { color, fill: sel ? 0.07 : 0.045 }),
        ...(sel ? [feat({ type: "Point", coordinates: toLL(p.x, p.y) }, { color, text: p.t + " с · ±" + Math.round(p.r) + " м" })] : [])])];
  }));
}

// Зниклі цілі (S.state.lost від сервера): де бачили востаннє й де шукати. Блякнуть за 90 с
function drawLost() {
  const lost = (S.state && S.state.lost) || [];
  S.map.getSource("lost").setData(fc(lost.flatMap(r => {
    const color = LOST_COLOR[r.side] || LOST_COLOR.unknown, fade = Math.max(0.3, 1 - r.age / 90), s = r.search;
    return [feat({ type: "Polygon", coordinates: [circleRing(s.fx, s.fy, s.r)] }, { color, fade }),
      feat({ type: "LineString", coordinates: [toLL(r.x, r.y), toLL(s.fx, s.fy)] }, { color, fade }),
      feat({ type: "Point", coordinates: toLL(r.x, r.y) }, { color, fade, part: "last", text: "" }),
      feat({ type: "Point", coordinates: toLL(s.fx, s.fy) }, { color, fade, part: "area", text: "#" + r.id + " × " + Math.round(r.age) + " с · " + (LOST_SHORT[r.reason] || "зникла") })];
  })));
}

function mapTag(text) {
  let tag = document.getElementById("maptag");
  if (!tag) {
    tag = document.createElement("div"); tag.id = "maptag";
    document.getElementById("mapwrap").appendChild(tag);
  }
  tag.textContent = text;
}

// Режим обертання: ліва кнопка крутить і нахиляє карту замість пересування (права кнопка робить це завжди, це вбудовано)
function setupRotateMode() {
  let on = false, rot = null, moved = false;
  const wrap = document.createElement("div");
  wrap.className = "maplibregl-ctrl maplibregl-ctrl-group";
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "rotbtn"; btn.textContent = "⟲";
  btn.title = "Режим обертання: ліва кнопка миші обертає й нахиляє карту"; btn.setAttribute("aria-pressed", "false");
  wrap.appendChild(btn);
  S.map.addControl({ onAdd: () => wrap, onRemove: () => wrap.remove() }, "bottom-right");
  btn.addEventListener("click", () => {
    on = !on; moved = false; btn.classList.toggle("on", on); btn.setAttribute("aria-pressed", String(on));
    if (on) S.map.dragPan.disable(); else S.map.dragPan.enable();
  });
  S.map.on("mousedown", e => {
    if (!on || e.originalEvent.button !== 0) return;
    rot = { x: e.originalEvent.clientX, y: e.originalEvent.clientY, b: S.map.getBearing(), p: S.map.getPitch() }; moved = false;
  });
  S.map.on("mousemove", e => {
    if (!rot) return;
    const dx = e.originalEvent.clientX - rot.x, dy = e.originalEvent.clientY - rot.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
    S.map.setBearing(rot.b - dx * 0.4); S.map.setPitch(Math.max(0, Math.min(80, rot.p - dy * 0.3)));
  });
  addEventListener("mouseup", () => { rot = null; });
  S.map.rotateDragged = () => moved;   // щоб клік після обертання не знімав виділення цілі
  S.map.rotateOn = () => on;
}

export function initMap() {
  if (typeof maplibregl === "undefined") { mapTag("карту не завантажено (немає WebGL чи бібліотеки)"); return; }
  try {
    const mode = (S.cfg.map && S.cfg.map.mode) || "none";
    if (mode === "offline") {
      if (typeof pmtiles === "undefined") throw new Error("немає бібліотеки pmtiles");
      maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile);
    }
    S.map = new maplibregl.Map({ container: "map", style: mode === "offline" ? offlineStyle() : BLANK_STYLE, center: toLL(0, 0), zoom: 14.6, pitch: 58, bearing: -18,
      maxPitch: 80, attributionControl: { compact: true } });
  } catch (err) { mapTag("карту не завантажено: " + err.message); return; }
  S.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
  setupRotateMode();
  S.map.on("style.load", addOverlays);
  setupSensorEditing();
  S.map.on("click", e => {
    if (S.map.rotateDragged && S.map.rotateDragged()) return;
    if (S.suppressClick) { S.suppressClick = false; return; }   // клік після перетягування датчика не знімає виділення цілі
    if (S.editing && S.tool) { addSensor(e.lngLat); return; }
    const hit = S.map.getLayer("tracks-hit") ? S.map.queryRenderedFeatures(e.point, { layers: ["tracks-hit"] })[0] : null;
    S.selected = hit ? Number(hit.properties.id) : null;   // клік повз ціль знімає виділення
    select();
  });
  S.map.on("mouseenter", "tracks-hit", () => { S.map.getCanvas().style.cursor = "pointer"; });
  S.map.on("mouseleave", "tracks-hit", () => { S.map.getCanvas().style.cursor = ""; });
  const mode = (S.cfg.map && S.cfg.map.mode) || "none";
  if (mode === "online") {   // свідомо ввімкнено в конфігу: запити тайлів видають стороннім серверу, яку ділянку розглядає оператор
    mapTag("карта з зовнішніх тайлів (S.map.online_tiles): ділянку видно стороннім");
    fetch(STYLE_URL).then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(style => S.map.setStyle(hudStyle(style), { diff: false }))   // без diff: інакше охоронні шари зникають, а style.load не повторюється
      .catch(err => mapTag("тайли карти недоступні (" + err.message + "): охоронні шари працюють без підкладки"));
  } else if (mode === "none") {
    mapTag("підкладки карти немає: створіть файл командою dronewatch-mapbuild (зовнішні тайли вимкнено)");
  }
}
