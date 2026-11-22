// ---------- розстановка датчиків (лише демо: у бойовому режимі датчики з ключами задає конфіг) ----------
// Датчик ставиться кліком по карті, перетягується мишею, прибирається кнопкою ×. Кожна зміна це повна нова розстановка
// на сервері (PUT /api/layout): він перевіряє її й перезапускає симуляцію, тож карта завжди показує те, що рахує рушій.
import { S, api, esc, trails } from "./core.js";
import { M_PER_DEG, fc, feat, pushData, pushSensors, pushZones, toLL } from "./map.js";
import { openSensors, renderSensors } from "./panels.js";
import { stopViewer } from "./viewer3d.js";

let layoutChain = Promise.resolve();
export const typeInfo = id => ((S.cfg && S.cfg.sensor_types) || []).find(t => t.id === id);
const round1 = v => Math.round(v * 10) / 10;

function fromLL(lng, lat) {   // обернене до toLL: градуси -> метри (схід, північ)
  const o = S.cfg.origin;
  return [(lng - o.lon) * M_PER_DEG * Math.cos(o.lat * Math.PI / 180), (lat - o.lat) * M_PER_DEG];
}

function notice(text) {
  const el = document.createElement("div");
  el.className = "toast WARNING"; el.textContent = text;
  document.getElementById("toast").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

export function sensorSpec(t) {
  const sg = t.sigma, acc = t.label === "radar" ? `дальність ±${sg[0]} м, азимут ±${sg[1]}°, висота ±${sg[2]}°` : `пеленг ±${sg[0]}°`;
  const view = t.fov_deg < 360 ? `дивиться в сектор ${t.fov_deg}° (білою ручкою його повертають)` : "круговий огляд";
  return `${t.name}: радіус дії ${t.max_range} м, ${view}, виявлення ${Math.round(t.pd * 100)} %, похибка: ${acc}. ${t.label === "radar" ? "Дає позицію й висоту." : "Дає лише напрямок: потрібні два датчики."}`;
}

// ---------- підсвітка покриття (рахує сервер: POST /api/coverage) ----------
// Клас клітинки буферної зони: що система зможе зробити з ціллю саме тут
const COVERAGE = [
  { name: "blind",  color: "#ff3b63", label: "сліпа зона: ніхто не бачить" },
  { name: "weak",   color: "#ffb020", label: "слабке: лише напрямок, без висоти" },
  { name: "good",   color: "#9be564", label: "добре: радар (позиція й висота)" },
  { name: "strong", color: "#33e6c2", label: "повне: радар + запас + RF-ідентифікація" },
];
let covSig = "", covTimer = null, covSeq = 0;

function clearCoverage() {
  if (S.map && S.map.getSource("coverage")) S.map.getSource("coverage").setData(fc([]));
  document.getElementById("cov-stats").innerHTML = ""; covSig = "";
}

export function scheduleCoverage() {
  if (!document.getElementById("cov-on").checked || !S.cfg || S.replay) return;
  const sig = JSON.stringify(S.cfg.sensors.map(s => [s.id, s.type, s.pos, s.heading_deg]));
  if (sig === covSig || covTimer) return;   // розстановка та сама, або запит уже заплановано (піде з найновішою)
  covTimer = setTimeout(runCoverage, 120);
}

async function runCoverage() {
  covTimer = null;
  const my = ++covSeq, sig = JSON.stringify(S.cfg.sensors.map(s => [s.id, s.type, s.pos, s.heading_deg]));
  const body = { sensors: S.cfg.sensors.map(s => ({ id: s.id, type: s.type, x: round1(s.pos[0]), y: round1(s.pos[1]), heading: (s.heading_deg || 0) % 360 })) };
  try {
    const r = S.editing   // у редакторі рахуємо розстановку, що ще тягнеться; поза ним беремо поточні датчики рушія (працює й у бойовому режимі)
      ? await fetch("/api/coverage", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      : await fetch("/api/coverage", { credentials: "same-origin" });
    if (r.status === 401) { location.replace("/login"); return; }
    if (my !== covSeq) return;   // застаріла відповідь: з'явилась новіша розстановка
    if (!r.ok) { clearCoverage(); covSig = sig; document.getElementById("cov-stats").textContent = "покриття не порахувати: потрібен радар або два пеленгові датчики"; return; }
    if (drawCoverage(await r.json())) covSig = sig;
  } catch (err) { /* сервіс тимчасово недоступний: підсвітка просто лишається попередньою */ }
}

function drawCoverage(c) {
  if (!S.map || !S.map.getSource("coverage")) return false;   // карта ще не готова: спробуємо з наступним оновленням
  const s = c.cell;
  S.map.getSource("coverage").setData(fc(c.cells.map(([ix, iy, k]) => feat({ type: "Polygon", coordinates: [[
    toLL(ix * s, iy * s), toLL((ix + 1) * s, iy * s), toLL((ix + 1) * s, (iy + 1) * s), toLL(ix * s, (iy + 1) * s), toLL(ix * s, iy * s)]] },
    { color: COVERAGE[k].color }))));
  document.getElementById("cov-stats").innerHTML = COVERAGE.slice().reverse().map(k =>
    `<div><i style="background:${k.color}"></i>${esc(k.label)}<b>${c.stats[k.name]} %</b></div>`).join("") +
    `<div class="sub">RF може визначити модель дрона на <b style="margin-left:6px">${c.stats.ident} %</b> зони</div>`;
  return true;
}

const PARAM_UA = { max_range: "дальність", fov: "сектор огляду", sigma: "точність", sigma_range: "точність по дальності", pd: "ймовірність виявлення", clutter: "хибні спрацювання" };

// Реальна модель: що підтвердив виробник, що ми лише припустили, і де це написано
export function realInfoHtml(t) {
  const r = t.real; if (!r) return "";
  const safe = u => /^https:\/\//.test(u);
  const links = r.sources.filter(safe).map((u, i) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">джерело ${i + 1}</a>`).join(", ");
  return `<div class="real-info"><b>${esc(r.vendor)} · ${esc(r.model)}</b><br>` +
    `Підтверджено виробником: ${esc(Object.values(r.known).join("; "))}.<br>` +
    `<span class="assumed">Припущено (виробник не вказав): ${esc(r.assumed.map(k => PARAM_UA[k] || k).join(", "))}.</span><br>` +
    `${esc(r.note)}<br>Ще в даташиті (не вся модель це враховує): ${esc(r.extra.join("; "))}.<br>${links}` +
    (r.files && r.files.length ? ` · файл у проєкті: ${esc(r.files.join(", "))}` : "") + `</div>`;
}

function renderPalette() {
  const btn = t => `<button type="button" data-tool="${esc(t.id)}" class="${S.tool === t.id ? "on" : ""}${t.real ? " real" : ""}" title="${esc(sensorSpec(t))}">${esc(t.name)}</button>`;
  const generic = S.cfg.sensor_types.filter(t => !t.real), real = S.cfg.sensor_types.filter(t => t.real);
  document.getElementById("palette").innerHTML = `<button type="button" data-tool="" class="${S.tool ? "" : "on"}">↖ курсор</button>` + generic.map(btn).join("") +
    (real.length ? `<div class="grp">Реальні моделі (дані виробника)</div>` + real.map(btn).join("") : "");
  const info = S.tool && typeInfo(S.tool);
  document.getElementById("palette-hint").innerHTML = info ? esc(sensorSpec(info) + " Клікніть по карті, щоб поставити; Esc: курсор.") + realInfoHtml(info)
    : "Курсор: тягніть датчики, × у списку прибирає. Оберіть тип, щоб поставити новий кліком по карті. Буферна й критична зони йдуть за радарами.";
  if (S.map) S.map.getCanvas().style.cursor = S.editing && S.tool ? "crosshair" : "";
}

function setEditing(on) {
  S.editing = on; S.tool = null; S.drag = null;
  document.getElementById("editor").hidden = !on;
  const btn = document.getElementById("edit-sensors");
  btn.classList.toggle("on", on); btn.textContent = on ? "✓ Готово" : "✎ Розставити";
  if (on) renderPalette(); else if (S.map) S.map.getCanvas().style.cursor = "";
  renderSensors(S.state ? S.state.sensors || [] : []);
  clearCoverage(); scheduleCoverage();   // редактор і перегляд беруть покриття з різних джерел
  pushSensors();   // ручки повороту секторних датчиків показуються лише в редакторі
}

// Зони виводяться з радарів (те саме правило, що в layout.derive_zones на сервері): навколо кожного радара буферна й критична.
// Тут це лише миттєвий перегляд, щоб зони їхали разом із радаром ще до відповіді сервера; остаточні зони приходять від нього.
// Без радарів зони залежать від центра датчиків: тоді чекаємо сервер.
function previewZones() {
  const radars = S.cfg.sensors.filter(s => s.label === "radar"), r = S.cfg.zone_rule;
  if (!radars.length || !r) return;
  S.cfg.zones = radars.flatMap(s => [
    { name: "Буферна зона " + s.id, level: "WARNING", radius: round1(s.max_range * r.buffer), center: s.pos.slice() },
    { name: "Критична зона " + s.id, level: "CRITICAL", radius: round1(s.max_range * r.critical), center: s.pos.slice() }]);
  pushZones();
}

function applyConfig(nc) {
  S.cfg.sensors = nc.sensors; S.cfg.zones = nc.zones; S.cfg.layout_rev = nc.layout_rev;
  trails.clear(); S.selected = null;   // рушій почав новий сеанс: старі треки й виділення більше нічого не означають
  if (S.popup) { const p = S.popup; S.popup = null; stopViewer(); p.remove(); }
  pushData();
}

export async function reloadConfig() {
  const fresh = await api("/api/config");
  Object.assign(S.cfg, fresh);
  trails.clear(); pushData();
  if (S.editing) renderPalette();
}

function commitLayout() {
  layoutChain = layoutChain.then(async () => {
    const body = { sensors: S.cfg.sensors.map(s => ({ id: s.id, type: s.type, x: round1(s.pos[0]), y: round1(s.pos[1]), heading: (s.heading_deg || 0) % 360 })) };
    try {
      const r = await fetch("/api/layout", { method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.status === 401) { location.replace("/login"); return; }
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        notice(typeof d.detail === "string" ? d.detail : "Розстановку відхилено: " + ((d.detail && d.detail[0] && d.detail[0].msg) || "помилка " + r.status).replace(/^Value error, /, ""));
        await reloadConfig();   // повертаємо те, що справді діє на сервері
        return;
      }
      applyConfig(await r.json());
    } catch (err) { notice("Не вдалося зберегти розстановку: сервіс недоступний"); }
  });
}

export function addSensor(lngLat) {
  const info = typeInfo(S.tool); if (!info) return;
  const [x, y] = fromLL(lngLat.lng, lngLat.lat).map(round1);
  if (S.cfg.sensors.length >= S.cfg.max_sensors) return notice("Забагато датчиків: не більше " + S.cfg.max_sensors);
  if (Math.abs(x) > S.cfg.layout_limit_m || Math.abs(y) > S.cfg.layout_limit_m) return notice("Занадто далеко від центру: ліміт " + S.cfg.layout_limit_m + " м");
  const prefix = info.label === "radar" ? "R" : info.label === "rf" ? "RF" : "AC";
  let n = 1; while (S.cfg.sensors.some(s => s.id === prefix + n)) n++;
  // секторний датчик одразу дивиться на центр охоронюваної ділянки (середина зон; без зон: на початок координат)
  const cz = S.cfg.zones.length ? [S.cfg.zones.reduce((a, z) => a + z.center[0], 0) / S.cfg.zones.length, S.cfg.zones.reduce((a, z) => a + z.center[1], 0) / S.cfg.zones.length] : [0, 0];
  const heading = Math.hypot(cz[0] - x, cz[1] - y) < 10 ? 0 : round1((Math.atan2(cz[0] - x, cz[1] - y) * 180 / Math.PI + 360) % 360);
  S.cfg.sensors.push({ id: prefix + n, type: info.id, kind: info.label === "radar" ? "radar" : "bearing", label: info.label, pos: [x, y],
                     max_range: info.max_range, fov_deg: info.fov_deg, heading_deg: heading, sigma: info.sigma, pd: info.pd });
  pushSensors(); previewZones(); commitLayout();
}

export function setupSensorEditing() {
  S.map.on("mousedown", "sensors-dot", e => {
    if (!S.editing || S.tool || (S.map.rotateOn && S.map.rotateOn()) || e.originalEvent.button !== 0) return;
    e.preventDefault();
    S.drag = { sid: e.features[0].properties.sid, moved: false };
    S.map.dragPan.disable(); S.map.getCanvas().style.cursor = "grabbing";
  });
  S.map.on("mousedown", "handles-dot", e => {   // ручка повороту: тягнеш, і сектор повертається в бік курсора
    if (!S.editing || (S.map.rotateOn && S.map.rotateOn()) || e.originalEvent.button !== 0) return;
    e.preventDefault();
    S.drag = { sid: e.features[0].properties.sid, moved: false, turn: true };
    S.map.dragPan.disable(); S.map.getCanvas().style.cursor = "grabbing";
  });
  S.map.on("mouseenter", "handles-dot", () => { if (S.editing && !S.drag) S.map.getCanvas().style.cursor = "grab"; });
  S.map.on("mouseleave", "handles-dot", () => { if (!S.drag) S.map.getCanvas().style.cursor = S.editing && S.tool ? "crosshair" : ""; });
  S.map.on("mousemove", e => {
    if (!S.drag) return;
    const s = S.cfg.sensors.find(x => x.id === S.drag.sid); if (!s) return;
    if (S.drag.turn) {
      const [x, y] = fromLL(e.lngLat.lng, e.lngLat.lat);
      s.heading_deg = round1((Math.atan2(x - s.pos[0], y - s.pos[1]) * 180 / Math.PI + 360) % 360);
      S.drag.moved = true; pushSensors(); return;
    }
    const lim = S.cfg.layout_limit_m, [x, y] = fromLL(e.lngLat.lng, e.lngLat.lat);
    s.pos = [round1(Math.max(-lim, Math.min(lim, x))), round1(Math.max(-lim, Math.min(lim, y)))];
    S.drag.moved = true; pushSensors(); previewZones();
  });
  addEventListener("mouseup", () => {
    if (!S.drag) return;
    const d = S.drag; S.drag = null;
    if (!(S.map.rotateOn && S.map.rotateOn())) S.map.dragPan.enable();
    S.map.getCanvas().style.cursor = S.editing && !S.tool ? "grab" : "";
    if (d.moved) {
      S.suppressClick = true; setTimeout(() => { S.suppressClick = false; }, 0);   // клік після відпускання приходить одразу; якщо його не буде, прапорець не має «з'їсти» наступний
      commitLayout();
    }
  });
  S.map.on("mouseenter", "sensors-dot", () => { if (S.editing && !S.tool && !S.drag) S.map.getCanvas().style.cursor = "grab"; });
  S.map.on("mouseleave", "sensors-dot", () => { if (!S.drag) S.map.getCanvas().style.cursor = S.editing && S.tool ? "crosshair" : ""; });
}

document.getElementById("edit-sensors").addEventListener("click", () => setEditing(!S.editing));
// Перетягування типу з каталогу (або з палітри) прямо на карту: ставить датчик у місці скидання
document.getElementById("catalog-body").addEventListener("dragstart", e => {
  const d = e.target.closest && e.target.closest("[data-type]"); if (!d || !S.cfg.editable) return;
  e.dataTransfer.setData("text/plain", "sensor:" + d.dataset.type); e.dataTransfer.effectAllowed = "copy";
});
{
  const mapEl = document.getElementById("map");
  mapEl.addEventListener("dragover", e => { if (S.cfg && S.cfg.editable) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
  mapEl.addEventListener("drop", e => {
    const m = /^sensor:(.+)$/.exec(e.dataTransfer.getData("text/plain") || "");
    if (!m || !S.cfg || !S.cfg.editable || !S.map) return;
    e.preventDefault();
    const r = mapEl.getBoundingClientRect();
    if (!S.editing) setEditing(true);
    S.tool = m[1]; addSensor(S.map.unproject([e.clientX - r.left, e.clientY - r.top])); S.tool = null; renderPalette();
  });
}
(() => {   // легенда закриває лише потрібну частину мапи: за замовчуванням згорнута, вибір памʼятається
  const box = document.getElementById("legend"), btn = document.getElementById("legend-toggle");
  const set = collapsed => { box.classList.toggle("collapsed", collapsed); btn.setAttribute("aria-expanded", String(!collapsed)); };
  try { if (localStorage.getItem("legend-open") === "1") set(false); } catch { /* сховище недоступне: лишаємо згорнутою */ }
  btn.addEventListener("click", () => {
    const collapsed = !box.classList.contains("collapsed"); set(collapsed);
    try { localStorage.setItem("legend-open", collapsed ? "0" : "1"); } catch { /* не критично */ }
  });
})();
document.getElementById("cov-on").addEventListener("change", () => { clearCoverage(); scheduleCoverage(); });
document.getElementById("palette").addEventListener("click", e => {
  const b = e.target.closest("button[data-tool]"); if (!b) return;
  S.tool = b.dataset.tool || null; renderPalette();
});
document.getElementById("sensors").addEventListener("click", e => {
  if (e.target.closest("a")) return;   // посилання на джерело в описі не згортає опис
  const row = e.target.closest(".sensor");
  if (row && !e.target.closest("button[data-del]")) {   // клік по датчику розгортає його опис
    const id = row.querySelector("span").firstChild.textContent.trim();
    if (openSensors.has(id)) openSensors.delete(id); else openSensors.add(id);
    renderSensors(S.state ? S.state.sensors || [] : []);
    return;
  }
  const b = e.target.closest("button[data-del]"); if (!b || !S.editing) return;
  S.cfg.sensors = S.cfg.sensors.filter(s => s.id !== b.dataset.del);
  pushSensors(); previewZones(); commitLayout();
});
document.getElementById("layout-reset").addEventListener("click", () => {
  layoutChain = layoutChain.then(async () => {
    try {
      const r = await fetch("/api/layout", { method: "DELETE", credentials: "same-origin" });
      if (r.status === 401) { location.replace("/login"); return; }
      if (r.ok) applyConfig(await r.json()); else notice("Не вдалося скинути розстановку");
    } catch (err) { notice("Не вдалося скинути розстановку: сервіс недоступний"); }
  });
});
addEventListener("keydown", e => { if (e.key === "Escape" && S.editing && S.tool) { S.tool = null; renderPalette(); } });
