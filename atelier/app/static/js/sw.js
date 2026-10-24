/*
 * Atelier Sayyar - service worker.
 *
 * Scope of this worker is deliberately tiny: it makes the *static assets*
 * (CSS, JS, fonts, icons) load instantly and keep working on a flaky mobile
 * connection.  It NEVER touches HTML pages, API/JSON calls or any non-GET
 * request - those always go straight to the network, so users can never see a
 * stale page, stale accounting data or a replayed form post.
 *
 * Strategy
 *   fonts, images   cache-first      (immutable in practice)
 *   css, js, json   network-first    (fresh when online, cached copy offline)
 *
 * Bump CACHE_VERSION whenever the precache list changes shape; old caches are
 * deleted on activate.
 *
 * To control the whole site this file must be served with the response header
 *   Service-Worker-Allowed: /
 * (or be served from "/sw.js").  See docs/UI_GUIDE.md.
 */
'use strict';

var CACHE_VERSION = 'v1';
var CACHE_PREFIX = 'atelier-static-';
var CACHE_NAME = CACHE_PREFIX + CACHE_VERSION;

// Same-origin path prefix of the Flask static folder.
var STATIC_PREFIX = '/static/';

var PRECACHE = [
  'css/app.css',
  'css/print.css',
  'js/theme-init.js',
  'js/money.js',
  'js/jalali.js',
  'js/core.js',
  'fonts/Vazirmatn-Variable.woff2',
  'img/favicon.svg',
  'img/icon-192.png',
  'manifest.webmanifest'
].map(function (p) { return STATIC_PREFIX + p; });

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      // One failing file (e.g. renamed asset) must not block the install.
      return Promise.all(PRECACHE.map(function (url) {
        return cache.add(new Request(url, { cache: 'reload' })).catch(function () { /* ignore */ });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (name) {
        if (name.indexOf(CACHE_PREFIX) === 0 && name !== CACHE_NAME) return caches.delete(name);
        return null;
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function isStaticAsset(url) {
  return url.origin === self.location.origin && url.pathname.indexOf(STATIC_PREFIX) === 0;
}

function cachePut(request, response) {
  if (!response || response.status !== 200 || response.type === 'opaque') return response;
  var copy = response.clone();
  caches.open(CACHE_NAME).then(function (cache) { cache.put(request, copy); });
  return response;
}

self.addEventListener('fetch', function (event) {
  var request = event.request;
  // Never intercept: non-GET (forms, API writes), navigations (HTML), or anything outside /static/.
  if (request.method !== 'GET' || request.mode === 'navigate') return;
  var url = new URL(request.url);
  if (!isStaticAsset(url)) return;
  if (request.headers.get('range')) return; // partial content: let the browser handle it

  var path = url.pathname;
  var cacheFirst = /\.(woff2?|ttf|otf|png|jpe?g|gif|svg|ico|webp|avif)$/i.test(path);

  if (cacheFirst) {
    event.respondWith(
      caches.match(request, { ignoreSearch: true }).then(function (hit) {
        return hit || fetch(request).then(function (res) { return cachePut(request, res); });
      })
    );
    return;
  }

  // network-first for code that changes between deploys
  event.respondWith(
    fetch(request).then(function (res) { return cachePut(request, res); }).catch(function () {
      return caches.match(request, { ignoreSearch: true }).then(function (hit) {
        return hit || Response.error();
      });
    })
  );
});
