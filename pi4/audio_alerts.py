"""Aviation-style audio alerts for the PFD.

Callouts are short voice WAVs generated once via espeak at first run
and cached in ~/.pfd_audio/, then played through pygame.mixer.
Subsequent boots skip the espeak step entirely. Rate-limited per
alert so a persistent condition doesn't spam the speaker —
matches standard EGPWS / TAWS callout pacing.

Module-level init() runs once at startup; play(name) is safe to call
every render frame (returns immediately when the rate limit hasn't
elapsed, when audio is unavailable, or when the callout WAV is
missing). Failures are silent and never raise — audio is
non-essential, the visual alerts still carry the load.
"""
import os
import subprocess
import time

try:
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False


# Voice text for each callout. Short and decisive — small panel
# speakers and the pilot's brain don't want a paragraph.
_CALLOUTS = {
    "terrain":  "Terrain. Terrain.",
    "pull_up":  "Pull up. Pull up.",
    "bank":     "Bank angle. Bank angle.",
}

# Per-alert minimum interval between repeats (seconds). Pull-up sits
# tighter than terrain because the warning band needs urgency, and
# bank-angle sits looser because pilots can spend longer in a hard
# bank than they should be allowed to spend at a TAWS warning.
_MIN_INTERVAL = {
    "terrain":  3.0,
    "pull_up":  1.5,
    "bank":     3.0,
}

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".pfd_audio")

_sounds: dict = {}
_last_played: dict = {}
_initialized = False
_disabled = False


def _generate_wav(text: str, path: str) -> bool:
    """One-shot espeak invocation. Returns True on success, False (and
    prints diagnostic) when espeak is missing or fails."""
    try:
        subprocess.run(
            ["espeak", "-w", path, "-s", "150", "-a", "200", text],
            check=True, capture_output=True, timeout=10,
        )
    except FileNotFoundError:
        print("[audio] espeak not installed — "
              "sudo apt install espeak  to enable voice callouts")
        return False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[audio] espeak failed for {os.path.basename(path)}: {e}")
        return False
    return os.path.exists(path) and os.path.getsize(path) > 0


def init():
    """Initialise the mixer and load (or generate-then-load) every
    callout WAV. Idempotent and never raises — leaves _disabled set
    when audio is unavailable so play() becomes a cheap no-op."""
    global _initialized, _disabled
    if _initialized or _disabled:
        return
    if not HAS_PYGAME:
        _disabled = True
        return
    try:
        # Small buffer keeps callout-to-speaker latency under ~30 ms,
        # which matters when the warning band fires.
        pygame.mixer.init(frequency=22050, size=-16, channels=2, buffer=512)
    except pygame.error as e:
        print(f"[audio] mixer init failed: {e}")
        _disabled = True
        return

    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
    except OSError as e:
        print(f"[audio] cache dir unavailable: {e}")
        _disabled = True
        return

    for name, text in _CALLOUTS.items():
        path = os.path.join(_CACHE_DIR, name + ".wav")
        if not os.path.exists(path):
            if not _generate_wav(text, path):
                continue
        try:
            _sounds[name] = pygame.mixer.Sound(path)
        except pygame.error as e:
            print(f"[audio] load {name} failed: {e}")

    _initialized = True
    if _sounds:
        print(f"[audio] {len(_sounds)} callouts ready: "
              f"{', '.join(sorted(_sounds))}")
    else:
        print("[audio] no callouts loaded (espeak missing?)")


def play(name: str) -> None:
    """Play callout `name` if loaded and the per-alert rate limit
    allows. Safe to call every frame — the rate limit handles
    repetition for persistent conditions."""
    if not _initialized or name not in _sounds:
        return
    now = time.monotonic()
    if now - _last_played.get(name, 0.0) < _MIN_INTERVAL.get(name, 3.0):
        return
    _last_played[name] = now
    try:
        _sounds[name].play()
    except pygame.error:
        pass


def stop_all() -> None:
    """Cut any currently-playing callout. Used on shutdown or when
    entering a state where alerts shouldn't sound."""
    if not _initialized:
        return
    try:
        pygame.mixer.stop()
    except pygame.error:
        pass
