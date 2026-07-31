/* SELF-DESTRUCT service worker (2026-07-30).
   The Learn PWA service worker was REMOVED (operator: "rip it out entirely") after
   recurring iOS Safari cache-staleness. This stub exists only to evict itself from
   any device that still has an old worker installed: on its next update check the
   browser fetches THIS file, which deletes every cache, unregisters the worker, and
   reloads open pages so they return under NO service-worker control — plain,
   always-fresh network loads. There is intentionally no fetch handler. */
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    } catch (_) {}
    try { await self.registration.unregister(); } catch (_) {}
    try {
      const clients = await self.clients.matchAll({ type: "window" });
      clients.forEach((c) => { try { c.navigate(c.url); } catch (_) {} });
    } catch (_) {}
  })());
});
