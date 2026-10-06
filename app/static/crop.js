/* Zuschnitt-Editor für das Gebäudefoto: Bild wählen, verschieben (Maus/Finger), zoomen (Regler/Mausrad/Pinch),
   runde Vorschau; Ergebnis 512×512 JPEG als Data-URL im versteckten Feld „photo_data“. */
(function () {
  const root = document.getElementById("cropper");
  if (!root) return;
  const file = root.querySelector("input[type=file]");
  const canvas = root.querySelector("canvas");
  const zoom = root.querySelector("input[type=range]");
  const out = root.querySelector("input[name=photo_data]");
  const preview = document.getElementById("crop-preview");
  const ctx = canvas.getContext("2d");
  const SIZE = canvas.width;  // Ansicht (quadratisch)
  let img = null, scale = 1, minScale = 1, x = 0, y = 0;
  const pointers = new Map();
  let pinchStart = null;

  function clamp() {
    const w = img.width * scale, h = img.height * scale;
    x = Math.min(0, Math.max(SIZE - w, x));
    y = Math.min(0, Math.max(SIZE - h, y));
  }
  function draw() {
    ctx.clearRect(0, 0, SIZE, SIZE);
    if (!img) return;
    clamp();
    ctx.drawImage(img, x, y, img.width * scale, img.height * scale);
    // Abdunkeln außerhalb des Kreises
    ctx.save();
    ctx.fillStyle = "rgba(0,0,0,.45)";
    ctx.beginPath();
    ctx.rect(0, 0, SIZE, SIZE);
    ctx.arc(SIZE / 2, SIZE / 2, SIZE / 2 - 2, 0, Math.PI * 2, true);
    ctx.fill("evenodd");
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(SIZE / 2, SIZE / 2, SIZE / 2 - 2, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
    exportImage();
  }
  function exportImage() {
    const c = document.createElement("canvas");
    c.width = c.height = 512;
    const k = 512 / SIZE;
    c.getContext("2d").drawImage(img, x * k, y * k, img.width * scale * k, img.height * scale * k);
    const data = c.toDataURL("image/jpeg", 0.88);
    out.value = data;
    if (preview) { preview.src = data; preview.hidden = false; }
  }
  function setScale(newScale, cx = SIZE / 2, cy = SIZE / 2) {
    newScale = Math.max(minScale, Math.min(minScale * 6, newScale));
    x = cx - (cx - x) * (newScale / scale);
    y = cy - (cy - y) * (newScale / scale);
    scale = newScale;
    zoom.value = String(scale / minScale);
    draw();
  }
  file.addEventListener("change", () => {
    const f = file.files && file.files[0];
    if (!f) return;
    const url = URL.createObjectURL(f);
    const im = new Image();
    im.onload = () => {
      img = im;
      minScale = Math.max(SIZE / im.width, SIZE / im.height);
      scale = minScale;
      x = (SIZE - im.width * scale) / 2;
      y = (SIZE - im.height * scale) / 2;
      zoom.value = "1";
      root.classList.add("has-image");
      draw();
    };
    im.src = url;
  });
  zoom.addEventListener("input", () => img && setScale(minScale * parseFloat(zoom.value)));
  canvas.addEventListener("wheel", (e) => {
    if (!img) return;
    e.preventDefault();
    const r = canvas.getBoundingClientRect(), k = SIZE / r.width;
    setScale(scale * (e.deltaY < 0 ? 1.08 : 1 / 1.08), (e.clientX - r.left) * k, (e.clientY - r.top) * k);
  }, { passive: false });
  canvas.addEventListener("pointerdown", (e) => {
    if (!img) return;
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinchStart = { d: Math.hypot(a.x - b.x, a.y - b.y), scale };
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!img || !pointers.has(e.pointerId)) return;
    const prev = pointers.get(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const r = canvas.getBoundingClientRect(), k = SIZE / r.width;
    if (pointers.size === 2 && pinchStart) {
      const [a, b] = [...pointers.values()];
      setScale(pinchStart.scale * Math.hypot(a.x - b.x, a.y - b.y) / pinchStart.d);
    } else {
      x += (e.clientX - prev.x) * k;
      y += (e.clientY - prev.y) * k;
      draw();
    }
  });
  const end = (e) => { pointers.delete(e.pointerId); if (pointers.size < 2) pinchStart = null; };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
})();
