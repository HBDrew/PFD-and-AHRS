/*
 * sw.js – Service Worker for the iPhone PFD.
 *
 * Caches the three static files (index.html, preview.html, terrain.js) plus
 * the PWA manifest so the PFD launches fully offline once it's been opened
 * over a network at least once.  Terrain tiles are fetched on demand and
 * cached as they come back, so a region you've flown over before still
 * renders terrain bands in airplane mode.
 *
 * Cache strategy:
 *   - Static app shell:   cache-first, revalidate in background
 *   - /events (SSE):      never cached, always network
 *   - /baro, /trim:       never cached, always network
 *   - Terrain tiles:      stale-while-revalidate with an LRU cap (~200 tiles)
 *
 * Bump CACHE_VERSION any time one of the three files changes so the phone
 * picks up the update on next launch.  The old cache is then evicted in
 * the activate handler below.
 */
'use strict';

const CACHE_VERSION = 'pfd-v99';
const CACHE_STATIC  = `${CACHE_VERSION}-static`;
const CACHE_TILES   = `${CACHE_VERSION}-tiles`;
const MAX_TILES     = 200;

const STATIC_ASSETS = [
  './',
  './index.html',
  './preview.html',
  './terrain.js',
  './manifest.webmanifest',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_STATIC).then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_STATIC && k !== CACHE_TILES)
          .map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

function isTileRequest(url) {
  return url.host === 's3.amazonaws.com'
      && url.pathname.startsWith('/elevation-tiles-prod/terrarium/');
}

async function trimCache(name, max) {
  const cache = await caches.open(name);
  const keys  = await cache.keys();
  if (keys.length <= max) return;
  // LRU-ish: drop the oldest entries until under the cap.  Tiles downloaded
  // longest ago go first — matches the use-case where the pilot downloads
  // a region for this flight and the cache naturally turns over by route.
  for (let i = 0; i < keys.length - max; i++) {
    await cache.delete(keys[i]);
  }
}

self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Don't interfere with SSE / baro / trim — those are live Pico endpoints.
  if (url.pathname === '/events'
      || url.pathname.startsWith('/baro')
      || url.pathname.startsWith('/trim')) {
    return;
  }

  // Terrain tiles: stale-while-revalidate.
  if (isTileRequest(url)) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_TILES);
      const cached = await cache.match(req);
      const fetchPromise = fetch(req).then(resp => {
        if (resp && resp.ok) {
          cache.put(req, resp.clone());
          trimCache(CACHE_TILES, MAX_TILES);
        }
        return resp;
      }).catch(() => cached);
      return cached || fetchPromise;
    })());
    return;
  }

  // App shell: cache-first, then network.
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_STATIC);
    const cached = await cache.match(req);
    if (cached) {
      // Kick a background refresh so the next launch picks up any updates.
      fetch(req).then(resp => {
        if (resp && resp.ok) cache.put(req, resp.clone());
      }).catch(() => {});
      return cached;
    }
    try {
      const resp = await fetch(req);
      if (resp && resp.ok && req.method === 'GET'
          && (req.url.endsWith('.html') || req.url.endsWith('.js')
              || req.url.endsWith('.webmanifest'))) {
        cache.put(req, resp.clone());
      }
      return resp;
    } catch (e) {
      return cached || new Response('Offline', {status: 503});
    }
  })());
});
