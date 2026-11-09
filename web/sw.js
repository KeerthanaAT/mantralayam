// Service worker of the guest site: an offline shell. Static files (the page, styles, scripts, translations, images) are kept and refreshed in the background;
// the API is never cached, so prices, availability and bookings always come live and nothing personal is stored by the worker.
const CACHE = "mr-shell-v1";
const SHELL = ["/", "/shared.css", "/tokens.css", "/i18n.js", "/locales/en.json", "/images/icon-192.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

const isStatic = (u) => u.origin === location.origin && (u.pathname === "/" || /\.(css|js|webmanifest|png|jpg|jpeg|webp|svg|ico)$/.test(u.pathname) || u.pathname.startsWith("/locales/"));

self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/") || url.pathname === "/health") return;  // the network, always
  if (req.mode === "navigate") {  // a page load: the network first so a new version shows at once, the kept shell when offline
    e.respondWith(fetch(req).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put("/", copy)); return r; }).catch(() => caches.match("/")));
    return;
  }
  if (isStatic(url)) {  // stale while revalidate
    e.respondWith(caches.open(CACHE).then(async (c) => {
      const hit = await c.match(req);
      const net = fetch(req).then((r) => { if (r.ok) c.put(req, r.clone()); return r; }).catch(() => hit);
      return hit || net;
    }));
  }
});
