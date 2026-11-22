// ---------- 3D-модель дрона в досьє ----------
// Це узагальнений силует КЛАСУ (мультикоптер, дельта-крило, пряме крило, X-крило камікадзе), а не точна копія моделі:
// форму сервер бере з каталогу за ідентифікованою моделлю. Three.js віддаємо самі й вантажимо ліниво, щоб його
// відсутність не ламала решту дашборда.
let three = null, viewer = null;
import("/static/vendor/three.module.min.js").then(m => { three = m; }).catch(err => console.warn("3D-моделі вимкнено:", err));
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const VIEW_H = 130;

function hudMesh(geo, color, ghost) {   // темна заливка + контурні ребра: читається як схема, а не іграшка
  const g = new three.Group();
  g.add(new three.Mesh(geo, new three.MeshBasicMaterial({ color: 0x0b1620, transparent: true, opacity: ghost ? 0.45 : 0.95,
    polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 })));
  g.add(new three.LineSegments(new three.EdgesGeometry(geo, 15),
    new three.LineBasicMaterial({ color, transparent: true, opacity: ghost ? 0.5 : 1 })));
  return g;
}

// Ніс уздовж +X, угору +Y. Повертає { root, rotors }: rotors крутяться в анімації.
function buildDrone(shape, colorHex, ghost) {
  const T = three, color = new T.Color(colorHex), root = new T.Group(), rotors = [];
  const add = (geo, x, y, z, rot = [0, 0, 0]) => { const m = hudMesh(geo, color, ghost); m.position.set(x, y, z); m.rotation.set(...rot); root.add(m); return m; };
  const box = (w, h, d) => new T.BoxGeometry(w, h, d);
  const cyl = (r1, r2, h, s = 16) => new T.CylinderGeometry(r1, r2, h, s);
  const prop = (x, r) => {   // пропелер-штовхач: диск і лопать, крутиться навколо осі X
    const g = new T.Group(); g.position.set(x, 0, 0);
    g.add(hudMesh(new T.CylinderGeometry(r, r, 0.015, 28), color, ghost).rotateZ(Math.PI / 2));
    const blade = hudMesh(box(0.03, r * 2, 0.07), color, ghost); g.add(blade);
    root.add(g); rotors.push({ obj: blade, axis: "x" });
  };
  const nose = (x, r, len) => add(new T.ConeGeometry(r, len, 14), x, 0, 0, [0, 0, -Math.PI / 2]);

  if (shape === "delta") {   // Герань/Shahed: трикутне крило, шайби на кінцях, пропелер ззаду
    const s = new T.Shape(); s.moveTo(1.2, 0); s.lineTo(-0.9, 1.25); s.lineTo(-0.9, -1.25); s.closePath();
    const wing = new T.ExtrudeGeometry(s, { depth: 0.06, bevelEnabled: false }); wing.rotateX(-Math.PI / 2); wing.translate(0, -0.03, 0);
    add(wing, 0, 0, 0);
    add(cyl(0.13, 0.1, 2.3), 0.1, 0.08, 0, [0, 0, Math.PI / 2]);
    nose(1.5, 0.13, 0.5);
    add(box(0.5, 0.42, 0.04), -0.75, 0.2, 1.22); add(box(0.5, 0.42, 0.04), -0.75, 0.2, -1.22);
    prop(-1.1, 0.42);
  } else if (shape === "straight") {   // розвідувальний БпЛА: пряме крило, хвіст-«V», пропелер ззаду
    add(cyl(0.11, 0.09, 2.1), 0, 0, 0, [0, 0, Math.PI / 2]);
    nose(1.3, 0.11, 0.45);
    add(box(0.42, 0.05, 3.1), 0.25, 0.06, 0);
    add(box(1.1, 0.05, 0.05), -1.3, 0.05, 0.22); add(box(1.1, 0.05, 0.05), -1.3, 0.05, -0.22);   // дві балки
    add(box(0.3, 0.04, 0.95), -1.8, 0.28, 0.3, [0.6, 0, 0]); add(box(0.3, 0.04, 0.95), -1.8, 0.28, -0.3, [-0.6, 0, 0]);
    prop(-1.1, 0.34);
  } else if (shape === "loiter") {   // Ланцет/КУБ: невеликий корпус і дві пари X-крил
    add(cyl(0.12, 0.12, 1.8), 0, 0, 0, [0, 0, Math.PI / 2]);
    nose(1.1, 0.12, 0.45);
    for (const a of [Math.PI / 4, 3 * Math.PI / 4]) {
      add(box(0.36, 0.03, 1.7), 0.35, 0, 0, [a, 0, 0]);
      add(box(0.24, 0.03, 1.0), -0.8, 0, 0, [a, 0, 0]);
    }
    prop(-1.0, 0.26);
  } else {   // мультикоптер
    add(box(0.7, 0.2, 0.7), 0, 0, 0);
    add(new T.SphereGeometry(0.14, 12, 10), 0.42, -0.14, 0);   // камера на підвісі
    for (const [sx, sz] of [[1, 1], [1, -1], [-1, 1], [-1, -1]]) {
      add(box(1.15, 0.06, 0.09), sx * 0.4, 0, sz * 0.4, [0, Math.atan2(-sz, sx), 0]);
      add(cyl(0.38, 0.38, 0.02, 26), sx * 0.8, 0.07, sz * 0.8);
      const b = hudMesh(box(0.72, 0.015, 0.06), color, ghost); b.position.set(sx * 0.8, 0.1, sz * 0.8); root.add(b);
      rotors.push({ obj: b, axis: "y" });
    }
  }
  return { root, rotors };
}

