"""Keep Windows from going to sleep while a book is being read (the screen may still turn off)."""

from __future__ import annotations

import contextlib
import os

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake():
    if os.name != "nt":
        yield
        return
    import ctypes

    kernel = ctypes.windll.kernel32
    kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        kernel.SetThreadExecutionState(ES_CONTINUOUS)
