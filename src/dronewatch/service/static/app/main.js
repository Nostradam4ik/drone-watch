// ---------- старт сторінки, вихід, AI-асистент, WebSocket. Підключається останнім: start() кличе все інше ----------
import { initAudit } from "./audit.js";
import { LoginRequired, ROLE_UA, S, api, apiPost, can, esc } from "./core.js";
import { revealAnswer } from "./dossier.js";
import { initMap } from "./map.js";
import { render, renderCatalog } from "./panels.js";

async function start() {
  S.cfg = await api("/api/config");
  const mode = document.getElementById("mode");
  mode.textContent = S.cfg.demo ? "ДЕМО (симуляція)" : "бойовий режим"; mode.className = "chip " + (S.cfg.demo ? "" : "live");
  S.me = await api("/api/me");
  if (S.me.auth) {   // без автентифікації (демо) ні імені, ні кнопки виходу
    document.getElementById("who-name").textContent = S.me.user + " (" + (ROLE_UA[S.me.role] || S.me.role) + ")";
    document.getElementById("who").hidden = document.getElementById("logout").hidden = false;
  }
  S.cfg.editable = S.cfg.editable && can("admin");   // розстановку датчиків робить лише admin (і лише в демо)
  renderCatalog();
  document.getElementById("edit-sensors").hidden = !S.cfg.editable;
  document.getElementById("ai-box").hidden = !can("operator");
  initAudit();
  initMap();
  connect();
}

document.getElementById("logout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: "{}" });
  location.replace("/login");
});

document.getElementById("ai-form").addEventListener("submit", async e => {
  e.preventDefault();
  const input = document.getElementById("ai-q-input"), btn = e.target.querySelector("button");
  const question = input.value.trim();
  if (!question) return;
  input.value = ""; btn.disabled = true;
  const log = document.getElementById("ai-log");
  const empty = log.querySelector(".empty"); if (empty) empty.remove();
  const qEl = document.createElement("div"); qEl.className = "ai-msg ai-q"; qEl.textContent = question;
  const aEl = document.createElement("div"); aEl.className = "ai-msg ai-a"; aEl.textContent = "…";
  log.append(qEl, aEl);
  log.scrollTop = log.scrollHeight;
  try {
    const r = await apiPost("/api/ai/ask", { question });
    if (!r.ok) { aEl.textContent = "AI недоступний"; return; }
    const body = await r.json();
    await revealAnswer(aEl, body.answer || "(порожня відповідь)");
    if (body.tool_calls && body.tool_calls.length) {
      const trace = document.createElement("div"); trace.className = "tool-trace";
      trace.innerHTML = body.tool_calls.map(c => `<span class="tool-chip">${esc(c.name)}</span>`).join("");
      aEl.appendChild(trace);
    }
  } catch (err) {
    if (err instanceof LoginRequired) return;
    aEl.textContent = "AI недоступний";
  } finally {
    btn.disabled = false;
    log.scrollTop = log.scrollHeight;
  }
});

function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  const conn = document.getElementById("conn");
  ws.onopen = () => { conn.textContent = "на звʼязку"; conn.className = "chip live"; ws.send("hi"); };
  ws.onmessage = e => render(JSON.parse(e.data));
  ws.onclose = async () => {
    conn.textContent = "немає звʼязку"; conn.className = "chip off";
    // Сервер закриває WebSocket і коли сесія скінчилась: тоді перепідключатись марно, потрібно знову увійти
    try { await api("/api/me"); } catch (e) { if (e instanceof LoginRequired) return; }   // мережева помилка: пробуємо знову
    setTimeout(connect, 2000);
  };
}
start();
