from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import threading


LOCK_ROOT = Path(__file__).resolve().parents[1] / "data" / "locks"
_GUARD = threading.Lock()
_THREAD_LOCKS: dict[tuple[int, int], threading.RLock] = {}
_LOCAL = threading.local()


def _thread_lock(source_id: int, asset_id: int) -> threading.RLock:
    key = (int(source_id), int(asset_id))
    with _GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _held_locks() -> dict[tuple[int, int], tuple[int, object]]:
    held = getattr(_LOCAL, "held", None)
    if held is None:
        held = {}
        _LOCAL.held = held
    return held


@contextmanager
def asset_lock(source_id: int, asset_id: int):
    """Coordinate one asset mutation across threads and processes.

    The lock is deliberately re-entrant in one thread. That lets a higher level
    image-write path call a scanner refresh helper without opening a second
    ``flock`` descriptor for the same asset and risking a self-deadlock.
    """
    key = (int(source_id), int(asset_id))
    mutex = _thread_lock(*key)
    with mutex:
        held = _held_locks()
        if key in held:
            depth, handle = held[key]
            held[key] = (depth + 1, handle)
            try:
                yield
            finally:
                depth, handle = held[key]
                if depth <= 1:
                    # The outer invocation owns the OS lock and performs the
                    # actual unlock/close in its own finally block.
                    held[key] = (1, handle)
                else:
                    held[key] = (depth - 1, handle)
            return

        path = LOCK_ROOT / str(key[0]) / f"{key[1]}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[key] = (1, handle)
            try:
                yield
            finally:
                held.pop(key, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
