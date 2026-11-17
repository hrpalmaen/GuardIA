"""Virtual clock and scheduler: a whole night of GuardIA in seconds.

`LocalScheduler` replaces EventBridge Scheduler: jobs run every N minutes
(DiscoverProcesses every 15, MissedRunWatchdog every 5) or once at a given
time (a replayed execution finishing). `run_until` jumps from one due job to
the next, setting the clock first, so every handler sees the virtual time.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass
class VirtualClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def set(self, moment: datetime) -> None:
        if moment < self.current:
            raise ValueError("the virtual clock cannot go back")
        self.current = moment


@dataclass(order=True)
class _Job:
    due: datetime
    seq: int
    name: str = field(compare=False)
    action: Callable[[datetime], Any] = field(compare=False)
    every: timedelta | None = field(compare=False, default=None)


@dataclass
class LocalScheduler:
    clock: VirtualClock
    log: list[tuple[datetime, str, Any]] = field(default_factory=list)
    _queue: list[_Job] = field(default_factory=list)
    _seq: Any = field(default_factory=itertools.count)

    def every(
        self, interval: timedelta, name: str, action: Callable[[datetime], Any],
        first: datetime | None = None,
    ) -> None:  # fmt: skip
        due = first or self.clock()
        heapq.heappush(self._queue, _Job(due, next(self._seq), name, action, interval))

    def at(self, moment: datetime, name: str, action: Callable[[datetime], Any]) -> None:
        heapq.heappush(self._queue, _Job(moment, next(self._seq), name, action))

    def run_until(self, end: datetime) -> None:
        while self._queue and self._queue[0].due <= end:
            job = heapq.heappop(self._queue)
            self.clock.set(max(job.due, self.clock()))
            self.log.append((job.due, job.name, job.action(job.due)))
            if job.every:
                job.due += job.every
                heapq.heappush(self._queue, job)
        self.clock.set(max(end, self.clock()))
