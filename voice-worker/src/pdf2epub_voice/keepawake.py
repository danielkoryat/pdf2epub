"""Keep Windows from going to sleep while a book is being read (the screen may still turn off), and from slowing
the reading down.

Windows 11 runs a process with no window of its own in "efficiency mode" when it sees fit, on slow processor
cores. The app starts the worker without a window when it restarts itself, and the worker then read at 1x real
time instead of 9x (it feeds the GPU from the processor, and Renikud runs there): 750 letters a minute instead
of 6,700 on 2026-10-23. Asking for full speed turns that off for this process.
"""

from __future__ import annotations

import contextlib
import os

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS ProcessPowerThrottling
THROTTLE_EXECUTION_SPEED = 0x1
THROTTLE_IGNORE_TIMER_RESOLUTION = 0x4


@contextlib.contextmanager
def keep_awake():
    if os.name != "nt":
        yield
        return
    import ctypes

    kernel = ctypes.windll.kernel32
    kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    full_speed()
    try:
        yield
    finally:
        kernel.SetThreadExecutionState(ES_CONTINUOUS)


def full_speed() -> bool:
    """Opt this process out of Windows' power throttling (efficiency mode)."""
    import ctypes
    from ctypes import wintypes

    class State(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    # The policy is controlled (ControlMask) and switched off (StateMask 0).
    state = State(1, THROTTLE_EXECUTION_SPEED | THROTTLE_IGNORE_TIMER_RESOLUTION, 0)
    return bool(kernel.SetProcessInformation(wintypes.HANDLE(kernel.GetCurrentProcess()), PROCESS_POWER_THROTTLING,
                                             ctypes.byref(state), ctypes.sizeof(state)))
