"""Rate limiter: never more than rpm requests a minute; the earliest-submitted team keeps reserved slots."""

import threading

from judgebench.agents.modes import RESERVE, RateLimiter


def acquire_in_thread(lim: RateLimiter, priority: float) -> threading.Thread:
    t = threading.Thread(target=lim.acquire, args=(priority,), daemon=True)
    t.start()
    t.join(0.4)
    return t


def test_later_teams_leave_reserved_slots_for_the_earliest() -> None:
    lim = RateLimiter(RESERVE + 2)
    lim.acquire(1.0)  # the earliest team (e.g. Team Beta) is active
    lim.acquire(2.0)  # a later team: 2 calls in the window, limit for later teams = rpm - RESERVE = 2
    assert acquire_in_thread(lim, 2.0).is_alive()  # later team blocked: the remaining slots are reserved
    assert not acquire_in_thread(lim, 1.0).is_alive()  # the earliest team still gets one immediately
    assert lim.sent == 3


def test_never_more_than_rpm_per_minute() -> None:
    lim = RateLimiter(3)
    for _ in range(3):
        lim.acquire(1.0)
    assert acquire_in_thread(lim, 1.0).is_alive() and lim.sent == 3
