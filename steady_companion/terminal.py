"""A terminal-only waiting indicator; provider and state work stay on the caller."""
from __future__ import annotations

import sys
import threading
import time
import unicodedata


class WaitingStatus:
    """Show elapsed time on a TTY without moving a request into a worker thread.

    The worker only writes a fixed status line. It is stopped and joined before
    the context exits, including after KeyboardInterrupt. Non-TTY streams receive
    no progress bytes, so redirected output remains readable and deterministic.
    """

    def __init__(self, stream=None, *, interval: float = 1.0):
        self.stream = stream if stream is not None else sys.stderr
        self.interval = max(0.01, interval)
        self._stop = threading.Event()
        self._thread = None
        self._width = 0
        self._started = 0.0
        self._enabled = False

    def _write(self, value: str) -> None:
        try:
            self.stream.write(value)
            self.stream.flush()
        except (OSError, ValueError):
            # A closed status stream must not abort the model call or print a
            # background traceback that could be mistaken for an agent crash.
            self._enabled = False
            self._stop.set()

    def _render(self) -> None:
        if not self._enabled:
            return
        elapsed = max(0, int(time.monotonic() - self._started))
        message = f"等待回复… {elapsed}秒；Ctrl+C取消本轮"
        width = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in message)
        self._write("\r" + message + " " * max(0, self._width - width))
        self._width = width

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._render()

    def __enter__(self):
        try:
            self._enabled = bool(self.stream.isatty())
        except (AttributeError, OSError, ValueError):
            self._enabled = False
        if self._enabled:
            self._started = time.monotonic()
            self._render()
            if self._enabled:
                self._thread = threading.Thread(target=self._run, name="steady-wait-status", daemon=True)
                self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._enabled:
            self._write("\r" + " " * self._width + "\r")
        return False
