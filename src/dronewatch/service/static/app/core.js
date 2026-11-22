// Спільне для всіх файлів дашборда: підписи, глобальний стан сторінки, екранування, запити до API.
// Файли — ES-модулі: залежності між ними явні (import/export), точка входу main.js.
export const COLORS = { NONE: "#33e6c2", WARNING: "#ffb020", CRITICAL: "#ff3b63" };
// Колір цілі = чия вона (приналежність), а зона загрози показана кільцем навколо: так два різні питання не зливаються в один колір.
export const SIDE = {
  enemy:   { color: "#ff3b63", label: "ВОРОГ" },
  friend:  { color: "#33e6c2", label: "СВІЙ" },
  unknown: { color: "#b9c6cf", label: "НЕ ВИЗНАЧЕНО" },
};
export const LEVEL_UA = { NONE: "—", WARNING: "увага", CRITICAL: "загроза" };
export const KIND_UA = { radar: "радар", rf: "RF", acoustic: "акуст." };
export const SENSOR_UA = { OK: "справний", WAITING: "очікуємо", LOST: "немає звʼязку" };
export const sensorState = id => ((S.state && S.state.sensors || []).find(s => s.id === id) || {}).state;
// Спільний змінний стан сторінки. Модуль не може переприсвоїти чужу змінну, тож усе, що міняють з різних файлів, лежить тут:
// cfg (/api/config), state (останній кадр з сервера), selected (id цілі), map і popup (MapLibre), me (/api/me), а також
// стан редактора розстановки: editing, tool, drag, suppressClick, reloading.
export const S = { cfg: null, state: null, selected: null, map: null, popup: null, me: null,
  editing: false, tool: null, drag: null, suppressClick: false, reloading: false };
export const trails = new Map();   // id цілі -> останні точки шлейфу
// Роль користувача з /api/me. Лише для показу: сервер усе одно відхиляє заборонені запити (403), UI ховає недоступне.
export const ROLE_UA = { viewer: "перегляд", operator: "оператор", admin: "адмін" };
const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 };
export const can = role => (ROLE_RANK[S.me && S.me.role] ?? -1) >= ROLE_RANK[role];

export function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// Відповідь AI — звичайний текст з markdown-розміткою (**жирний**, переноси рядків), а не HTML. Усе спершу
// екранується через esc(), і лише тоді controlled-регексом додаються свої ж безпечні теги — вхід моделі
// ніколи не потрапляє в розмітку як є.
// Висоту міряє лише радар: трек із самих пеленгів має умовні 60 м трекера, а не виміряні (alt_known від сервера)
export const altText = t => t.alt_known === false ? "—" : String(Math.round(t.z));
export function mdLite(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\n/g, "<br>"); }


// Усі запити до API йдуть сюди: 401 означає, що входу немає або сесія скінчилась, тож ведемо на форму входу
export class LoginRequired extends Error {}
export async function api(path) {
  const response = await fetch(path, { credentials: "same-origin" });
  if (response.status === 401) { location.replace("/login"); throw new LoginRequired(); }
  return response.json();
}

export async function apiPost(path, body) {
  const response = await fetch(path, { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (response.status === 401) { location.replace("/login"); throw new LoginRequired(); }
  return response;
}
