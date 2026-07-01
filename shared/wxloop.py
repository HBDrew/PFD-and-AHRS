"""
wxloop.py — radar loop buffer for MFD playback (NEXRAD / FIS-B).

Keeps a short ring buffer of radar frames plus the playback state (on/off,
current index, auto-play).  RAM-only.  Frames are opaque payloads the caller
snapshots and renders; this module only manages the timeline + controls, so it
serves both the internet-WMS raster and the FIS-B datalink cells.

Multiple named sources snapshot TOGETHER on a fixed cadence, so frame index i
is the same capture time in every source's buffer — playback scrubs both in
lock-step.  A frame's payload may be None (that source had nothing then); the
slot is still kept so the timeline stays aligned.

Used only on the full-screen MFD — the small PFD inset always shows live.
"""


class RadarLoop:
    def __init__(self, interval_s=300.0, max_frames=12, advance_s=0.8):
        self.interval_s = float(interval_s)     # snapshot cadence (5 min)
        self.max_frames = int(max_frames)       # ring length (12 → 60 min)
        self.advance_s  = float(advance_s)      # auto-play frame dwell
        self._names = []
        self._buf   = {}                        # name -> [(wall_s, payload), ...]
        self._last_snap_mono = None
        # playback state
        self.on      = False
        self.playing = True
        self.idx     = 0
        self._last_adv_mono = 0.0

    def register(self, *names):
        for n in names:
            if n not in self._names:
                self._names.append(n)
                self._buf[n] = []

    def maybe_snapshot(self, now_mono, wall_s, payloads):
        """Append one frame per source when the interval has elapsed AND at
        least one source has data.  ``payloads`` maps name -> payload_or_None.
        Returns True if a snapshot was taken."""
        if (self._last_snap_mono is not None
                and now_mono - self._last_snap_mono < self.interval_s):
            return False
        if not any(payloads.get(n) is not None for n in self._names):
            return False
        self._last_snap_mono = now_mono
        for n in self._names:
            b = self._buf[n]
            b.append((wall_s, payloads.get(n)))
            if len(b) > self.max_frames:
                del b[:len(b) - self.max_frames]
        return True

    def count(self):
        return max((len(b) for b in self._buf.values()), default=0)

    def frame(self, name, idx):
        """(wall_s, payload) for source ``name`` at ``idx`` (clamped), or None."""
        b = self._buf.get(name) or []
        if not b:
            return None
        idx = max(0, min(idx, len(b) - 1))
        return b[idx]

    def wall_at(self, idx):
        """Capture wall-clock (epoch s) of frame ``idx``, or None when empty."""
        for n in self._names:
            b = self._buf.get(n) or []
            if b:
                idx = max(0, min(idx, len(b) - 1))
                return b[idx][0]
        return None

    def clear(self):
        for n in self._names:
            self._buf[n] = []
        self.on = False
        self.idx = 0

    # ── playback controls ──────────────────────────────────────────────────
    def enter(self):
        if self.count() == 0:
            return
        self.on = True
        self.playing = True
        self.idx = 0

    def exit(self):
        self.on = False

    def toggle(self):
        self.exit() if self.on else self.enter()

    def toggle_play(self):
        self.playing = not self.playing

    def scrub_to(self, idx):
        n = self.count()
        if n:
            self.idx = max(0, min(idx, n - 1))
        self.playing = False

    def tick(self, now_mono):
        """Advance the auto-play cursor.  Call once per frame."""
        n = self.count()
        if not self.on or n == 0:
            self.idx = min(self.idx, max(0, n - 1))
            return
        if self.playing and now_mono - self._last_adv_mono >= self.advance_s:
            self._last_adv_mono = now_mono
            self.idx = (self.idx + 1) % n
