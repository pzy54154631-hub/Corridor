"""Two bounded thread pools for session work, with the session's display context bound.

Controllers are not thread-safe, so every executor job of one session runs under that
session's asyncio lock (what a handler does before it submits the job, such as parsing
the body or choosing the pool, runs outside it). Calls that may reach a live model run in
a small pool of their own, so a slow call never exhausts the pool everything else uses. A
client that disconnects neither cancels the work nor releases the lock early: the call
finishes, its result is recorded, and only then is the lock released.
"""

import asyncio
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Literal, TypeVar

from corridor.i18n import reset_locale, set_locale
from corridor.ui.audience import reset_audience, set_audience
from corridor.web.errors import error

T = TypeVar("T")
Pool = Literal["main", "live"]


@contextmanager
def bound(ws: Any) -> Generator[None, None, None]:
    """The session's locale and audience in this thread, restored afterwards.

    Thread pools do not copy context variables, and the audience defaults to the judge
    view, so every job sets both explicitly.
    """
    locale = set_locale(ws.locale)
    audience = set_audience(ws.view)
    try:
        yield
    finally:
        reset_audience(audience)
        reset_locale(locale)


def _bound_call(ws: Any, job: Callable[[], T]) -> T:
    with bound(ws):
        return job()


def _observed(task: "asyncio.Task[Any]") -> None:
    # The awaiting request may be gone; the outcome was recorded by the job itself.
    if not task.cancelled():
        task.exception()


class Lane:
    def __init__(self, name: str, workers: int, queue: int) -> None:
        if workers < 1 or queue < 0:
            raise ValueError("A pool needs one worker and a non-negative queue")
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"web-{name}")
        self.limit = workers + queue
        self.admitted = 0


class Executor:
    def __init__(
        self, *, pool_main: int = 4, pool_live: int = 2, queue_main: int = 16, queue_live: int = 4
    ) -> None:
        self.lanes = {
            "main": Lane("main", pool_main, queue_main),
            "live": Lane("live", pool_live, queue_live),
        }

    def _admit(self, pool: Pool) -> Lane:
        lane = self.lanes[pool]
        if lane.admitted >= lane.limit:
            # Refused before the lock: no controller is touched.
            raise error("busy")
        lane.admitted += 1
        return lane

    async def run(self, ws: Any, job: Callable[[], T], *, pool: Pool) -> T:
        """``job()`` in a pool thread, under the session lock, in the session's context."""
        lane = self._admit(pool)
        ws.busy += 1
        loop = asyncio.get_running_loop()

        async def locked() -> T:
            try:
                async with ws.lock:
                    return await loop.run_in_executor(lane.pool, _bound_call, ws, job)
            finally:
                ws.busy -= 1
                lane.admitted -= 1

        task = asyncio.ensure_future(locked())
        task.add_done_callback(_observed)
        return await asyncio.shield(task)

    async def call(self, job: Callable[[], T], *, pool: Pool = "main") -> T:
        """Work that belongs to no session yet (creating one), under the same bound."""
        lane = self._admit(pool)
        loop = asyncio.get_running_loop()

        async def counted() -> T:
            try:
                return await loop.run_in_executor(lane.pool, job)
            finally:
                lane.admitted -= 1

        task = asyncio.ensure_future(counted())
        task.add_done_callback(_observed)
        return await asyncio.shield(task)

    def shutdown(self) -> None:
        for lane in self.lanes.values():
            lane.pool.shutdown(wait=False, cancel_futures=True)


_process: Executor | None = None


async def run(ws: Any, job: Callable[[], T], *, pool: Pool) -> T:
    """The contract's module-level form, on a process-wide executor.

    Each application built by ``make_app`` has its own executor (``settings["executor"]``)
    sized from its settings; its handlers use that one.
    """
    global _process
    if _process is None:
        _process = Executor()
    return await _process.run(ws, job, pool=pool)
