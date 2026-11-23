// ---------- кадр стану з сервера: шапка, таблиця треків, тривоги, список датчиків ----------
import { KIND_UA, LEVEL_UA, PANEL, S, SENSOR_UA, SIDE, altText, can, esc, trails } from "./core.js";
import { select, toast, updateDossier } from "./dossier.js";
import { realInfoHtml, reloadConfig, sensorSpec, typeInfo } from "./editor.js";
import { isSector, pushData, shortName, toLL } from "./map.js";
import { sendToMain } from "./layout.js";
import { soundCheck } from "./sound.js";
import { startReplay } from "./replay.js";
import { stopViewer } from "./viewer3d.js";

export function render(msg) {
  S.liveMsg = msg;
  if (!S.replay) S.state = msg;   // під час відтворення карта показує запис, а тривоги, тости й звук лишаються живими
  document.getElementById("s-tracks").textContent = msg.tracks.filter(t => !t.coasting).length;
  document.getElementById("s-tent").textContent = msg.tentative;
  document.getElementById("s-enemy").textContent = msg.tracks.filter(t => t.side === "enemy" && !t.coasting).length;
  document.getElementById("s-ev").textContent = msg.stats.events_in;
  document.getElementById("s-t").textContent = msg.t.toFixed(0);
  const cal = document.getElementById("calib");
  cal.textContent = msg.calibration.done ? "калібрування: " + msg.calibration.note : "калібрування: не виконано";
  cal.className = "chip " + (msg.calibration.done ? "live" : "off");

  const mq = document.getElementById("mqtt");
  mq.hidden = !msg.mqtt;
  if (msg.mqtt) {
    const idle = msg.mqtt.connected && msg.mqtt.idle_s > 10;   // зʼєднання є, а подій немає: типовий симптом хибної ACL брокера
    mq.textContent = "MQTT " + msg.mqtt.host + (msg.mqtt.tls ? " · TLS" : " · без шифрування") + (!msg.mqtt.connected ? " — немає звʼязку" : idle ? " — подій немає " + Math.round(msg.mqtt.idle_s) + " с (перевірте ACL)" : "");
    mq.className = "chip " + (msg.mqtt.connected && !idle ? "live" : "off");
  }

  if (!S.replay && S.cfg && msg.layout_rev !== undefined && msg.layout_rev !== S.cfg.layout_rev && !S.drag && !S.reloading) {   // розстановку змінили деінде (інша вкладка)
    S.reloading = true;
    reloadConfig().finally(() => { S.reloading = false; });
  }
  renderSensors(msg.sensors || []);
  const st = msg.stats, ov = document.getElementById("overload"), shed = st.events_dropped + st.tracks_capped + st.bearings_truncated;
  const dn = document.getElementById("denied");
  dn.hidden = !st.events_unauthorized;   // події без ключа: або хибно налаштований датчик, або чужий намагається підкинути дані
  if (st.events_unauthorized) dn.textContent = "без чинного ключа: " + st.events_unauthorized + " подій";
  ov.hidden = !shed;
  if (shed) ov.textContent = "перевантаження: відкинуто " + st.events_dropped + " подій" + (st.tracks_capped ? ", треків " + st.tracks_capped : "");

  const ids = new Set(msg.tracks.map(t => t.id));
  if (!S.replay) msg.tracks.forEach(t => {
    const tr = trails.get(t.id) || []; tr.push([t.x, t.y]); if (tr.length > 60) tr.shift(); trails.set(t.id, tr);
  });
  if (!S.replay) [...trails.keys()].forEach(id => { if (!ids.has(id)) trails.delete(id); });

  const rows = msg.tracks.filter(t => !t.coasting).sort((a, b) => ({ CRITICAL: 2, WARNING: 1, NONE: 0 }[b.level] - { CRITICAL: 2, WARNING: 1, NONE: 0 }[a.level]) || a.id - b.id);
  const tbodyHtml = rows.map(t =>
    `<tr data-id="${t.id}" class="${t.id === S.selected ? "sel" : ""}"><td>#${t.id}</td>` +
    `<td class="l"><i class="sd" style="background:${(SIDE[t.side] || SIDE.unknown).color}"></i>${esc(t.model_name ? shortName(t) : "—")}${t.diving ? ' <b style="color:var(--crit)">⬇</b>' : ""}</td>` +
    `<td class="${t.level}"${t.side === "friend" ? ' style="opacity:.55" title="мітка «свій» з RF, не автентифікована"' : ""}>${LEVEL_UA[t.level]}</td><td>${t.speed}</td><td${t.alt_known === false ? ' title="висоту не виміряно: ціль бачать лише пеленгові датчики"' : ""}>${altText(t)}</td></tr>`).join("");
  if (!S.replay && tbodyHtml !== lastTbody) { lastTbody = tbodyHtml; document.getElementById("tbody").innerHTML = tbodyHtml; }   // не чіпаємо DOM, коли нічого не змінилось

  const box = document.getElementById("alerts");
  const counter = document.getElementById("alerts-new");
  const unreviewed = msg.alerts.filter(a => a.status === "new").length;
  counter.hidden = !unreviewed; counter.textContent = unreviewed ? "· без рішення: " + unreviewed : "";
  if (msg.alerts.length) {
    const ordered = msg.alerts.map((a, i) => [a, i]).sort((x, y) => ((x[0].status === "new" ? 0 : 1) - (y[0].status === "new" ? 0 : 1)) || x[1] - y[1]).map(x => x[0]);
    const alertsHtml = ordered.map(a =>
      `<div class="alert ${a.level}${a.side === "friend" ? " friend" : ""}${a.status !== "new" ? " reviewed" : ""}" data-track="${a.track_id}" title="Показати ціль на карті"><span><span class="lvl">${a.level === "CRITICAL" ? "ЗАГРОЗА" : "УВАГА"}</span> <span class="id">#${a.track_id} · ${esc(a.zone)}</span>${alertTag(msg, a)}</span>` +
      `<span class="meta"><span>${new Date(a.ts * 1000).toLocaleTimeString()}</span><span class="sep"></span><span>${a.speed} м/с</span><span class="sep"></span><span>${esc(a.sensors.map(k => KIND_UA[k] || k).join(", "))}</span></span>` +
      reviewHtml(a) + (a.id ? `<button type="button" class="rpbtn" data-replay="${a.id}" title="Показати, що відбувалось за хвилину до й після тривоги">▶ Відтворити</button>` : "") + `</div>`).join("");
    if (alertsHtml !== lastAlerts) { lastAlerts = alertsHtml; rebuildAlerts(box, alertsHtml); }   // чернетка коментаря не входить у HTML: набір тексту не перебудовує список
    const current = new Set();
    msg.alerts.forEach(a => {
      const k = a.ts + ":" + a.track_id + ":" + a.level; current.add(k);
      if (!seenAlerts.has(k) && !firstMsg) toast(a);
    });
    seenAlerts = current;   // памʼятаємо лише тривоги, що ще в списку: множина не росте безмежно
  } else if (lastAlerts !== "") { lastAlerts = ""; seenAlerts = new Set(); box.innerHTML = '<div class="empty">Тривог немає</div>'; }
  if (!PANEL) soundCheck(msg, firstMsg);   // звук лише в головному вікні, інакше сигнал лунав би двічі
  firstMsg = false;
  document.getElementById("truth-row").hidden = !(S.cfg && S.cfg.demo && msg.truth);
  if (S.replay || PANEL) return;
  pushData();
  updateDossier();
}

