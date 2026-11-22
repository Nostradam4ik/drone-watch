// ---------- розкладка під себе: ширина бокової панелі, відкріплення панелей у окремі вікна (на інший монітор) ----------
// Відкріплена панель це та сама сторінка за адресою /panel/<назва>: код малювання один, вхід і ролі ті самі. Вікна не мають
// спільної памʼяті, тож «виділити ціль на карті» і «відтворити інцидент» панель просить у головного вікна через BroadcastChannel
// (лише в межах одного походження й браузера). Головне вікно перевіряє кожне повідомлення: це числа, а не довільні дані.
import { S, PANEL } from "./core.js";
import { toast } from "./dossier.js";

const KEY = "dw-aside-w", DEFAULT_W = 348, MIN_W = 280;
const chan = typeof BroadcastChannel === "function" ? new BroadcastChannel("dronewatch-panels") : null;

export const PANELS = { alerts: "Тривоги", tracks: "Активні треки" };
export function sendToMain(msg) { if (chan) chan.postMessage(msg); }
export function listenPanels(handler) {
  if (chan) chan.onmessage = e => { const d = e.data; if (d && typeof d === "object") handler(d); };
}

const maxWidth = () => Math.max(MIN_W, Math.min(900, Math.round(innerWidth * 0.7)));
const clamp = w => Math.min(maxWidth(), Math.max(MIN_W, Math.round(w)));
function setWidth(w, save) {
  const v = clamp(w);
  document.documentElement.style.setProperty("--aside-w", v + "px");
  if (S.map) S.map.resize();
  if (save) { try { localStorage.setItem(KEY, String(v)); } catch (err) { /* без сховища ширина діє до перезавантаження */ } }
  return v;
}

function initSplitter() {
  const bar = document.getElementById("splitter");
  let saved = NaN;
  try { saved = parseInt(localStorage.getItem(KEY), 10); } catch (err) { /* ширина за замовчуванням */ }
  if (Number.isFinite(saved)) setWidth(saved, false);
  const current = () => document.querySelector("aside").getBoundingClientRect().width;
  bar.addEventListener("pointerdown", e => {
    bar.setPointerCapture(e.pointerId); bar.classList.add("drag"); e.preventDefault();
  });
  bar.addEventListener("pointermove", e => { if (bar.hasPointerCapture(e.pointerId)) setWidth(innerWidth - e.clientX, false); });
  const stop = e => {
    if (!bar.hasPointerCapture(e.pointerId)) return;
    bar.releasePointerCapture(e.pointerId); bar.classList.remove("drag");
    setWidth(current(), true);
  };
  bar.addEventListener("pointerup", stop); bar.addEventListener("pointercancel", stop);
  bar.addEventListener("dblclick", () => { setWidth(DEFAULT_W, false); try { localStorage.removeItem(KEY); } catch (err) { /* нічого */ } });
  bar.addEventListener("keydown", e => {
    const step = e.shiftKey ? 80 : 20;
    if (e.key === "ArrowLeft") { setWidth(current() + step, true); e.preventDefault(); }
    else if (e.key === "ArrowRight") { setWidth(current() - step, true); e.preventDefault(); }
  });
  addEventListener("resize", () => { const w = current(); if (w > maxWidth()) setWidth(w, false); });
}

function initDetach() {
  document.addEventListener("click", e => {
    const btn = e.target.closest("button[data-detach]"); if (!btn || !(btn.dataset.detach in PANELS)) return;
    const name = btn.dataset.detach;
    // Те саме імʼя вікна: повторний клік виводить уже відкріплену панель наперед, а не плодить копії
    const w = window.open("/panel/" + name, "dw-panel-" + name, "popup=yes,width=460,height=780");
    if (!w) toast({ level: "WARNING", text: "Браузер заблокував вікно: дозвольте спливаючі вікна для цієї адреси" });
    else w.focus();
  });
}

// Відкріплене вікно перебудовується одразу при завантаженні модуля, а не після запитів до API: без цього на мить блимала б уся сторінка
if (PANEL) {
  document.body.classList.add("panel", "panel-" + PANEL);
  document.title = "Drone Watch — " + PANELS[PANEL];
}

export function initLayout() {
  if (PANEL) return;
  initSplitter();
  initDetach();
}
