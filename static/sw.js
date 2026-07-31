/* Learn PWA service worker — P4.15 (iss_a0cef670).
   App shell = cache-first (opens offline); GET /api/* = network-first → last-seen cache
   (so concepts/cards you've SEEN — including mastered ones — read offline); POST always
   network (answering, login, upload require a connection). Read-only offline by design.
   Served from "/" (root scope) via the app's /sw.js route + Service-Worker-Allowed: /. */
const SHELL = "learn-shell-v2";
const DATA  = "learn-data-v1";
const SHELL_ASSETS = [
  "/", "/static/index.html", "/static/manifest.webmanifest",
  "/static/icons/icon-192.png", "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL)
      .then((c) => c.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())   // a missing asset must never wedge activation
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;                         // POST/PUT/etc never cached
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;          // leave cross-origin (font CDN) alone

  // App shell: navigations + /static/* → cache-first, fall back to network (and cache it),
  // ultimately fall back to the cached shell so the SPA always boots offline.
  if (req.mode === "navigate" || url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.match(req).then((hit) =>
        hit ||
        fetch(req)
          .then((res) => {
            const copy = res.clone();
            caches.open(SHELL).then((c) => c.put(req, copy));
            return res;
          })
          .catch(() => caches.match("/static/index.html"))
      )
    );
    return;
  }

  // GET /api/* : network-first (fresh online), fall back to last-seen cache (offline read).
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(DATA).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() =>
          caches.match(req).then((hit) =>
            hit ||
            new Response(
              JSON.stringify({ offline: true, error: "You're offline — showing what was last loaded." }),
              { status: 200, headers: { "Content-Type": "application/json" } }
            )
          )
        )
    );
  }
});
