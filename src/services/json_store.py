"""Atomic JSON persistence shared by the sidecar stores (review queue,
report history/users/profile, statement overrides, OCR settings).

Two guarantees the bare ``Path.write_text`` pattern lacked:

- **write atomicity** — write to a temp file then ``os.replace``. A crash
  mid-write can never leave truncated JSON behind; the stores' ``_read()``
  helpers "recover" from corrupt JSON by returning an empty default, so a
  torn write used to silently wipe the whole store on the next save.
- **mutual exclusion** — one lock per file serialises read-modify-write
  sequences across threads (uvicorn's ``to_thread`` pool, the folder-watcher
  thread, Gmail sync). Without it, two concurrent writers mint the same id
  or drop each other's items.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(path) -> threading.Lock:
    """One lock per file path, lazily created."""
    key = str(Path(path))
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def locked(path_factory: Callable[[], Path]):
    """Decorator: hold the file's lock for the whole function — the lock must
    span read→mutate→write, not just the write."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with lock_for(path_factory()):
                return fn(*args, **kwargs)

        return wrapper

    return deco


def write_json(path, data) -> None:
    """Atomically replace ``path`` with the JSON serialisation of ``data``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
