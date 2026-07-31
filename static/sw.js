/* Learn PWA service worker — P4.15 (iss_a0cef670).
   Strategy honors this project's core invariant — edit-file → refresh, NO build:
   the app shell + API are NETWORK-FIRST (always fresh online, so an index.html edit
   shows on refresh) and fall back to cache only when OFFLINE. Cache-FIRST is reserved
   for immutable content-addressed assets (hashed .m4a voice files, icons) where the
   name changes if the bytes do, so a stale hit is impossible. POST always network
   (answering, login, upload need a connection). Read-only offline by design.
   Served from "/" (root scope) via the app's /sw.js route + Service-Worker-Allowed: /. */
const SHELL = "learn-shell-v3";
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
  const p = url.pathname;

  // Immutable content-addressed assets → cache-first (hashed .m4a voice files, icons):
  // the filename changes if the bytes do, so a cache hit is always correct + saves the fetch.
  const immutable = (p.startsWith("/static/audio/") && p.endsWith(".m4a")) || p.startsWith("/static/icons/");
  if (immutable) {
    e.respondWith(
      caches.match(req).then((hit) =>
        hit ||
        fetch(req).then((res) => {
          if (res && res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(req, copy)); }
          return res;
        })
      )
    );
    return;
  }

  // Everything else — the "/" navigation, index.html, manifest, /static/*, /api/* —
  // NETWORK-FIRST so edits are always seen on refresh; fall back to cache only offline.
  const store = p.startsWith("/api/") ? DATA : SHELL;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) { const copy = res.clone(); caches.open(store).then((c) => c.put(req, copy)); }
        return res;
      })
      .catch(() =>
        caches.match(req).then((hit) =>
          hit ||
          (req.mode === "navigate"
            ? caches.match("/static/index.html")
            : new Response(
                JSON.stringify({ offline: true, error: "You're offline — showing what was last loaded." }),
                { status: 200, headers: { "Content-Type": "application/json" } }
              ))
        )
      )
  );
});
