// Спільне для всіх файлів дашборда: підписи, глобальний стан сторінки, екранування, запити до API.
// Файли — звичайні <script> (не модулі): спільні змінні верхнього рівня видно в усіх. Порядок у index.html важливий.
const COLORS = { NONE: "#33e6c2", WARNING: "#ffb020", CRITICAL: "#ff3b63" };
// Колір цілі = чия вона (приналежність), а зона загрози показана кільцем навколо: так два різні питання не зливаються в один колір.
const SIDE = {
  enemy:   { color: "#ff3b63", label: "ВОРОГ" },
  friend:  { color: "#33e6c2", label: "СВІЙ" },
  unknown: { color: "#b9c6cf", label: "НЕ ВИЗНАЧЕНО" },
};
const LEVEL_UA = { NONE: "—", WARNING: "увага", CRITICAL: "загроза" };
const KIND_UA = { radar: "радар", rf: "RF", acoustic: "акуст." };
const SENSOR_UA = { OK: "справний", WAITING: "очікуємо", LOST: "немає звʼязку" };
const sensorState = id => ((state && state.sensors || []).find(s => s.id === id) || {}).state;
let cfg = null, state = null, selected = null, trails = new Map(), seenAlerts = new Set(), firstMsg = true, lastTbody = null, lastAlerts = null;
let map = null, popup = null;

function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// Відповідь AI — звичайний текст з markdown-розміткою (**жирний**, переноси рядків), а не HTML. Усе спершу
// екранується через esc(), і лише тоді controlled-регексом додаються свої ж безпечні теги — вхід моделі
// ніколи не потрапляє в розмітку як є.
function mdLite(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\n/g, "<br>"); }


// Усі запити до API йдуть сюди: 401 означає, що входу немає або сесія скінчилась, тож ведемо на форму входу
class LoginRequired extends Error {}
async function api(path) {
  const response = await fetch(path, { credentials: "same-origin" });
  if (response.status === 401) { location.replace("/login"); throw new LoginRequired(); }
  return response.json();
}

async function apiPost(path, body) {
  const response = await fetch(path, { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (response.status === 401) { location.replace("/login"); throw new LoginRequired(); }
  return response;
}
