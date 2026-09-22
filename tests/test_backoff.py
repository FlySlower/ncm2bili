"""backoff.py 单测（里程碑 M7，文档 §7 响应层 a）。

- 指数退避间隔序列 2s→4s→8s（初始 2，倍数 2，最多 3 次）；
- 搜索与收藏共用同一实现（import 同一函数）；
- mock sleep 断言实际等待间隔符合指数序列。
"""
from __future__ import annotations

import pytest

from backoff import backoff_delays, sleep_before_retry
from config import Config


def test_backoff_delays_exponential_sequence() -> None:
    """默认配置下退避间隔为 2s→4s→8s。"""
    delays = backoff_delays(Config().risk_control.retry)
    assert delays == [2.0, 4.0, 8.0]


def test_backoff_delays_custom_config() -> None:
    """参数来自 config.retry（初始/倍数/次数可调）。"""
    from config import RetryConfig

    delays = backoff_delays(RetryConfig(initial_delay_s=1, backoff_factor=3, max_retries=4))
    assert delays == [1.0, 3.0, 9.0, 27.0]


@pytest.mark.asyncio
async def test_sleep_before_retry_uses_sequence() -> None:
    """实际等待间隔与指数序列一致（mock sleep 断言）。"""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    for delay in backoff_delays(Config().risk_control.retry):
        await sleep_before_retry(delay, sleep=fake_sleep)

    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
async def test_sleep_before_retry_skips_non_positive() -> None:
    """0/负间隔不 sleep（测试注入 0 序列时保持快速）。"""
    slept = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal slept
        slept = True

    await sleep_before_retry(0.0, sleep=fake_sleep)
    assert not slept


def test_bili_and_fav_share_same_backoff() -> None:
    """搜索与收藏共用同一退避实现（import 同一 backoff 模块）。"""
    import bili_search
    import fav

    assert bili_search.sleep_before_retry is sleep_before_retry
    assert fav.sleep_before_retry is sleep_before_retry
