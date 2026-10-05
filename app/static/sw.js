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

/* ---- Push-Benachrichtigungen ---- */
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  const title = data.title || "Nebenkostenabrechnung";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    icon: "/static/icon-192.png",
    badge: "/static/badge-96.png",
    tag: data.tag || undefined,
    renotify: !!data.tag,
    data: { url: data.url || "/" },
    lang: "de",
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.startsWith(self.location.origin) && "focus" in w) {
        await w.focus();
        if ("navigate" in w) return w.navigate(url);
        return;
      }
    }
    return self.clients.openWindow(url);
  })());
});

self.addEventListener("pushsubscriptionchange", (event) => {
  // Browser hat das Abo erneuert → beim Server neu anmelden
  event.waitUntil((async () => {
    const cfg = await fetch("/api/push/config", { credentials: "include" }).then((r) => r.json());
    const sub = await self.registration.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: cfg.publicKey });
    await fetch("/api/push/subscribe", { method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub) });
  })());
});
