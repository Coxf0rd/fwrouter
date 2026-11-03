"""Small owner for one cooperative background worker's thread lifecycle.

The worker's loop, enablement policy, cadence and error reporting remain with
its scheduler. This object only owns the thread handle, stop signal and lock
that make start/stop idempotent and retain a worker that outlives a join.
"""
from __future__ import annotations

from threading import Event, Lock, Thread
from typing import Callable


class WorkerLifecycle:
    """Instance-owned start/stop state for one scheduler worker.

    The worker target must use ``stop_event`` for cooperative cancellation and
    must not call back into this object's start/stop methods. Holding the lock
    through ``join`` serializes concurrent start/stop requests; this is safe
    because worker loops only wait on the Event and never acquire this lock.
    """

    def __init__(self) -> None:
        self.stop_event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None

    @property
    def thread(self) -> Thread | None:
        """Return the currently owned worker handle for observation/testing."""
        with self._lock:
            return self._thread

    def start(
        self,
        target: Callable[[], None],
        *,
        name: str,
        thread_factory: Callable[..., Thread] = Thread,
    ) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            self.stop_event.clear()
            worker = thread_factory(target=target, name=name, daemon=True)
            self._thread = worker
            try:
                worker.start()
            except BaseException:
                if worker.is_alive():
                    # Some custom factories can report a post-start failure.
                    # Keep the live handle and request cooperative exit.
                    self.stop_event.set()
                else:
                    # A failed pre-start Thread cannot be joined. Do not retain
                    # it as though a worker were still owned.
                    self._thread = None
                raise
            return True

    def stop(self, *, timeout_seconds: float = 2.0) -> bool:
        with self._lock:
            worker = self._thread
            if worker is None:
                return False

            self.stop_event.set()
            worker.join(timeout=max(0.0, timeout_seconds))
            if worker.is_alive():
                # Keep both the handle and set stop signal. A later start must
                # not clear cancellation while this worker still owns the slot.
                return False

            self._thread = None
            return True