export function ensureViewer(host) {
  if (!three) return null;
  if (!viewer) {
    try {
      const renderer = new three.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
      const scene = new three.Scene();
      const camera = new three.PerspectiveCamera(34, 1, 0.1, 50);
      camera.position.set(3.7, 2.0, 4.2); camera.lookAt(0, 0, 0);
      const grid = new three.GridHelper(5, 10, 0x1c3a4a, 0x14262f); grid.position.y = -0.75; scene.add(grid);   // «стіл», що не крутиться
      viewer = { renderer, scene, camera, model: null, key: null, yaw: 0.6, pitch: 0.0, drag: null, raf: 0 };
      const cv = renderer.domElement;
      cv.addEventListener("pointerdown", e => { viewer.drag = { x: e.clientX, y: e.clientY }; cv.setPointerCapture(e.pointerId); });
      cv.addEventListener("pointermove", e => {
        if (!viewer.drag) return;
        viewer.yaw += (e.clientX - viewer.drag.x) * 0.012; viewer.pitch = Math.max(-0.9, Math.min(0.9, viewer.pitch + (e.clientY - viewer.drag.y) * 0.008));
        viewer.drag = { x: e.clientX, y: e.clientY };
      });
      const end = () => { viewer.drag = null; };
      cv.addEventListener("pointerup", end); cv.addEventListener("pointercancel", end);
    } catch (err) { console.warn("3D-в'ювер вимкнено:", err); return null; }
  }
  if (viewer.renderer.domElement.parentNode !== host) host.prepend(viewer.renderer.domElement);
  const w = host.clientWidth || 264;
  viewer.renderer.setSize(w, VIEW_H, false);
  viewer.camera.aspect = w / VIEW_H; viewer.camera.updateProjectionMatrix();
  return viewer;
}

export function setDroneModel(v, shape, colorHex, ghost) {
  const key = shape + colorHex + ghost;
  if (v.key === key) return;
  if (v.model) { v.scene.remove(v.model.root); v.model.root.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); }); }
  v.model = buildDrone(shape, colorHex, ghost);
  v.scene.add(v.model.root);
  v.key = key;
}

export function startViewer() {
  if (!viewer || viewer.raf) return;
  const frame = () => {
    viewer.raf = requestAnimationFrame(frame);
    if (!viewer.model) return;
    if (!viewer.drag && !reducedMotion) viewer.yaw += 0.007;   // повільний оберт, поки не беруть мишею
    viewer.model.root.rotation.set(viewer.pitch, viewer.yaw, 0);
    for (const r of viewer.model.rotors) r.obj.rotation[r.axis] += reducedMotion ? 0 : 0.5;
    viewer.renderer.render(viewer.scene, viewer.camera);
  };
  frame();
}
export function stopViewer() { if (viewer && viewer.raf) { cancelAnimationFrame(viewer.raf); viewer.raf = 0; } }
