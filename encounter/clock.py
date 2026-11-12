"""Deterministic virtual clock and timer heap.

The simulation never sleeps: timers carry the *generation* (encounter epoch)
and an opaque key they were scheduled under.  A callback that survives a wipe
or phase reset is detected as stale when it fires and is dropped explicitly,
which is exactly the "late callback during reset" scenario the coordinator
must survive.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

Callback = Callable[["TimerHandle"], None]


@dataclass
class TimerHandle:
    timer_id: int
    due: float
    generation: int
    key: Tuple[Any, ...]
    callback: Callback = field(repr=False)
    cancelled: bool = False
    periodic: bool = False
    interval: float = 0.0

    def cancel(self) -> None:
        self.cancelled = True


class Clock:
    def __init__(self) -> None:
        self._now = 0.0
        self._heap: List[TimerHandle] = []
        self._next_id = 1

    @property
    def now(self) -> float:
        return self._now

    def schedule(
        self,
        delay: float,
        generation: int,
        key: Tuple[Any, ...],
        callback: Callback,
        periodic: bool = False,
        interval: float = 0.0,
    ) -> TimerHandle:
        if delay < 0:
            raise ValueError("timer delay cannot be negative")
        handle = TimerHandle(
            timer_id=self._next_id,
            due=self._now + delay,
            generation=generation,
            key=key,
            callback=callback,
            periodic=periodic,
            interval=interval,
        )
        self._next_id += 1
        heapq.heappush(self._heap, (handle.due, handle.timer_id, handle))
        return handle

    def advance(self, until: float) -> List[TimerHandle]:
        """Pop handles due at or before ``until`` and move the clock.

        The heap is re-checked after every pop because a periodic handle
        re-arms itself for ``now + interval``, which is strictly in the
        future and must not be consumed again in the same pump.
        """

        if until < self._now:
            raise ValueError("the clock cannot move backwards")
        due: List[TimerHandle] = []
        while self._heap:
            fire_at, _, handle = self._heap[0]
            if fire_at > until:
                break
            heapq.heappop(self._heap)
            if handle.cancelled:
                continue
            self._now = fire_at
            if handle.periodic:
                handle.periodic = False
                self.schedule(
                    handle.interval,
                    handle.generation,
                    handle.key,
                    handle.callback,
                    periodic=True,
                    interval=handle.interval,
                )
            due.append(handle)
        self._now = until
        return due

    def peek(self) -> Optional[float]:
        while self._heap and self._heap[0][2].cancelled:
            heapq.heappop(self._heap)
        return self._heap[0][0] if self._heap else None

    def pending(self) -> List[TimerHandle]:
        return [h for _, _, h in self._heap if not h.cancelled]

    def cancel_key(self, key: Tuple[Any, ...]) -> int:
        """Cancel every live timer sharing ``key``; returns how many."""

        count = 0
        for _, _, handle in self._heap:
            if not handle.cancelled and handle.key == key:
                handle.cancel()
                count += 1
        return count

    def clear_generation(self, generation: int) -> List[TimerHandle]:
        """Drop every timer belonging to a dead generation (used on wipe)."""

        dropped: List[TimerHandle] = []
        for _, _, handle in self._heap:
            if not handle.cancelled and handle.generation == generation:
                handle.cancel()
                dropped.append(handle)
        return dropped

    def reset(self) -> None:
        self._heap.clear()
        self._now = 0.0
        self._next_id = 1
