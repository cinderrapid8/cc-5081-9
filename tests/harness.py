"""Shared helpers for the encounter test suite."""

from __future__ import annotations

from typing import List

from encounter.clock import Clock
from encounter.coordinator import PhaseCoordinator
from encounter.events import Event, EventBus
from encounter.presentation import EventRenderer
from encounter.recorder import RunRecorder
from encounter.scenario import build_encounter_config


class Harness:
    """A wired coordinator with fresh bus, clock, recorder and renderer."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.clock = Clock()
        self.config = build_encounter_config()
        self.coordinator = PhaseCoordinator(self.config, self.bus, self.clock)
        self.recorder = RunRecorder(self.config.encounter_id)
        self.renderer = EventRenderer()
        self.bus.subscribe(self.recorder)
        self.bus.subscribe(self.renderer)
        self.events: List[Event] = []
        self.bus.subscribe(self.events.append)

    def start(self) -> None:
        self.coordinator.start_encounter()

    def types(self) -> List[str]:
        return [e.type for e in self.events]

    def payloads(self, event_type: str) -> List[dict]:
        return [e.payload for e in self.events if e.type == event_type]

    def ids(self, event_type: str) -> List[str]:
        return [p["mechanic_id"] for p in self.payloads(event_type)]

    def latest(self, event_type: str) -> dict:
        matches = self.payloads(event_type)
        assert matches, f"no event of type {event_type}"
        return matches[-1]

    def assert_clean_teardown(self) -> None:
        assert self.coordinator.resources.is_empty()
        assert self.renderer.view.claims == {}
        assert self.renderer.view.active_mechanics == {}
