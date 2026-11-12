"""Event bus and the single event type driving every external representation.

The coordinator never calls into the presentation layer directly.  Every state
change that the outside world may care about is published as an :class:`Event`
on the bus; recorders, renderers and tests are all plain subscribers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass(frozen=True)
class Event:
    """An immutable fact emitted by the coordinator.

    ``seq`` and ``time`` are stamped by the bus, not by emitters, so records
    from independent subscribers always agree on ordering.
    """

    type: str
    time: float
    seq: int
    payload: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "time": self.time,
            "seq": self.seq,
            "payload": self.payload,
        }


Listener = Callable[[Event], None]


class EventBus:
    """Synchronous publish/subscribe bus with a monotonic sequence number."""

    def __init__(self) -> None:
        self._listeners: List[Listener] = []
        self._seq = 0

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def publish(self, event_type: str, time: float, **payload: Any) -> Event:
        self._seq += 1
        event = Event(event_type, time, self._seq, payload)
        for listener in list(self._listeners):
            listener(event)
        return event

    @property
    def sequence(self) -> int:
        return self._seq
