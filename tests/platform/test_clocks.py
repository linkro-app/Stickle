"""The clock that tells this computer's clock being set from time passing."""

import time

from stickle.platform.clocks import since_start


def test_it_goes_on_with_time() -> None:
    first, started = since_start(), time.monotonic()
    time.sleep(0.2)
    passed = since_start() - first

    assert since_start() >= first
    assert abs(passed - (time.monotonic() - started) * 1000) < 100
