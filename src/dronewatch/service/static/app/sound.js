// ---------- звук для ворожих цілей ----------
// Браузер не дає грати звук, доки людина не клікнула по сторінці, тому AudioContext створюється при першому жесті.
let soundOn = true, audio = null, lastAlarm = 0;
try { soundOn = localStorage.getItem("dw-sound") !== "off"; } catch (err) { /* без сховища звук просто увімкнено */ }
function ensureAudio() {
  if (!audio) { try { audio = new (window.AudioContext || window.webkitAudioContext)(); } catch (err) { return null; } }
  if (audio.state === "suspended") audio.resume().then(updateSoundBtn).catch(() => {});
  return audio;
}
function updateSoundBtn() {
  const b = document.getElementById("sound"), locked = soundOn && !(audio && audio.state === "running");
  b.textContent = "♪ звук: " + (!soundOn ? "вимк." : locked ? "клікніть, щоб дозволити" : "увімк.");
  b.style.color = soundOn && !locked ? "var(--ok)" : "";
}
function tone(freq, at, dur, gain = 0.08, type = "square") {
  const o = audio.createOscillator(), g = audio.createGain();
  o.type = type; o.frequency.setValueAtTime(freq, audio.currentTime + at);
  g.gain.setValueAtTime(0.0001, audio.currentTime + at);
  g.gain.exponentialRampToValueAtTime(gain, audio.currentTime + at + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + at + dur);
  o.connect(g).connect(audio.destination); o.start(audio.currentTime + at); o.stop(audio.currentTime + at + dur + 0.02);
}
// Три сигнали, що відрізняються на слух: пікірування (швидкі спадні), загроза (дві ноти двічі), увага (одна м'яка)
const ALARMS = {
  dive: () => [1400, 1150, 950, 780].forEach((f, i) => tone(f, i * 0.13, 0.11, 0.1)),
  critical: () => [880, 660, 880, 660].forEach((f, i) => tone(f, i * 0.2, 0.17, 0.09)),
  warning: () => tone(620, 0, 0.25, 0.06, "triangle"),
};
function alarm(kind) {
  if (!soundOn || !ensureAudio() || audio.state !== "running" || performance.now() - lastAlarm < 1000) return;
  lastAlarm = performance.now();
  ALARMS[kind]();
}
const announced = { dive: new Set(), critical: new Set(), warning: new Set() };
function soundCheck(msg, silent) {
  const ids = new Set(msg.tracks.map(t => t.id));
  for (const set of Object.values(announced)) for (const id of set) if (!ids.has(id)) set.delete(id);
  let loudest = null;   // за один кадр звучить лише найважливіше: пікірування > загроза > увага
  for (const t of msg.tracks) {
    if (t.coasting || t.side !== "enemy") { announced.dive.delete(t.id); continue; }
    if (t.diving) { if (!announced.dive.has(t.id)) { announced.dive.add(t.id); loudest = "dive"; } } else announced.dive.delete(t.id);
    const kind = t.level === "CRITICAL" ? "critical" : t.level === "WARNING" ? "warning" : null;
    if (kind && !announced[kind].has(t.id)) {
      announced[kind].add(t.id); if (kind === "warning") announced.critical.delete(t.id);
      if (loudest !== "dive" && (loudest !== "critical" || kind === "critical")) loudest = loudest === "critical" ? "critical" : kind;
    }
  }
  if (loudest && !silent) alarm(loudest);   // при першому кадрі після відкриття сторінки мовчимо: лише оновлюємо, що вже відоме
}
document.getElementById("sound").addEventListener("click", () => {
  soundOn = !soundOn;
  try { localStorage.setItem("dw-sound", soundOn ? "on" : "off"); } catch (err) { /* не критично */ }
  if (soundOn) { ensureAudio(); setTimeout(() => { lastAlarm = 0; alarm("warning"); updateSoundBtn(); }, 50); }   // пробний сигнал
  updateSoundBtn();
});
addEventListener("pointerdown", ensureAudio, { once: true });
addEventListener("keydown", ensureAudio, { once: true });
updateSoundBtn();
