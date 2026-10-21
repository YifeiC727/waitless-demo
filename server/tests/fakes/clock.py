"""FakeClock：可手动推进的时钟（tech §12；实现 waitless.interfaces.Clock）。

用法：
    clock = FakeClock()
    task = asyncio.create_task(something_that_sleeps(clock))
    await clock.advance(20_000)      # 推进 20 s，唤醒到期的 sleep
"""

import asyncio
import heapq
import itertools
from datetime import datetime, timedelta, timezone

DEFAULT_START_WALL = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, *, start_ms: int = 0, start_wall: datetime | None = None, yields: int = 5) -> None:
        if start_wall is not None and start_wall.tzinfo is None:
            raise ValueError("start_wall must be timezone-aware")
        self._start_ms = start_ms
        self._now_ms = start_ms
        self._wall0 = start_wall or DEFAULT_START_WALL
        self._sleepers: list[tuple[int, int, asyncio.Future[None]]] = []
        self._counter = itertools.count()
        self._yields = yields
        self.sleep_calls: list[float] = []

    # -- Clock 接口 ---------------------------------------------------------
    def monotonic_ms(self) -> int:
        return self._now_ms

    def now(self) -> datetime:
        return self._wall0 + timedelta(milliseconds=self._now_ms - self._start_ms)

    def now_iso(self) -> str:
        return self.now().isoformat()

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        delay_ms = int(round(seconds * 1000))
        if delay_ms <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._now_ms + delay_ms, next(self._counter), fut))
        await fut

    # -- 测试控制 -----------------------------------------------------------
    async def advance(self, ms: int) -> None:
        """推进 ms 毫秒；按到期顺序唤醒 sleep，并让被唤醒的协程有机会继续运行。"""
        if ms < 0:
            raise ValueError("cannot go back in time")
        target = self._now_ms + ms
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, fut = heapq.heappop(self._sleepers)
            self._now_ms = max(self._now_ms, deadline)
            if not fut.done():
                fut.set_result(None)
            await self._let_run()
        self._now_ms = target
        await self._let_run()

    def advance_sync(self, ms: int) -> None:
        """同步推进；有到期的 sleep 时报错（那种情况必须用 await advance）。"""
        if ms < 0:
            raise ValueError("cannot go back in time")
        target = self._now_ms + ms
        if any(d <= target and not f.done() for d, _, f in self._sleepers):
            raise RuntimeError("sleepers would wake; use `await clock.advance(ms)`")
        self._now_ms = target

    async def run_until_idle(self) -> None:
        await self._let_run()

    @property
    def pending_sleepers(self) -> int:
        return sum(1 for _, _, f in self._sleepers if not f.done())

    @property
    def next_deadline_ms(self) -> int | None:
        live = [d for d, _, f in self._sleepers if not f.done()]
        return min(live) if live else None

    async def _let_run(self) -> None:
        for _ in range(self._yields):
            await asyncio.sleep(0)
