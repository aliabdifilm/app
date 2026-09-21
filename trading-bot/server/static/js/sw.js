/* ApexAlgo service worker.
 *
 * Caches only the shell (CSS, JS, icon) so the panel opens instantly and can
 * show its frame offline. Live data is NEVER cached: a stale equity figure on
 * a trading panel is worse than no figure at all, so every /api/ request goes
 * straight to the network and fails visibly when there is none.
 */
var CACHE = "apex-shell-v1";
var SHELL = [
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/icons/icon.svg",
  "/manifest.webmanifest"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return cache.addAll(SHELL);
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
                             .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);

  if (event.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;
  // Never serve trading data, or an authenticated page, from cache.
  if (url.pathname.startsWith("/api/") ||
      url.pathname === "/" ||
      url.pathname.startsWith("/login") ||
      url.pathname.startsWith("/logout")) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then(function (cached) {
      return cached || fetch(event.request).then(function (response) {
        if (response.ok) {
          var copy = response.clone();
          caches.open(CACHE).then(function (cache) { cache.put(event.request, copy); });
        }
        return response;
      });
    })
  );
});
