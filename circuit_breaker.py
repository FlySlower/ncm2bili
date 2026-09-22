"""全局熔断器（里程碑 M7，对应文档 §7 响应层 b）。

滑动窗口 60s 内 API code `-412`（风控）计数：
- 达 3 次（threshold_warn）→ 全局暂停 60s（pause_warn_s），输出 WARNING；
- 达 5 次（threshold_critical）→ 暂停 5min（pause_critical_s）并降并发 50%，输出 WARNING。

配合单请求指数退避（§7 响应层 a）与间隔 jitter（§7 预防层）形成三层风控。
时间源与 sleep 均可注入，便于测试（freezegun / mock sleep）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from config import CircuitBreakerConfig

logger = logging.getLogger(__name__)

# 注入类型：sleep(seconds) 协程；now() 返回单调时钟秒
SleepFn = Callable[[float], Any]
NowFn = Callable[[], float]


class CircuitBreaker:
    """全局熔断器：-412 滑动窗口计数 → 暂停 + 降并发。"""

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        *,
        sleep: SleepFn | None = None,
        now: NowFn | None = None,
    ) -> None:
        self._cfg = config or CircuitBreakerConfig()
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.monotonic
        # 滑动窗口内 -412 的时间戳（单调时钟）
        self._window: deque[float] = deque()
        self._paused_until: float = 0.0
        self._concurrency_cut = False  # 是否已触发降并发 50%

    # ---- 状态查询 -------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """当前是否处于全局暂停中。"""
        return self._now() < self._paused_until

    @property
    def concurrency_multiplier(self) -> float:
        """并发倍率：触发 critical 熔断后为 0.5，否则 1.0。"""
        return self._cfg.concurrency_cut if self._concurrency_cut else 1.0

    @property
    def recent_failures(self) -> int:
        """窗口内当前的 -412 计数。"""
        self._prune_window()
        return len(self._window)

    # ---- 熔断判定 -------------------------------------------------

    def record_failure(self) -> None:
        """记录一次 -412 风控；达阈值时触发全局暂停（并可能降并发）。"""
        now = self._now()
        self._window.append(now)
        self._prune_window()

        n = len(self._window)
        if n >= self._cfg.threshold_critical:
            if not self._concurrency_cut:
                self._concurrency_cut = True
                logger.warning(
                    "触发风控熔断：窗口内 %d 次 -412，暂停 %ds 并降并发 50%%",
                    n, self._cfg.pause_critical_s,
                )
            self._paused_until = max(self._paused_until, now + self._cfg.pause_critical_s)
        elif n >= self._cfg.threshold_warn:
            logger.warning(
                "触发风控熔断：窗口内 %d 次 -412，暂停 %ds", n, self._cfg.pause_warn_s,
            )
            self._paused_until = max(self._paused_until, now + self._cfg.pause_warn_s)

    async def wait_if_paused(self) -> None:
        """若处于暂停期，sleep 至暂停结束（配合降并发生效）。"""
        if not self.is_paused:
            return
        remaining = self._paused_until - self._now()
        if remaining > 0:
            logger.info("全局熔断暂停中，剩余 %.0fs", remaining)
            await self._sleep(remaining)

    def reset(self) -> None:
        """清空窗口（测试/恢复用）。"""
        self._window.clear()
        self._paused_until = 0.0
        self._concurrency_cut = False

    # ---- 工具 -----------------------------------------------------

    def _prune_window(self) -> None:
        cutoff = self._now() - self._cfg.window_s
        while self._window and self._window[0] < cutoff:
            self._window.popleft()


__all__ = ["CircuitBreaker"]
