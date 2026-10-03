/* Service Worker der Nebenkostenabrechnung.
   Bewusst sparsam: zwischengespeichert werden nur statische Dateien und die Offline-Seite –
   niemals Abrechnungen oder andere persönliche Daten. */
const VERSION = "__VERSION__";
const CACHE = "nk-static-" + VERSION;
const PRECACHE = ["/offline", "/static/style.css", "/static/picker.js", "/static/favicon.svg",
                  "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("nk-static-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === "navigate") {  // Seiten: immer frisch vom Server, offline → Hinweisseite
    event.respondWith(fetch(req).catch(() => caches.match("/offline")));
    return;
  }
  if (url.pathname.startsWith("/static/")) {  // statische Dateien: Cache, im Hintergrund aktualisieren
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(req).then((hit) => {
          const net = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => hit);
          return hit || net;
        })
      )
    );
  }
});
