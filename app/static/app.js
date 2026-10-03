/* PWA (Service Worker, Installieren-Knopf) und mobiles Menü */
(function () {
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  }
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let deferred = null;
  const buttons = () => document.querySelectorAll("[data-install]");

  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferred = e;
    buttons().forEach((b) => (b.hidden = false));
    const hint = document.getElementById("st-hint");
    if (hint) hint.hidden = true;
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    buttons().forEach((b) => (b.hidden = true));
  });
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-install]");
    if (btn && deferred) {
      deferred.prompt();
      await deferred.userChoice;
      deferred = null;
      buttons().forEach((b) => (b.hidden = true));
    }
    const toggle = e.target.closest(".menu-toggle");
    if (toggle) {
      const nav = toggle.closest("nav");
      const open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    if (standalone) document.documentElement.classList.add("standalone");
    const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
    set("st-standalone", standalone ? "✔ Läuft als installierte App" : "ℹ Läuft im Browser");
    set("st-sw", "serviceWorker" in navigator
      ? (window.isSecureContext ? "✔ Offline-Hinweis aktiv (Service Worker)" : "✘ Service Worker nur über HTTPS")
      : "✘ Browser unterstützt keine Web-Apps");
    const hint = document.getElementById("st-hint");
    if (hint && window.isSecureContext && !standalone) setTimeout(() => { if (!deferred) hint.hidden = false; }, 1500);
  });
})();
