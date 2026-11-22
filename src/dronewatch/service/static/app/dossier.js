// ---------- досьє цілі ----------
// Радар міряє лише положення й рух. Тип і приналежність дає RF-ідентифікація (протокол дрона), а без неї лишається
// явно позначена оцінка за швидкістю й висотою, а не вигадана класифікація.
function guessClass(t) {
  if (t.speed >= 22) return "швидкісний профіль (можливо, FPV або крилатий)";
  if (t.speed <= 6) return "повільний/завислий профіль (мультикоптер?)";
  return "типовий мультикоптерний профіль";
}
const COMPASS = ["Пн", "ПнСх", "Сх", "ПдСх", "Пд", "ПдЗх", "Зх", "ПнЗх"];
function heading(t) {
  if (t.speed < 1) return "—";
  const deg = (Math.round(Math.atan2(t.vx, t.vy) * 180 / Math.PI) + 360) % 360;
  return deg + "° " + COMPASS[Math.round(deg / 45) % 8];
}

let detailsOpen = false;   // чи розгорнуто «докладніше»: памʼятаємо між оновленнями (вікно перемальовується щоскану)
function dossierParts(t) {
  const side = SIDE[t.side] || SIDE.unknown, ll = toLL(t.x, t.y);
  const ident = t.identity_conflict
    ? `<div class="hint">RF-датчики називають різні моделі для цієї цілі: приналежність не визначено, потрібна перевірка оператором.</div>`
    : t.identified_by
    ? `<div class="hint">Тип і приналежність: RF-ідентифікація протоколу${cfg.demo ? " (симуляція: модель задає емулятор)" : ""}. Радар сам «свій/ворог» не визначає.</div>`
    : `<div class="hint">${esc(guessClass(t))}. Оцінка за швидкістю й висотою, не розпізнавання: RF-ідентифікації немає.</div>`;
  const dive = t.diving ? `<div class="dive">⬇ ПІКІРУЄ: до землі ~${t.ttg} с. Прогнозна точка падіння позначена на карті.</div>` : "";
  const head = `<div class="ttl"><span>ЦІЛЬ #${t.id}</span><b style="color:${side.color}">${side.label}</b></div>` + dive +
    `<div class="model">${esc(t.model_name || (t.identity_conflict ? "суперечливі дані RF" : "тип не визначено"))}</div>` +
    (t.drone_kind ? `<div class="sub">${esc(t.drone_kind)} · ${esc(t.country)}</div>` : "");
  const body = `<dl><dt>швидкість</dt><dd><b>${t.speed} м/с</b> · ${Math.round(t.speed * 3.6)} км/год</dd>` +
    `<dt>висота</dt><dd>${Math.round(t.z)} м</dd>` +
    `<dt>верт. швидкість</dt><dd class="${t.diving ? "CRITICAL" : ""}">${t.vz > 0 ? "+" : ""}${t.vz} м/с${t.ttg ? " · до землі " + t.ttg + " с" : ""}</dd>` +
    `<dt>курс</dt><dd>${heading(t)}</dd>` +
    `<dt>рівень</dt><dd class="${t.level}">${LEVEL_UA[t.level]}</dd>` +
    `<dt>підтвердили</dt><dd>${esc(t.kinds.map(k => KIND_UA[k] || k).join(", ") || "—")}</dd></dl>` +
    `<details${detailsOpen ? " open" : ""}><summary>докладніше</summary><dl>` +
    `<dt>координати</dt><dd>${ll[1].toFixed(5)}, ${ll[0].toFixed(5)}</dd>` +
    `<dt>похибка</dt><dd>±${t.err} м</dd>` +
    `<dt>довіра треку</dt><dd>${t.score}</dd></dl></details>` + ident;
  return { head, body };
}

