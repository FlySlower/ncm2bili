"""circuit_breaker.py 单测（里程碑 M7，文档 §7 响应层 b）。

- 窗口内 -412 达 3 次 → 暂停 60s；
- 达 5 次 → 暂停 300s 并降并发 50%（concurrency_multiplier=0.5）；
- 滑动窗口裁剪：超过 60s 的旧失败不计数；
- 暂停期内 wait_if_paused 触发 sleep，暂停结束恢复正常。
"""
from __future__ import annotations

import pytest

from circuit_breaker import CircuitBreaker


def _breaker(**kw) -> CircuitBreaker:
    return CircuitBreaker(**kw)


class FakeClock:
    """可推进的单调时钟，替代 time.monotonic。"""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def advance(self, delta: float) -> None:
        self.t += delta

    def __call__(self) -> float:
        return self.t


async def _noop_sleep(seconds: float) -> None:
    return None


def test_warn_threshold_pauses_60s() -> None:
    """3 次 -412 → 全局暂停 60s。"""
    clock = FakeClock()
    breaker = _breaker(now=clock)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_paused
    assert breaker.concurrency_multiplier == 1.0  # warn 不降并发


def test_warn_less_than_threshold_not_paused() -> None:
    """2 次 -412 不足阈值，不暂停。"""
    breaker = _breaker()
    for _ in range(2):
        breaker.record_failure()
    assert not breaker.is_paused


def test_critical_threshold_cuts_concurrency() -> None:
    """5 次 -412 → 暂停 300s + 降并发 50%。"""
    clock = FakeClock()
    breaker = _breaker(now=clock)
    for _ in range(5):
        breaker.record_failure()
    assert breaker.is_paused
    assert breaker.concurrency_multiplier == 0.5  # critical 降并发 50%


@pytest.mark.asyncio
async def test_wait_if_paused_sleeps_and_recovers() -> None:
    """暂停期内 wait_if_paused 触发 sleep；窗口过期后自动恢复。"""
    clock = FakeClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    breaker = _breaker(now=clock, sleep=fake_sleep)
    for _ in range(3):
        breaker.record_failure()

    await breaker.wait_if_paused()
    assert sleeps and sleeps[-1] >= 60  # 暂停 60s

    # 时间推进越过 60s 窗口：-412 滑出窗口，不再暂停
    clock.advance(61)
    assert not breaker.is_paused
    assert breaker.recent_failures == 0


def test_window_prunes_old_failures() -> None:
    """超过 60s 的旧 -412 不计入窗口。"""
    clock = FakeClock()
    breaker = _breaker(now=clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(70)  # 窗口过期
    breaker.record_failure()
    assert breaker.recent_failures == 1  # 仅新的 1 次


def test_reset_clears_state() -> None:
    """reset 清空窗口与降并发标记。"""
    breaker = _breaker()
    for _ in range(5):
        breaker.record_failure()
    assert breaker.is_paused and breaker.concurrency_multiplier == 0.5
    breaker.reset()
    assert not breaker.is_paused
    assert breaker.concurrency_multiplier == 1.0
    assert breaker.recent_failures == 0
