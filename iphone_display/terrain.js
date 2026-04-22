/**
 * terrain.js  –  Terrain data management and synthetic vision rendering
 *
 * Data source:  AWS Open Data Terrain Tiles (Terrarium RGB format)
 *   URL pattern: https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
 *   Elevation (m) = R*256 + G + B/256 - 32768
 *   CORS: Access-Control-Allow-Origin: * (publicly accessible)
 *
 * Storage:
 *   - Compressed PNG tiles cached in IndexedDB (~8-10 MB for global coarse zoom-5)
 *   - Decoded pixel data kept in a 64-tile LRU in-memory cache for fast lookup
 *   - Tiles decoded on demand (lazy); render loop uses whatever is ready
 *
 * Usage (from HTML):
 *   Terrain.init(onStatusChange)                 // call once on load
 *   Terrain.downloadCoarse(onProgress)           // ~8 MB, run on WiFi before flight
 *   Terrain.downloadRegion(lat,lon,100,onProg)   // detail tiles, ~2-4 MB
 *   Terrain.render(ctx, D, L)                    // call each animation frame
 */

const Terrain = (() => {
  'use strict';

  // ── Constants ─────────────────────────────────────────────────────────────
  const TILE_SIZE  = 256;
  const COARSE_Z   = 5;    // ~4.8 km/px at equator; 576 tiles cover lat -60°…+75°
  const DETAIL_Z   = 8;    // ~600 m/px at equator; fast regional download
  const DB_NAME    = 'ahrs-pfd-terrain-v1';
  const MEM_LIMIT  = 64;   // max decoded tiles in memory
  const NM_TO_M    = 1852;
  const DEG        = Math.PI / 180;

  function tileURL(z, x, y) {
    return `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/${z}/${x}/${y}.png`;
  }

  // ── State ──────────────────────────────────────────────────────────────────
  let _db          = null;
  let _status      = 'idle';   // 'idle'|'loading'|'ready'|'error'
  let _tileCount   = 0;
  let _notify      = null;

  const _decoded   = new Map();   // key → Uint8ClampedArray (256*256*4, RGBA)
  const _loading   = new Set();   // keys currently being decoded
  const _lruOrder  = [];          // insertion-order list for LRU eviction

  // ── Tile coordinate math ───────────────────────────────────────────────────
  function latLonToTile(lat, lon, z) {
    const n   = 1 << z;
    const x   = Math.floor((lon + 180) / 360 * n);
    const lr  = Math.log(Math.tan((90 + lat) * DEG / 2));
    const y   = Math.floor((1 - lr / Math.PI) / 2 * n);
    return { x: Math.max(0, Math.min(n - 1, x)),
             y: Math.max(0, Math.min(n - 1, y)) };
  }

  function pixelToElevation(r, g, b) {
    return (r * 256 + g + b / 256) - 32768;   // metres
  }

  // ── IndexedDB helpers ──────────────────────────────────────────────────────
  function openDB() {
    return new Promise((res, rej) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = e => e.target.result.createObjectStore('tiles');
      req.onsuccess = e  => { _db = e.target.result; res(); };
      req.onerror   = () => rej(req.error);
    });
  }

  function dbGet(key) {
    return new Promise((res, rej) => {
      const req = _db.transaction('tiles', 'readonly').objectStore('tiles').get(key);
      req.onsuccess = () => res(req.result || null);
      req.onerror   = () => rej(req.error);
    });
  }

  function dbPut(key, value) {
    return new Promise((res, rej) => {
      const req = _db.transaction('tiles', 'readwrite').objectStore('tiles').put(value, key);
      req.onsuccess = () => res();
      req.onerror   = () => rej(req.error);
    });
  }

  function dbCount() {
    return new Promise((res, rej) => {
      const req = _db.transaction('tiles', 'readonly').objectStore('tiles').count();
      req.onsuccess = () => res(req.result || 0);
      req.onerror   = () => rej(req.error);
    });
  }

  // ── PNG decode ─────────────────────────────────────────────────────────────
  function decodePNG(arrayBuffer) {
    return new Promise((res, rej) => {
      const blob = new Blob([arrayBuffer], { type: 'image/png' });
      const url  = URL.createObjectURL(blob);
      const img  = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        const c  = document.createElement('canvas');
        c.width  = c.height = TILE_SIZE;
        const cx = c.getContext('2d');
        cx.drawImage(img, 0, 0, TILE_SIZE, TILE_SIZE);
        res(new Uint8ClampedArray(cx.getImageData(0, 0, TILE_SIZE, TILE_SIZE).data));
      };
      img.onerror = () => { URL.revokeObjectURL(url); rej(new Error('decode failed')); };
      img.src = url;
    });
  }

  // ── LRU cache management ───────────────────────────────────────────────────
  function cacheStore(key, pixels) {
    if (_decoded.size >= MEM_LIMIT) {
      const oldest = _lruOrder.shift();
      _decoded.delete(oldest);
    }
    _decoded.set(key, pixels);
    _lruOrder.push(key);
  }

  // ── Lazy tile decode (fire-and-forget from render loop) ───────────────────
  function triggerDecode(z, x, y) {
    const key = `${z}/${x}/${y}`;
    if (_decoded.has(key) || _loading.has(key) || !_db) return;
    _loading.add(key);
    dbGet(key)
      .then(buf => buf ? decodePNG(buf) : null)
      .then(pixels => { if (pixels) cacheStore(key, pixels); })
      .catch(() => {})
      .finally(() => _loading.delete(key));
  }

  // ── Elevation lookup (synchronous, lazy-loads tile in background) ──────────
  function elevationAt(lat, lon) {
    for (const z of [DETAIL_Z, COARSE_Z]) {
      const { x, y } = latLonToTile(lat, lon, z);
      const key      = `${z}/${x}/${y}`;
      const pixels   = _decoded.get(key);

      if (!pixels) { triggerDecode(z, x, y); continue; }

      // Sub-tile pixel lookup
      const n    = 1 << z;
      const px   = ((lon + 180) / 360 * n - x);
      const lr   = Math.log(Math.tan((90 + lat) * DEG / 2));
      const py   = ((1 - lr / Math.PI) / 2 * n - y);
      const ix   = Math.max(0, Math.min(TILE_SIZE - 1, Math.floor(px * TILE_SIZE)));
      const iy   = Math.max(0, Math.min(TILE_SIZE - 1, Math.floor(py * TILE_SIZE)));
      const base = (iy * TILE_SIZE + ix) * 4;

      return pixelToElevation(pixels[base], pixels[base + 1], pixels[base + 2]);
    }
    return 0;   // unknown → assume sea level
  }

  // ── Tile download helpers ──────────────────────────────────────────────────
  async function fetchAndStoreTile(z, x, y) {
    const key = `${z}/${x}/${y}`;
    if (_decoded.has(key)) return;   // already in memory

    // If in DB but not decoded, just store the download and let lazy-decode handle it
    const existing = await dbGet(key);
    if (existing) return;

    const resp = await fetch(tileURL(z, x, y));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const buf = await resp.arrayBuffer();
    await dbPut(key, buf);
    _tileCount++;
  }

  function yieldToUI() { return new Promise(r => setTimeout(r, 0)); }

  // ── Public: download global coarse coverage ────────────────────────────────
  async function downloadCoarse(onProgress) {
    const z = COARSE_Z;
    const n = 1 << z;   // 32

    // Latitude band -60° … +75° (where virtually all aircraft operate)
    const yMin = latLonToTile(75,  0, z).y;
    const yMax = latLonToTile(-60, 0, z).y;

    const tiles = [];
    for (let tx = 0; tx < n; tx++)
      for (let ty = yMin; ty <= yMax; ty++)
        tiles.push([z, tx, ty]);

    let done = 0;
    for (const [z, x, y] of tiles) {
      try { await fetchAndStoreTile(z, x, y); }
      catch (e) { console.warn(`coarse tile ${z}/${x}/${y}:`, e.message); }
      done++;
      if (onProgress) onProgress(done / tiles.length, done, tiles.length);
      if (done % 12 === 0) await yieldToUI();
    }

    _status = 'ready';
    if (_notify) _notify(_status, _tileCount);
  }

  // ── Public: download regional detail tiles ─────────────────────────────────
  async function downloadRegion(lat, lon, radiusNm, onProgress) {
    const z    = DETAIL_Z;
    const n    = 1 << z;
    const dlat = radiusNm / 60;
    const dlon = radiusNm / (60 * Math.cos(lat * DEG));

    const t1 = latLonToTile(lat + dlat, lon - dlon, z);
    const t2 = latLonToTile(lat - dlat, lon + dlon, z);

    const tiles = [];
    for (let tx = t1.x; tx <= t2.x; tx++)
      for (let ty = t1.y; ty <= t2.y; ty++)
        if (tx >= 0 && tx < n && ty >= 0 && ty < n)
          tiles.push([z, tx, ty]);

    let done = 0;
    for (const [z, x, y] of tiles) {
      try { await fetchAndStoreTile(z, x, y); }
      catch (e) { console.warn(`detail tile ${z}/${x}/${y}:`, e.message); }
      done++;
      if (onProgress) onProgress(done / tiles.length, done, tiles.length);
      if (done % 4 === 0) await yieldToUI();
    }

    if (_status !== 'ready') _status = 'partial';
    if (_notify) _notify(_status, _tileCount);
  }

  // ── Public: init ───────────────────────────────────────────────────────────
  async function init(onStatusChange) {
    _notify = onStatusChange;
    _status = 'loading';
    if (_notify) _notify(_status, 0);
    try {
      await openDB();
      _tileCount = await dbCount();
      _status    = _tileCount > 0 ? 'ready' : 'idle';
    } catch (e) {
      console.error('Terrain init:', e);
      _status = 'error';
    }
    if (_notify) _notify(_status, _tileCount);
  }

  // ── Terrain rendering ──────────────────────────────────────────────────────
  // Projects a fan of terrain points ahead of the aircraft using perspective
  // projection consistent with the PFD's focal length.
  // Must be called AFTER drawAI (fills behind sky/ground) and BEFORE instrument
  // overlays (so tapes and heading tape remain on top).

  function terrainColor(elevM, altDiffM) {
    // EGPWS-style proximity coloring takes priority
    if (altDiffM > -30)   return 'rgba(200,20,10,0.88)';    // red   – at/above aircraft
    if (altDiffM > -152)  return 'rgba(185,95,10,0.82)';    // amber – within 500 ft
    if (altDiffM > -305)  return 'rgba(160,150,10,0.75)';   // yellow – within 1000 ft
    // Tint colour by absolute elevation
    if (elevM < 0)    return '#1a3050';   // water
    if (elevM < 300)  return '#2a4418';   // lowland
    if (elevM < 900)  return '#4a3c18';   // hills
    if (elevM < 2000) return '#6a5030';   // mountains
    return '#7a6858';                     // high peaks / snow line
  }

  function projectENU(north, east, up, yawR, pitchR, rollR) {
    // Rotate ENU → camera frame: yaw → pitch → roll
    const cy = Math.cos(yawR),   sy = Math.sin(yawR);
    const cp = Math.cos(pitchR), sp = Math.sin(pitchR);
    const cr = Math.cos(rollR),  sr = Math.sin(rollR);

    const fwd0 =  north * cy + east * sy;
    const rgt0 = -north * sy + east * cy;
    const up0  =  up;

    const fwd1 = fwd0 * cp + up0 * sp;
    const up1  = up0  * cp - fwd0 * sp;

    if (fwd1 < 10) return null;   // behind or too close

    const rgt2 = rgt0 * cr + up1 * sr;
    const up2  = up1  * cr - rgt0 * sr;

    return { fwd: fwd1, rgt: rgt2, up: up2 };
  }

  function render(ctx, D, L) {
    // Only render if we have any tile data loaded in memory
    if (_decoded.size === 0) return;

    const { spdX, spdW, altX, tapeTopY, tapeH, tapeMidY, cx, focal } = L;
    const clipX = spdX + spdW;
    const clipW = altX - clipX;
    if (clipW <= 0 || !focal) return;

    const yawR   =  D.yaw   * DEG;
    const pitchR =  D.pitch * DEG;
    const rollR  =  D.roll  * DEG;
    const altM   =  D.alt   * 0.3048;   // feet → metres

    // Sampling grid: 21 bearings (±40°) × 9 distance rings
    const BRG_COUNT  = 21;
    const DIST_COUNT = 9;
    const BRG_HALF   = 42;   // degrees either side
    const DISTANCES  = [0.5, 1, 2, 4, 8, 15, 25, 40, 60];   // nautical miles

    ctx.save();
    // Clip to the full screen width so the TAWS-coloured horizon band
    // reads edge-to-edge. Tapes are semi-transparent (alpha ~0.8) and
    // are drawn after terrain, so they tint the band without hiding it.
    ctx.beginPath();
    ctx.rect(0, tapeTopY, ctx.canvas.width, tapeH);
    ctx.clip();

    // Build projected grid [distIdx][brgIdx]
    const grid = [];
    for (let di = 0; di < DIST_COUNT; di++) {
      const distM = DISTANCES[di] * NM_TO_M;
      const row   = [];
      for (let bi = 0; bi < BRG_COUNT; bi++) {
        const bearOff = -BRG_HALF + bi * (BRG_HALF * 2 / (BRG_COUNT - 1));
        const bearRad = yawR + bearOff * DEG;
        const north   = distM * Math.cos(bearRad);
        const east    = distM * Math.sin(bearRad);

        // Approximate lat/lon offset (valid to ~100 nm)
        const ptLat = D.lat + north / 111320;
        const ptLon = D.lon + east  / (111320 * Math.cos(D.lat * DEG));
        const elevM = elevationAt(ptLat, ptLon);
        const upM   = elevM - altM;

        const pt = projectENU(north, east, upM, yawR, pitchR, -rollR);
        row.push(pt ? {
          sx:       cx + (pt.rgt / pt.fwd) * focal,
          sy:       tapeMidY - (pt.up / pt.fwd) * focal,
          elevM,
          altDiffM: upM,
        } : null);
      }
      grid.push(row);
    }

    // Painter's algorithm: draw far quads first (no depth sort needed – grid is ordered)
    ctx.lineWidth = 0.4;
    for (let di = DIST_COUNT - 2; di >= 0; di--) {
      for (let bi = 0; bi < BRG_COUNT - 1; bi++) {
        const p00 = grid[di    ][bi    ];
        const p01 = grid[di    ][bi + 1];
        const p10 = grid[di + 1][bi    ];
        const p11 = grid[di + 1][bi + 1];
        if (!p00 || !p01 || !p10 || !p11) continue;

        const avgElev    = (p00.elevM    + p01.elevM    + p10.elevM    + p11.elevM)    / 4;
        const avgAltDiff = (p00.altDiffM + p01.altDiffM + p10.altDiffM + p11.altDiffM) / 4;

        ctx.fillStyle   = terrainColor(avgElev, avgAltDiff);
        ctx.strokeStyle = 'rgba(0,0,0,0.12)';

        ctx.beginPath();
        ctx.moveTo(p00.sx, p00.sy);
        ctx.lineTo(p01.sx, p01.sy);
        ctx.lineTo(p11.sx, p11.sy);
        ctx.lineTo(p10.sx, p10.sy);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
      }
    }

    ctx.restore();
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  return {
    init,
    downloadCoarse,
    downloadRegion,
    elevationAt,
    render,
    get status()    { return _status;    },
    get tileCount() { return _tileCount; },
  };
})();