// Досьє — спливаюче вікно, привʼязане до цілі: летить разом із нею. Його каркас будується один раз (інакше щоразу
// знищувався б canvas із 3D-моделлю), оновлюються лише текстові частини.
function fillDossier(p, t) {
  const parts = dossierParts(t);
  p._head.innerHTML = parts.head;
  p._body.innerHTML = parts.body;
  const det = p._body.querySelector("details");
  if (det) det.addEventListener("toggle", () => { detailsOpen = det.open; });
  const side = SIDE[t.side] || SIDE.unknown, ghost = !t.shape;
  const v = ensureViewer(p._view);
  p._view.hidden = !v;
  if (v) {
    setDroneModel(v, t.shape || "quad", side.color, ghost);
    p._cap.textContent = ghost ? "модель невідома · умовний силует" : "силует класу, не точна копія · тягніть";
    startViewer();
  }
}

// Досьє високе (3D-модель + дані) і відкривається НАД ціллю: якщо ціль надто високо чи скраю, зсуваємо карту,
// щоб вікно не обрізалось краєм екрана.
function ensureFits(p, t) {
  if (popup !== p) return;
  const el = p.getElement(), box = map.getContainer();
  const W = box.clientWidth, H = box.clientHeight, h = el.offsetHeight + 34, half = el.offsetWidth / 2 + 12;
  if (h + 20 > H) return;   // карта замала для такого вікна: нічого не вдієш
  const pt = map.project(toLL(t.x, t.y));
  const dx = Math.max(half, Math.min(W - half, pt.x)), dy = Math.max(h + 20, Math.min(H - 40, pt.y));
  if (Math.abs(dx - pt.x) < 2 && Math.abs(dy - pt.y) < 2) return;
  map.easeTo({ center: toLL(t.x, t.y), offset: [dx - W / 2, dy - H / 2], duration: 450 });
}

function updateDossier() {
  const t = state && state.tracks.find(tt => tt.id === selected);
  if (!t || !map) {
    if (popup) { const p = popup; popup = null; stopViewer(); p.remove(); }
    return;
  }
  if (!popup) {
    const el = document.createElement("div");
    el.innerHTML = '<div class="dhead"></div><div class="viewer"><div class="vcap"></div></div><div class="dbody"></div>';
    const p = new maplibregl.Popup({ closeButton: true, closeOnClick: false, anchor: "bottom", offset: 20, maxWidth: "300px", className: "dossier" });
    p._head = el.querySelector(".dhead"); p._view = el.querySelector(".viewer"); p._cap = el.querySelector(".vcap"); p._body = el.querySelector(".dbody");
    p.on("close", () => { if (popup === p) { popup = null; stopViewer(); selected = null; refreshSelection(); } });
    popup = p;
    p.setLngLat(toLL(t.x, t.y)).setDOMContent(el).addTo(map);
    fillDossier(p, t);
    requestAnimationFrame(() => ensureFits(p, t));
    return;
  }
  popup.setLngLat(toLL(t.x, t.y));
  fillDossier(popup, t);
}

function refreshSelection() {
  pushData();
  document.querySelectorAll("#tbody tr").forEach(tr => tr.classList.toggle("sel", +tr.dataset.id === selected));
}
function select() { refreshSelection(); updateDossier(); }

// Слово за словом — читається як «AI зараз думає», а не як статичний блок тексту, що просто зʼявився.
async function revealAnswer(el, text) {
  const words = text.split(" ");
  let acc = "";
  for (const w of words) {
    acc += (acc ? " " : "") + w;
    el.innerHTML = mdLite(acc) + '<span class="caret"></span>';
    document.getElementById("ai-log").scrollTop = 1e9;
    await new Promise(r => setTimeout(r, 16));
  }
  el.innerHTML = mdLite(text);
}

function toast(a) {
  const el = document.createElement("div");
  el.className = "toast " + a.level;
  el.textContent = a.text || ((a.level === "CRITICAL" ? "ЗАГРОЗА: " : "Увага: ") + "ціль #" + a.track_id + " — " + a.zone);
  document.getElementById("toast").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}
