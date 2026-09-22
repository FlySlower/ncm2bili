"""单请求指数退避（里程碑 M7，对应文档 §7 响应层 a）。

搜索与收藏共用同一实现：初始 2s，倍数 2，最多重试 3 次（间隔 2s→4s→8s）。
参数来自 config.retry（RetryConfig），可被测试注入 sleep 断言间隔序列。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from config import RetryConfig

SleepFn = Callable[[float], Any]


def backoff_delays(config: RetryConfig) -> list[float]:
    """生成指数退避间隔序列：initial, initial*factor, ... 共 max_retries 个。"""
    delays: list[float] = []
    for i in range(config.max_retries):
        delays.append(config.initial_delay_s * (config.backoff_factor**i))
    return delays


async def sleep_before_retry(delay: float, sleep: SleepFn | None = None) -> None:
    """等待一次退避间隔（sleep 可注入测试）。"""
    if delay <= 0:
        return
    fn = sleep or asyncio.sleep
    await fn(delay)


__all__ = ["backoff_delays", "sleep_before_retry"]
