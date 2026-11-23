// ---------- відтворення інциденту: запис кадрів цілей навколо тривоги (GET /api/incidents/{id}/replay) на тій самій карті ----------
// Під час відтворення карта показує запис, а живі кадри лише оновлюють тривоги, тости й звук (render у panels.js): загроза
// в реальному часі не губиться. Запис містить і розстановку датчиків та зони на момент інциденту, тож їх підміняємо й повертаємо.
import { S, trails } from "./core.js";
import { toast, updateDossier } from "./dossier.js";
import { pushData, toLL } from "./map.js";
import { invalidateTable, render } from "./panels.js";

const SIDE_NAME = { e: "enemy", f: "friend", u: "unknown" }, LEVEL_NAME = ["NONE", "WARNING", "CRITICAL"], TRAIL_FRAMES = 60;
const $ = id => document.getElementById(id);

function toTrack(r) {   // [id, x, y, z, vx, vy, рівень, приналежність, coasting, висота виміряна, пікірує]
  const [id, x, y, z, vx, vy, level, side, coasting, alt, diving] = r;
  return { id, x, y, z, vx, vy, level: LEVEL_NAME[level] || "NONE", side: SIDE_NAME[side] || "unknown", coasting: !!coasting,
    alt_known: alt !== 0, diving: !!diving, speed: Math.round(Math.hypot(vx, vy) * 10) / 10, vz: 0, ttg: null, kinds: [], err: 0, score: 0, model_name: null };
}

function label(rp) {
  const s = Math.round(rp.data.frames[rp.i][0]);
  return "T" + (s < 0 ? "−" + -s : s > 0 ? "+" + s : "±0") + " с" + (rp.i === rp.data.frames.length - 1 && !rp.data.complete ? " (дописується)" : "");
}

function showFrame() {
  const rp = S.replay; if (!rp) return;
  const frames = rp.data.frames;
  S.state = { tracks: frames[rp.i][1].map(toTrack), sensors: [], truth: null };
  trails.clear();
  for (let j = Math.max(0, rp.i - TRAIL_FRAMES + 1); j <= rp.i; j++) {
    for (const r of frames[j][1]) { const q = trails.get(r[0]) || []; q.push([r[1], r[2]]); trails.set(r[0], q); }
  }
  $("rp-seek").value = rp.i; $("rp-time").textContent = label(rp);
  pushData();
}

function setPlaying(on) {
  const rp = S.replay; if (!rp) return;
  clearInterval(rp.timer); rp.timer = null; rp.playing = on;
  $("rp-play").textContent = on ? "⏸" : "▶"; $("rp-play").setAttribute("aria-label", on ? "Пауза" : "Відтворити");
  if (!on) return;
  if (rp.i >= rp.data.frames.length - 1) { rp.i = 0; showFrame(); }
  rp.timer = setInterval(() => {
    if (rp.i >= rp.data.frames.length - 1) { setPlaying(false); return; }
    rp.i++; showFrame();
  }, Math.max(30, (rp.data.step || 1) * 1000 / rp.speed));
}

function exitReplay() {
  const rp = S.replay; if (!rp) return;
  clearInterval(rp.timer);
  Object.assign(S.cfg, rp.saved);
  trails.clear(); S.replay = null; S.selected = null;
  $("replay").hidden = true;
  S.state = S.liveMsg;
  invalidateTable();
  if (S.liveMsg) render(S.liveMsg);
  pushData(); updateDossier();
}

function enterReplay(data) {
  if (S.replay) exitReplay();
  const frames = data.frames, setup = data.setup || {};
  S.replay = { data, i: 0, playing: false, speed: 1, timer: null,
    saved: { sensors: S.cfg.sensors, zones: S.cfg.zones, origin: S.cfg.origin } };
  S.cfg.sensors = setup.sensors || []; S.cfg.zones = setup.zones || []; if (setup.origin) S.cfg.origin = setup.origin;
  S.selected = data.target && data.target.track_id != null ? data.target.track_id : null;   // кільце на цілі, що дала тривогу
  const seek = $("rp-seek"); seek.max = frames.length - 1; seek.value = 0;
  $("rp-speed").value = "1";
  $("replay").hidden = false;
  updateDossier();
  const p = data.target && data.target.pos;
  if (S.map && p) S.map.easeTo({ center: toLL(p[0], p[1]), duration: 600 });
  S.replay.i = Math.max(0, frames.findIndex(f => f[0] >= 0) - Math.round(15 / (data.step || 1)));   // починаємо за ~15 с до тривоги
  showFrame(); setPlaying(true);
}

export async function startReplay(id) {
  if (S.editing) { toast({ level: "WARNING", text: "Закінчіть розстановку датчиків, потім відтворюйте" }); return; }
  let r;
  try { r = await fetch(`/api/incidents/${id}/replay`, { credentials: "same-origin" }); }
  catch (err) { toast({ level: "WARNING", text: "Не вдалося завантажити запис: сервіс недоступний" }); return; }
  if (r.status === 401) { location.replace("/login"); return; }
  if (r.status === 404) { toast({ level: "WARNING", text: "Запису відтворення для цієї тривоги немає" }); return; }
  if (!r.ok) { toast({ level: "WARNING", text: "Не вдалося завантажити запис (HTTP " + r.status + ")" }); return; }
  const data = await r.json();
  if (!data.frames || !data.frames.length) { toast({ level: "WARNING", text: "Запис порожній" }); return; }
  enterReplay(data);
}

$("rp-play").addEventListener("click", () => { if (S.replay) setPlaying(!S.replay.playing); });
$("rp-close").addEventListener("click", exitReplay);
$("rp-seek").addEventListener("input", e => { if (S.replay) { S.replay.i = +e.target.value; showFrame(); } });
$("rp-speed").addEventListener("change", e => { if (S.replay) { S.replay.speed = +e.target.value; if (S.replay.playing) setPlaying(true); } });
addEventListener("keydown", e => { if (e.key === "Escape" && S.replay) exitReplay(); });
