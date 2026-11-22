// Вкладка «Журнал дій» (лише admin): таблиця з /api/audit і позначка цілісності ланцюжка з /api/health. Усі значення з журналу
// (імена, цілі, коментарі приходять від користувачів) потрапляють у DOM лише через textContent, ніколи як HTML.
async function loadAudit() {
  const body = document.getElementById("audit-body"), mark = document.getElementById("audit-integrity");
  try {
    const rows = await api("/api/audit?limit=200");
    if (!Array.isArray(rows)) throw new Error("audit");
    body.replaceChildren(...rows.map(r => {
      const tr = document.createElement("tr");
      [new Date(r.ts * 1000).toLocaleString(), r.user, r.role, r.action, r.target, r.result, r.ip].forEach((v, i) => {
        const td = document.createElement("td"); if (i) td.className = "l"; td.textContent = String(v); tr.append(td);
      });
      return tr;
    }));
    const h = await api("/api/health");
    const a = h && h.audit;
    mark.textContent = !a ? "цілісність невідома" : a.ok ? "ланцюжок цілий" : "ПОШКОДЖЕНО з запису " + a.broken_at;
    mark.className = "chip " + (a && a.ok ? "live" : "off");
  } catch (err) {
    if (err instanceof LoginRequired) return;
    mark.textContent = "журнал недоступний"; mark.className = "chip off";
  }
}
function initAudit() {
  const box = document.getElementById("audit-box");
  box.hidden = !can("admin");
  if (box.hidden) return;
  box.addEventListener("toggle", () => { if (box.open) loadAudit(); });   // оновлюється при кожному відкритті
}
