"""
perf.py — lightweight frame-timing sampler for field diagnostics.

Zero cost unless ``PFD_PERF`` is set in the environment.  When enabled, it
records per-frame *render* and *flip* milliseconds and, every
``PFD_PERF_INTERVAL`` seconds (default 10), appends a percentile summary line
to ``PFD_PERF_FILE`` (default /tmp/pfd_perf.txt) and prints it.

Purpose: let a field tester find what's slow without a profiler or a code edit.
On the device::

    export PFD_PERF=1                 # enable; 10 s windows
    # (optional) export PFD_PERF_INTERVAL=30   # one 30 s window per line
    <run the app, pan/zoom on the page you're testing>
    cat /tmp/pfd_perf.txt

Each line separates render (CPU draw work) from flip (buffer swap / display).
If render dominates it's draw cost; if flip dominates it's the display path
(software surface / no hardware double-buffer) — two very different fixes.
"""

import os
import time


class PerfGrab:
    """Accumulates per-frame timings and flushes a percentile summary per
    window.  A no-op (near-free ``add``) unless ``PFD_PERF`` is set."""

    def __init__(self):
        self.enabled  = bool(os.environ.get("PFD_PERF"))
        self.interval = max(1.0, float(os.environ.get("PFD_PERF_INTERVAL", "10")))
        self.path     = os.environ.get("PFD_PERF_FILE", "/tmp/pfd_perf.txt")
        self._render  = []
        self._flip    = []
        # Optional handoff breakdown (shared-GL composite path): the CPU
        # serialize (tostring) and GPU upload (tex.write) halves of flip, plus
        # the leftover present (vsync/swap).  Stay empty on non-GL paths.
        self._ser     = []
        self._upl     = []
        self._pre     = []
        self._win_start = None
        self._tag     = ""
        if self.enabled:
            msg = (f"[PERF] enabled — {self.interval:.0f}s windows -> "
                   f"{self.path} (render vs flip ms, percentiles)")
            print(msg)
            try:
                with open(self.path, "a") as f:
                    f.write(f"\n# {time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"perf grab start ({self.interval:.0f}s windows)\n")
            except OSError:
                pass

    def add(self, render_ms, flip_ms, tag="", now=None,
            serialize_ms=None, upload_ms=None, present_ms=None):
        """Record one frame.  ``tag`` is an optional context label (e.g. the
        current page + range) carried into the window's summary.

        ``serialize_ms``/``upload_ms``/``present_ms`` optionally break the flip
        time into its 2D-handoff parts (CPU tostring, GPU upload, vsync present)
        so the summary can show which one to optimise.  Omit on non-GL paths."""
        if not self.enabled:
            return
        now = now if now is not None else time.monotonic()
        if self._win_start is None:
            self._win_start = now
        self._render.append(render_ms)
        self._flip.append(flip_ms)
        if serialize_ms is not None:
            self._ser.append(serialize_ms)
        if upload_ms is not None:
            self._upl.append(upload_ms)
        if present_ms is not None:
            self._pre.append(present_ms)
        if tag:
            self._tag = tag
        if now - self._win_start >= self.interval:
            self._flush(now)

    # ── internals ───────────────────────────────────────────────────────────
    @staticmethod
    def _pct(sorted_a, p):
        if not sorted_a:
            return 0.0
        k = min(len(sorted_a) - 1, int(round((p / 100.0) * (len(sorted_a) - 1))))
        return sorted_a[k]

    def _fmt(self, dur, n):
        r = sorted(self._render)
        fl = sorted(self._flip)
        fps = n / dur if dur > 0 else 0.0
        line = (f"{time.strftime('%H:%M:%S')} "
                f"fps={fps:4.1f} n={n:4d}  "
                f"render p50={self._pct(r, 50):5.1f} p95={self._pct(r, 95):5.1f} "
                f"max={(r[-1] if r else 0.0):5.1f}  "
                f"flip p50={self._pct(fl, 50):5.1f} p95={self._pct(fl, 95):5.1f} "
                f"max={(fl[-1] if fl else 0.0):5.1f}")
        # Handoff breakdown — only when the GL composite path fed it in.
        if self._ser or self._upl or self._pre:
            s = sorted(self._ser); u = sorted(self._upl); p = sorted(self._pre)
            line += (f"  [serialize p50={self._pct(s, 50):4.1f} "
                     f"upload p50={self._pct(u, 50):4.1f} "
                     f"present p50={self._pct(p, 50):4.1f}]")
        return line + (f"  [{self._tag}]" if self._tag else "")

    def _flush(self, now):
        line = self._fmt(now - self._win_start, len(self._render))
        try:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print("[PERF] " + line)
        self._render.clear()
        self._flip.clear()
        self._ser.clear()
        self._upl.clear()
        self._pre.clear()
        self._win_start = now
        self._tag = ""
