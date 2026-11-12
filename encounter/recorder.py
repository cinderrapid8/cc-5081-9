"""Run record: an event subscriber that captures the whole encounter.

The recorder is deliberately passive.  It holds no gameplay authority; it
serializes what the coordinator emitted so a run can be replayed, audited or
diffed after the fact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .events import Event


@dataclass
class RunRecord:
    encounter_id: str
    generation: int = 0
    events: List[Event] = field(default_factory=list)
    final_status: str = "idle"

    def event_types(self) -> List[str]:
        return [e.type for e in self.events]

    def events_of(self, event_type: str) -> List[Event]:
        return [e for e in self.events if e.type == event_type]

    def phase_timeline(self) -> List[Dict[str, Any]]:
        return [
            {"time": e.time, "phase_id": e.payload["phase_id"], "index": e.payload["index"]}
            for e in self.events_of("phase_enter")
        ]

    def mechanic_timeline(self) -> List[Dict[str, Any]]:
        timeline = []
        for event in self.events:
            if event.type in (
                "mechanic_start",
                "mechanic_end",
                "mechanic_blocked",
                "mechanic_revoked",
                "mechanic_deferred",
                "mechanic_carried_over",
            ):
                timeline.append(
                    {"time": event.time, "type": event.type, **event.payload}
                )
        return timeline

    def to_dict(self) -> Dict[str, Any]:
        return {
            "encounter_id": self.encounter_id,
            "generation": self.generation,
            "final_status": self.final_status,
            "event_count": len(self.events),
            "phase_timeline": self.phase_timeline(),
            "mechanic_timeline": self.mechanic_timeline(),
            "events": [e.as_dict() for e in self.events],
        }

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)


class RunRecorder:
    """Subscribes to a bus and keeps one :class:`RunRecord` per generation."""

    def __init__(self, encounter_id: str) -> None:
        self.encounter_id = encounter_id
        self.records: List[RunRecord] = []
        self._current: RunRecord | None = None

    def __call__(self, event: Event) -> None:
        if event.type == "encounter_start":
            self._current = RunRecord(
                encounter_id=self.encounter_id,
                generation=event.payload["generation"],
            )
            self.records.append(self._current)
        if self._current is not None:
            self._current.events.append(event)
            if event.type in ("victory", "wipe"):
                self._current.final_status = event.type

    @property
    def current(self) -> RunRecord:
        if self._current is None:
            raise RuntimeError("no encounter has started yet")
        return self._current

    @property
    def latest(self) -> RunRecord:
        if not self.records:
            raise RuntimeError("no records captured")
        return self.records[-1]
