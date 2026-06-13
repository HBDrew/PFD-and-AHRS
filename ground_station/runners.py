"""
runners.py — run a build function in-process while streaming its stdout to the
GUI log.

The existing build tools report progress with print().  Rather than refactor
them, we temporarily redirect sys.stdout/stderr to a line-buffered writer that
forwards each completed line to a `log(text)` callback.  Builds run on a worker
thread (see app.py); the callback marshals lines back to the Tk main loop.
"""

import contextlib
import io
import sys


class _LineWriter(io.TextIOBase):
    """A text sink that calls `emit(line)` once per newline-terminated line.

    Reentrancy-safe: if `emit` itself writes to stdout (e.g. a logger that
    prints), that write is routed to the real stream instead of looping back
    in."""

    def __init__(self, emit, real):
        super().__init__()
        self._emit = emit
        self._real = real
        self._buf = ""
        self._in_emit = False

    def write(self, s):
        if not s:
            return 0
        if self._in_emit:                  # reentrant write from emit → passthru
            self._real.write(s)
            return len(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._in_emit = True
            try:
                self._emit(line)
            finally:
                self._in_emit = False
        return len(s)

    def flush(self):
        if self._buf and not self._in_emit:
            line, self._buf = self._buf, ""
            self._in_emit = True
            try:
                self._emit(line)
            finally:
                self._in_emit = False


@contextlib.contextmanager
def capture_to(log):
    """Redirect stdout+stderr to `log(line)` for the duration of the block."""
    old_out, old_err = sys.stdout, sys.stderr
    writer = _LineWriter(log, old_out)
    sys.stdout = writer
    sys.stderr = writer
    try:
        yield
    finally:
        writer.flush()
        sys.stdout, sys.stderr = old_out, old_err


def run_main(module, argv, log):
    """Call a tool module's main() with a synthesized argv, streaming output.

    Mirrors `python3 tools/<tool>.py <argv...>` but in-process so it works in a
    frozen binary.  Returns True on clean completion, False if main() raised or
    called sys.exit(non-zero)."""
    old_argv = sys.argv
    sys.argv = [getattr(module, "__file__", "tool")] + list(argv)
    try:
        with capture_to(log):
            try:
                module.main()
            except SystemExit as exc:
                code = exc.code
                return code in (0, None)
        return True
    except Exception as exc:                       # surface, don't crash the GUI
        log(f"ERROR: {exc}")
        return False
    finally:
        sys.argv = old_argv
