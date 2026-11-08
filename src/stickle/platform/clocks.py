"""A clock that only goes on and counts while the computer sleeps too: what
tells this computer's clock being set from time passing (stickle.core.cloud_time).
"""

import ctypes
import sys
import time


def since_start() -> int:
    """Milliseconds since the computer started, the time it slept included."""
    if sys.platform == "win32":
        # The tick count goes on through sleep and hibernation (the "unbiased"
        # interrupt time is the one that leaves them out).
        tick_count = ctypes.windll.kernel32.GetTickCount64
        tick_count.restype = ctypes.c_uint64
        return int(tick_count())
    if sys.platform == "linux":
        # CLOCK_MONOTONIC, which time.monotonic reads, stops while suspended.
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000
    if sys.platform == "darwin":
        # Here CLOCK_MONOTONIC goes on while asleep; CLOCK_UPTIME_RAW does not.
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1_000_000
    return time.monotonic_ns() // 1_000_000
