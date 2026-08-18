// Minimal network-first service worker.
//
// Its job is installability, not offline mode: the app serves live market
// data, so caching pages would just be another way to show stale numbers --
// the exact failure this project spent a day removing. Only the static icons
// are cached; everything else passes straight through to the network.
const STATIC_CACHE = 'losers-static-v1';
const STATIC_ASSETS = ['/static/icon-192.png', '/static/icon-512.png', '/static/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(STATIC_CACHE).then((c) => c.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== STATIC_CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then((hit) => hit || fetch(event.request))
    );
  }
  // Everything else: straight to the network. No stale pages, ever.
});
