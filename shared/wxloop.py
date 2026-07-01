"""
wxloop.py — radar loop model for MFD playback (NEXRAD / FIS-B).

Hybrid, per source:
  • FIS-B is push-only (no query API), so its frames are buffered continuously
    in RAM (one snapshot per `interval_s`).
  • Internet NEXRAD (IEM WMS) supports a TIME dimension (WMS-T), so its frames
    are pulled ON DEMAND when playback starts — no need to pre-record.

On playback entry a canonical timeline is built:
  • internet available → the last N wall-clock `interval_s` steps; each slot's
    WMS image is fetched async by the caller (set_wms) and its FIS-B layer is
    matched from the RAM buffer by nearest time.
  • FIS-B only → the RAM buffer's own frames.

This module is pure data + playback state (no network, no pygame).  MFD-only —
the PFD inset always shows live.
"""


class RadarLoop:
    def __init__(self, interval_s=300.0, span_min=60, advance_s=0.8):
        self.interval_s   = float(interval_s)
        self.count_target = int(span_min * 60 // interval_s) + 1   # incl. "now"
        self.advance_s    = float(advance_s)
        self._fisb = []            # background buffer: [(wall_s, cells)]
        self._fisb_last_mono = None
        self._fisb_max = self.count_target + 2
        # playback state
        self.on      = False
        self.playing = True
        self.idx     = 0
        self._last_adv_mono = 0.0
        self.frames  = []          # built on entry: [{t, wms, fisb}]

    # ── FIS-B background buffering ──────────────────────────────────────────
    def buffer_fisb(self, now_mono, wall_s, cells):
        """Snapshot the current FIS-B mosaic every interval_s (RAM ring)."""
        if not cells:
            return
        if (self._fisb_last_mono is not None
                and now_mono - self._fisb_last_mono < self.interval_s):
            return
        self._fisb_last_mono = now_mono
        self._fisb.append((wall_s, cells))
        if len(self._fisb) > self._fisb_max:
            del self._fisb[:len(self._fisb) - self._fisb_max]

    def has_fisb(self):
        return bool(self._fisb)

    def _nearest_fisb(self, t):
        best, best_dt = None, self.interval_s * 0.75
        for w, c in self._fisb:
            dt = abs(w - t)
            if dt <= best_dt:
                best_dt, best = dt, c
        return best

    # ── playback build / controls ──────────────────────────────────────────
    def build(self, now_wall, wms_ok):
        """Build the playback timeline.  ``wms_ok`` → internet radar is usable,
        so use aligned wall-clock steps (WMS-T fetch fills them); otherwise use
        the FIS-B buffer's own frames.  Returns the aligned epoch timestamps
        (empty for the FIS-B-only case) so the caller can fetch WMS-T."""
        if wms_ok:
            step = self.interval_s
            base = (int(now_wall) // int(step)) * int(step)   # floor to step
            ts = [base - (self.count_target - 1 - i) * step
                  for i in range(self.count_target)]          # oldest → newest
            self.frames = [{"t": t, "wms": None, "fisb": self._nearest_fisb(t)}
                           for t in ts]
            out = list(ts)
        else:
            self.frames = [{"t": w, "wms": None, "fisb": c} for w, c in self._fisb]
            out = []
        self.on      = bool(self.frames)
        self.playing = True
        self.idx     = 0
        return out

    def set_wms(self, i, payload):
        if 0 <= i < len(self.frames):
            self.frames[i]["wms"] = payload

    def count(self):
        return len(self.frames)

    def frame_wms(self, i):
        return self.frames[i]["wms"] if 0 <= i < len(self.frames) else None

    def frame_fisb(self, i):
        return self.frames[i]["fisb"] if 0 <= i < len(self.frames) else None

    def wall_at(self, i):
        return self.frames[i]["t"] if 0 <= i < len(self.frames) else None

    def exit(self):
        self.on = False
        self.frames = []

    def toggle_play(self):
        self.playing = not self.playing

    def scrub_to(self, i):
        if self.frames:
            self.idx = max(0, min(i, len(self.frames) - 1))
        self.playing = False

    def tick(self, now_mono):
        n = len(self.frames)
        if not self.on or n == 0:
            return
        if self.playing and now_mono - self._last_adv_mono >= self.advance_s:
            self._last_adv_mono = now_mono
            self.idx = (self.idx + 1) % n