// Після відтворення таблиця цілей має перемалюватись, навіть якщо живий кадр не змінився
export function invalidateTable() { lastTbody = null; }

// Рішення оператора по тривозі. Усе від користувача (автор, коментар) іде через esc.
let seenAlerts = new Set(), firstMsg = true, lastTbody = null, lastAlerts = null;
const REVIEW_UA = { confirmed: "підтверджено", dismissed: "відхилено" };
const noteDraft = {};   // id тривоги -> незавершений коментар (переживає перебудову DOM між сканами)
function reviewHtml(a) {
  const done = a.status !== "new";
  const tip = done ? `${REVIEW_UA[a.status]} · ${esc(a.reviewed_by || "")} · ${new Date(a.reviewed_at * 1000).toLocaleString()}${a.review_note ? " · " + esc(a.review_note) : ""}` : "";
  if (!can("operator")) return `<span class="rv" data-id="${a.id}">${done ? `<span class="st" title="${tip}">${a.status === "confirmed" ? "✓" : "✕"} ${REVIEW_UA[a.status]}</span>` : ""}</span>`;
  return `<span class="rv" data-id="${a.id}">` +
    (done ? `<span class="st" title="${tip}">${a.status === "confirmed" ? "✓" : "✕"} ${REVIEW_UA[a.status]}</span>` : "") +
    `<button type="button" data-review="confirmed" class="${a.status === "confirmed" ? "on" : ""}">✓ Підтвердити</button>` +
    `<button type="button" data-review="dismissed" class="${a.status === "dismissed" ? "on" : ""}">✕ Відхилити</button>` +
    `<input type="text" maxlength="500" placeholder="коментар (необов'язково)" data-note></span>`;
}
// Перебудова списку не повинна красти фокус у оператора, який пише коментар: зберігаємо поле й виділення, потім повертаємо
function rebuildAlerts(box, html) {
  const act = document.activeElement;
  const keep = act && act.matches && act.matches("input[data-note]") && box.contains(act)
    ? { id: act.closest(".rv").dataset.id, s: act.selectionStart, e: act.selectionEnd } : null;
  box.innerHTML = html;
  box.querySelectorAll("input[data-note]").forEach(i => { const v = noteDraft[i.closest(".rv").dataset.id]; if (v) i.value = v; });
  if (keep) {
    const i = box.querySelector(`.rv[data-id="${keep.id}"] input[data-note]`);
    if (i) { i.focus(); i.setSelectionRange(keep.s, keep.e); }
  }
}
async function sendReview(id, status) {
  try {
    const r = await fetch(`/api/incidents/${id}/review`, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, note: noteDraft[id] || null }) });
    if (r.status === 403) { toast({ level: "WARNING", text: "Недостатньо прав" }); return; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    delete noteDraft[id];   // новий стан прийде з наступним скан-повідомленням
    if (document.activeElement && document.activeElement.matches("input[data-note]")) document.activeElement.blur();   // інакше картка не перебудується
  } catch (err) { toast({ level: "WARNING", text: "Не вдалося зберегти рішення (" + err.message + ")" }); }
}
document.getElementById("alerts").addEventListener("input", e => {
  const inp = e.target.closest("input[data-note]"); if (inp) noteDraft[inp.closest(".rv").dataset.id] = inp.value;
});
document.getElementById("alerts").addEventListener("click", e => {
  const btn = e.target.closest("button[data-review]"); if (!btn) return;
  sendReview(+btn.closest(".rv").dataset.id, btn.dataset.review);
});

