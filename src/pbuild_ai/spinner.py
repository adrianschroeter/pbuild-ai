import itertools
import os
import sys
import threading
import time


_SPINNER_ENABLED = (
    os.environ.get("NO_SPINNER", "").lower() not in ("1", "true", "yes")
    and sys.stderr.isatty()
)
_COLORS_ENABLED = (
    _SPINNER_ENABLED
    and os.environ.get("NO_COLOR", "").lower() not in ("1", "true", "yes")
)


def _color(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLORS_ENABLED else text


CYAN = "36"
YELLOW = "33"
GREEN = "32"

AI_COLOR = CYAN


class Spinner:

    CHARS = itertools.cycle("⣾⣽⣻⢿⡿⣟⣯⣷")

    def __init__(self, prefix="", color=None, delay=0.1):
        self.prefix = _color(color, prefix) if color else prefix
        self.delay = delay
        self._running = False
        self._thread = None
        self._t0 = None

    def start(self):
        if not _SPINNER_ENABLED:
            return
        self._t0 = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self):
        while self._running:
            elapsed = time.time() - self._t0
            self._stream_write(f"\r{self.prefix} {next(self.CHARS)}  ({elapsed:.1f}s)")
            time.sleep(self.delay)

    def stop(self):
        if not _SPINNER_ENABLED:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.3)
        self._stream_write("\r\n")

    @staticmethod
    def _stream_write(text):
        sys.stderr.write(text)
        sys.stderr.flush()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
