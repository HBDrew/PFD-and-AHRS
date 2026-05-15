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

# SDL's default audio backend picks PulseAudio/PipeWire when available
# and only falls back to ALSA otherwise. PA/PW have their own default
# sink that doesn't follow ~/.asoundrc, so the asoundrc redirect that
# pins audio to the HDMI panel is silently bypassed by pygame.mixer.
# Force ALSA before pygame imports so mixer.init() lands on the device
# the asoundrc points at. setdefault so a user can still override via
# environment if they explicitly want a different backend.
os.environ.setdefault("SDL_AUDIODRIVER", "alsa")

try:
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False


# Voice text for each callout. Short and decisive — small panel
# speakers and the pilot's brain don't want a paragraph.
_CALLOUTS = {
    "terrain":  "Terrain. Terrain.",
    "obstacle": "Obstacle. Obstacle.",
    "pull_up":  "Pull up. Pull up.",
    "bank":     "Bank angle. Bank angle.",
}

# Per-alert minimum interval between repeats (seconds). Pull-up sits
# tighter than the caution callouts because the warning band needs
# urgency, and bank-angle sits looser because pilots can spend longer
# in a hard bank than they should be allowed to spend at a TAWS
# warning.
_MIN_INTERVAL = {
    "terrain":  3.0,
    "obstacle": 3.0,
    "pull_up":  1.5,
    "bank":     3.0,
}

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".pfd_audio")

_sounds: dict = {}
_last_played: dict = {}
_initialized = False
_disabled = False
_enabled = True       # master mute (False suppresses every callout)
_volume = 1.0         # 0.0..1.0 (applied via Sound.set_volume on each load)


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


def _log_sdl_driver():
    """Best-effort diagnostic: what audio driver did SDL actually pick?
    pygame doesn't expose SDL_GetCurrentAudioDriver, so reach into the
    shared library via ctypes. Logged so we can see in journalctl what
    backend is actually live — env-var setdefault doesn't guarantee
    the hint took effect."""
    try:
        import ctypes
        sdl = ctypes.CDLL("libSDL2-2.0.so.0")
        sdl.SDL_GetCurrentAudioDriver.restype = ctypes.c_char_p
        drv = sdl.SDL_GetCurrentAudioDriver()
        print(f"[audio] SDL driver in use: "
              f"{drv.decode() if drv else 'none'}; "
              f"SDL_AUDIODRIVER env={os.environ.get('SDL_AUDIODRIVER')!r}")
    except Exception as e:
        print(f"[audio] SDL driver lookup failed: {e}")


def init():
    """Initialise the mixer and load (or generate-then-load) every
    callout WAV. Idempotent and never raises — leaves _disabled set
    when audio is unavailable so play() becomes a cheap no-op.

    Must be called AFTER pygame.init() (which auto-inits the mixer with
    defaults). We don't quit + re-init: that previously left SDL in a
    half-alive state where Sound.play() returned success but the
    audio callback thread never pumped samples. Instead, accept
    whatever sample rate / channel layout pygame chose and load the
    WAVs against it. SDL will resample 22 050 Hz callouts on the fly.
    """
    global _initialized, _disabled
    if _initialized or _disabled:
        return
    if not HAS_PYGAME:
        _disabled = True
        return

    # pygame.init() should have inited the mixer by now. If it didn't
    # (e.g. SDL_INIT_AUDIO failed silently), bring it up ourselves.
    if not pygame.mixer.get_init():
        try:
            pygame.mixer.init(frequency=22050, size=-16, channels=2,
                              buffer=512)
        except pygame.error as e:
            print(f"[audio] mixer init failed: {e}")
            _disabled = True
            return

    print(f"[audio] mixer state: {pygame.mixer.get_init()}, "
          f"channels: {pygame.mixer.get_num_channels()}")
    _log_sdl_driver()

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
            snd = pygame.mixer.Sound(path)
            snd.set_volume(_volume)
            _sounds[name] = snd
        except pygame.error as e:
            print(f"[audio] load {name} failed: {e}")

    _initialized = True
    if _sounds:
        print(f"[audio] {len(_sounds)} callouts ready: "
              f"{', '.join(sorted(_sounds))}")
        # Self-test ping: play one short callout on startup so the
        # pilot hears that audio is wired and the speaker is alive.
        # Skipped if master switch was muted via persisted setting.
        if _enabled:
            ping = _sounds.get("terrain")
            if ping is not None:
                try:
                    chan = ping.play()
                    # Confirm the mixer thinks something's playing —
                    # if get_busy() returns 0 and no channel came back,
                    # SDL accepted the play but the audio thread isn't
                    # actually pumping samples (the silent-failure
                    # mode we keep hitting).
                    busy = pygame.mixer.get_busy()
                    print(f"[audio] startup self-test: chan={chan} "
                          f"busy={busy} length={ping.get_length():.2f}s")
                except pygame.error as e:
                    print(f"[audio] startup self-test failed: {e}")
    else:
        print("[audio] no callouts loaded (espeak missing?)")


def play(name: str) -> None:
    """Play callout `name` if loaded, the master switch is on, and the
    per-alert rate limit allows. Safe to call every frame."""
    if not _initialized or not _enabled or name not in _sounds:
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


def set_enabled(on: bool) -> None:
    """Master mute: when False, play() becomes a no-op and any sound
    in flight is cut immediately."""
    global _enabled
    _enabled = bool(on)
    if not _enabled:
        stop_all()


def is_enabled() -> bool:
    return _enabled


def set_volume(vol_0_1: float) -> None:
    """Apply a 0..1 volume multiplier to every loaded callout. Live —
    takes effect on the next play() call (already-playing sound keeps
    its previous volume until the clip ends, which is short)."""
    global _volume
    _volume = max(0.0, min(1.0, float(vol_0_1)))
    for s in _sounds.values():
        try:
            s.set_volume(_volume)
        except pygame.error:
            pass


def get_volume() -> float:
    return _volume