// Тип цілі в тривозі: беремо з поточного треку (тривога зберігає лише швидкість і зону)
function alertTag(msg, a) {
  const t = a.model_name ? a : msg.tracks.find(tt => tt.id === a.track_id);
  if (!t || !t.model_name) return "";
  const side = SIDE[t.side] || SIDE.unknown;
  return ` <span class="tag" style="color:${side.color}">${esc(shortName(t))} · ${side.label}</span>`;
}

function specOf(s) {   // «· 1500 м · сектор 120°»: що датчик бачить
  const c = S.cfg && S.cfg.sensors.find(x => x.id === s.id);
  return c ? " · " + c.max_range + " м" + (isSector(c) ? " · сектор " + c.fov_deg + "°" : "") : "";
}

const accuracyText = (kind, sg) => kind === "radar" ? `дальність ±${sg[0]} м, азимут ±${sg[1]}°, кут місця ±${sg[2]}°` : `пеленг ±${sg[0]}°`;
export const openSensors = new Set();   // датчики, чий опис розгорнуто в списку

// Що це за датчик і що він вміє: з його власних даних (працює і в бойовому режимі), плюс реальна модель, якщо це вона
function ownSpecHtml(c) {
  const t = typeInfo(c.type), view = isSector(c) ? `сектор ${c.fov_deg}° (дивиться на ${Math.round(c.heading_deg)}° за компасом)` : "круговий огляд";
  const gives = c.kind === "radar" ? "Дає позицію й висоту цілі." : "Дає лише напрямок на ціль: позицію знаходять два такі датчики разом.";
  return `Дальність виявлення ${c.max_range} м · ${view} · виявлення ${Math.round((c.pd || 0) * 100)} % · похибка: ${esc(accuracyText(c.kind, c.sigma || []))}. ${gives}` +
    (t && t.real ? realInfoHtml(t) : "");
}

export function renderCatalog() {
  const item = t => `<details${S.cfg.editable ? ` draggable="true" data-type="${esc(t.id)}" title="Перетягніть на карту, щоб поставити"` : ""}><summary>${esc(t.name)}<span>${t.max_range} м${t.fov_deg < 360 ? " · " + t.fov_deg + "°" : ""}</span></summary>` +
    `<div class="sp">${esc(sensorSpec(t))}</div>${realInfoHtml(t)}</details>`;
  const types = S.cfg.sensor_types || [], generic = types.filter(t => !t.real), real = types.filter(t => t.real);
  document.getElementById("catalog-body").innerHTML =
    (S.cfg.editable ? "" : `<div class="sp">Розстановка недоступна в бойовому режимі: датчики задає конфіг. Запустіть демо (mode: demo), щоб перетягувати датчики на карту.</div>`) +
    `<div class="grp">Типи (узагальнені)</div>` + generic.map(item).join("") +
    (real.length ? `<div class="grp">Реальні моделі (дані виробника)</div>` + real.map(item).join("") : "");
}

export function renderSensors(list) {
  if (document.activeElement && document.activeElement.matches && document.activeElement.matches("input[data-h]")) return;   // не перемальовуємо, поки вводять висоту
  const kindName = s => { const c = S.cfg && S.cfg.sensors.find(x => x.id === s.id), t = c && typeInfo(c.type); return t ? t.name : (KIND_UA[s.label] || s.label); };
  document.getElementById("sensors").innerHTML = list.map(s =>
    `<div class="sensor ${s.state}"><i></i><span>${esc(s.id)} <span class="st">${esc(kindName(s))}${esc(specOf(s))}</span></span>` +
    `<span class="st">${SENSOR_UA[s.state]}${s.state === "LOST" && s.age_s != null ? " · " + Math.round(s.age_s) + " с" : ""}</span>` +
    (S.editing ? `<label class="sensor-h" title="Висота над землею, м (дах, щогла): впливає на лінію видимості">↑<input type="number" min="0" max="300" step="1" data-h="${esc(s.id)}" value="${esc(String(((S.cfg && S.cfg.sensors.find(x => x.id === s.id)) || {}).height || 0))}" aria-label="Висота датчика ${esc(s.id)}, м"> м</label>` : "") +
    (S.editing ? `<button class="del" type="button" data-del="${esc(s.id)}" title="Прибрати датчик" aria-label="Прибрати датчик ${esc(s.id)}">×</button>` : "") + `</div>` +
    (openSensors.has(s.id) ? `<div class="sensor-spec">${(() => { const c = S.cfg && S.cfg.sensors.find(x => x.id === s.id); return c ? ownSpecHtml(c) : ""; })()}</div>` : "")).join("");
  const lost = list.filter(s => s.state === "LOST"), ok = list.filter(s => s.state === "OK").length;
  document.getElementById("tab-sensors").classList.toggle("warn", lost.length > 0);   // втрачений датчик видно, навіть коли відкрита інша вкладка
  const badge = document.getElementById("sens");
  badge.textContent = "датчики: " + ok + "/" + list.length;
  badge.className = "chip " + (lost.length ? "off" : ok === list.length ? "live" : "");
  const banner = document.getElementById("degraded");
  banner.hidden = !lost.length;
  if (lost.length) {
    const radarLost = lost.some(s => s.label === "radar");
    banner.textContent = "Втрачено звʼязок: " + lost.map(s => s.id).join(", ") + ". Охорона працює в обмеженому режимі" +
      (radarLost ? ": без радара позиція цілі рахується лише з пеленгів, точність нижча, підтвердження радаром не вимагається." : ".");
  }
}

document.getElementById("tbody").addEventListener("click", e => {
  const tr = e.target.closest("tr"); if (!tr) return;
  const id = +tr.dataset.id;
  S.selected = S.selected === id ? null : id;   // повторний клік знімає виділення
  if (PANEL) { sendToMain({ select: S.selected }); invalidateTable(); return; }   // карти тут немає: наводить головне вікно
  const t = S.state && S.state.tracks.find(tt => tt.id === S.selected);
  if (t && S.map) S.map.easeTo({ center: toLL(t.x, t.y), duration: 600 });
  select();
});
document.getElementById("truth").addEventListener("change", pushData);

// Клік по тривозі наводить карту на ціль і відкриває її досьє
document.getElementById("alerts").addEventListener("click", e => {
  if (e.target.closest(".rv")) return;   // кнопки й коментар рішення не наводять карту на ціль
  const rp = e.target.closest("button[data-replay]");
  if (rp) { if (PANEL) sendToMain({ replay: +rp.dataset.replay }); else startReplay(+rp.dataset.replay); return; }
  if (PANEL) { const c = e.target.closest(".alert[data-track]"); if (c) sendToMain({ select: +c.dataset.track }); return; }
  const card = e.target.closest(".alert[data-track]"); if (!card || !S.map) return;
  const id = +card.dataset.track;
  if (!S.state || !S.state.tracks.some(t => t.id === id)) { toast({ level: "WARNING", track_id: id, zone: "ціль вже не відстежується" }); return; }
  if (S.popup) { const p = S.popup; S.popup = null; stopViewer(); p.remove(); }   // нове досьє відкриється біля цілі й карта зсунеться до неї
  S.selected = id;
  select();
});
