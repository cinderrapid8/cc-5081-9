"""HTTP-backed graphical client for the boss encounter.

Why HTTP rather than a desktop toolkit: this environment ships without Tk, so
the graphical entry point is a small standard-library server plus a single HTML
page.  Two modes are supported:

* ``live`` -- an interactive boss encounter.  Each button calls the production
  :class:`~encounter.coordinator.PhaseCoordinator`; the page re-renders the
  live phase, boss HP, target HP, players, occupancy and mechanic positions.
* ``replay`` -- opens an acceptance scenario or failure replay file and steps
  through the recorded actions, so a failed property check can be inspected at
  the exact mechanic position where it happened.

There is deliberately no canned data for phases or occupancy: the JSON the page
consumes is produced from the coordinator itself.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..acceptance.actions import Scenario, action_from_dict
from ..acceptance.replay import (
    DEFAULT_REPLAY_DIR,
    load_scenario,
)
from ..clock import Clock
from ..coordinator import PhaseCoordinator
from ..events import Event, EventBus
from ..presentation import EventRenderer
from ..scenario import build_encounter_config

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class ClientSession:
    """A live encounter plus the event history the graphical page renders."""

    def __init__(self) -> None:
        self.config = build_encounter_config()
        self.bus = EventBus()
        self.clock = Clock()
        self.coordinator = PhaseCoordinator(self.config, self.bus, self.clock)
        self.renderer = EventRenderer()
        self.bus.subscribe(self.renderer)
        self.events: List[Event] = []
        self.bus.subscribe(self.events.append)
        # Re-entrant: replay helpers hold the lock and call the same action
        # machinery a live button uses.
        self._lock = threading.RLock()
        # Replay state.
        self.replay_scenario: Optional[Scenario] = None
        self.replay_cursor = 0
        self.replay_source: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Live state, read straight from the production coordinator
    # ------------------------------------------------------------------ #

    def state(self) -> Dict[str, Any]:
        with self._lock:
            coordinator = self.coordinator
            claims = [
                {"resource": claim.resource, "owner": claim.owner}
                for claim in coordinator.resources.active_claims()
            ]
            active = [
                {
                    "mechanic_id": instance.mechanic_id,
                    "name": instance.config.name,
                    "phase_id": self.config.phase_of(instance.mechanic_id).phase_id,
                    "resources": list(instance.config.resources),
                    "mutex_group": instance.config.mutex_group,
                    "start_time": instance.start_time,
                }
                for instance in coordinator.active_mechanics
            ]
            return {
                "mode": "replay" if self.replay_scenario else "live",
                "replay_source": self.replay_source,
                "replay_cursor": self.replay_cursor,
                "replay_length": (
                    len(self.replay_scenario.actions)
                    if self.replay_scenario
                    else 0
                ),
                "encounter_id": self.config.encounter_id,
                "time": self.clock.now,
                "generation": coordinator.generation,
                "status": coordinator.status,
                "phase": (
                    {
                        "phase_id": coordinator.current_phase.phase_id,
                        "name": coordinator.current_phase.name,
                        "index": coordinator.phase_index,
                    }
                    if coordinator.current_phase is not None
                    else None
                ),
                "boss": {
                    "boss_id": self.config.boss.boss_id,
                    "name": self.config.boss.name,
                    "hp": coordinator.boss_hp,
                    "max_hp": self.config.boss.max_hp,
                },
                "targets": [
                    {
                        "target_id": target.target_id,
                        "name": target.name,
                        "hp": coordinator.target_hp.get(target.target_id, 0.0),
                        "max_hp": target.max_hp,
                        "destroyed": coordinator.target_hp.get(target.target_id, 0.0)
                        == 0.0,
                    }
                    for target in self.config.targets
                ],
                "players": sorted(coordinator.players),
                "occupancy": claims,
                "active_mechanics": active,
                "mechanic_records": [
                    {
                        "mechanic_id": instance.mechanic_id,
                        "status": instance.status.value,
                        "detail": instance.detail,
                        "generation": instance.generation,
                    }
                    for instance in coordinator.mechanics.values()
                ],
                "transcript_tail": self.renderer.transcript[-12:],
                "last_events": [event.as_dict() for event in self.events[-8:]],
            }

    # ------------------------------------------------------------------ #
    # Actions -- every button maps to a real coordinator call
    # ------------------------------------------------------------------ #

    def act(self, action: Tuple[Any, ...]) -> Dict[str, Any]:
        with self._lock:
            kind = action[0]
            coordinator = self.coordinator
            if kind == "start":
                coordinator.start_encounter()
            elif kind == "wait":
                coordinator.tick(self.clock.now + float(action[1]))
            elif kind == "boss_damage":
                coordinator.apply_damage(
                    self.config.boss.boss_id, float(action[1]), action[2]
                )
            elif kind == "target_damage":
                coordinator.apply_damage(action[1], float(action[2]), action[3])
            elif kind == "destroy":
                coordinator.destroy_target(action[1], source=action[2])
            elif kind == "kill":
                coordinator.player_dies(action[1])
            elif kind == "interrupt":
                coordinator.interrupt_mechanic(action[1], action[2])
            else:
                raise ValueError(f"unsupported client action {kind!r}")
            return self.state()

    def reset(self) -> Dict[str, Any]:
        with self._lock:
            self.coordinator.reset_after_wipe()
            return self.state()

    def new_session(self) -> Dict[str, Any]:
        with self._lock:
            self.bus = EventBus()
            self.clock = Clock()
            self.coordinator = PhaseCoordinator(self.config, self.bus, self.clock)
            self.renderer = EventRenderer()
            self.bus.subscribe(self.renderer)
            self.events = []
            self.bus.subscribe(self.events.append)
            self.replay_scenario = None
            self.replay_cursor = 0
            self.replay_source = None
            return self.state()

    # ------------------------------------------------------------------ #
    # Replay / scenario opening
    # ------------------------------------------------------------------ #

    def open_scenario(self, path: str) -> Dict[str, Any]:
        scenario = load_scenario(path)
        with self._lock:
            self.bus = EventBus()
            self.clock = Clock()
            self.coordinator = PhaseCoordinator(self.config, self.bus, self.clock)
            self.renderer = EventRenderer()
            self.bus.subscribe(self.renderer)
            self.events = []
            self.bus.subscribe(self.events.append)
            self.replay_scenario = scenario
            self.replay_cursor = 0
            self.replay_source = os.path.basename(path)
            return self.state()

    def replay_step(self) -> Dict[str, Any]:
        with self._lock:
            if self.replay_scenario is None:
                raise ValueError("no replay loaded")
            if self.replay_cursor >= len(self.replay_scenario.actions):
                return self.state()
            action = self.replay_scenario.actions[self.replay_cursor]
            self._apply(action)
            self.replay_cursor += 1
            return self.state()

    def replay_all(self) -> Dict[str, Any]:
        with self._lock:
            if self.replay_scenario is None:
                raise ValueError("no replay loaded")
            while self.replay_cursor < len(self.replay_scenario.actions):
                action = self.replay_scenario.actions[self.replay_cursor]
                self._apply(action)
                self.replay_cursor += 1
            return self.state()

    def _apply(self, action: Tuple[Any, ...]) -> None:
        kind = action[0]
        coordinator = self.coordinator
        if kind == "start":
            coordinator.start_encounter()
        elif kind == "wait":
            coordinator.tick(self.clock.now + float(action[1]))
        elif kind == "boss_damage":
            coordinator.apply_damage(self.config.boss.boss_id, float(action[1]), action[2])
        elif kind == "target_damage":
            coordinator.apply_damage(action[1], float(action[2]), action[3])
        elif kind == "destroy":
            coordinator.destroy_target(action[1], source=action[2])
        elif kind == "kill":
            coordinator.player_dies(action[1])
        elif kind == "interrupt":
            coordinator.interrupt_mechanic(action[1], action[2])
        elif kind == "inject_stale":
            if coordinator.generation < 1 or coordinator.status == "active":
                raise ValueError("stale injection only valid between rounds")
            self.clock.schedule(
                delay=9.0,
                generation=coordinator.generation,
                key=("phase_timer", "p1", "p1_channel"),
                callback=lambda handle: None,
            )
        else:
            raise ValueError(f"unsupported replay action {kind!r}")


def list_acceptance_files(output_dir: Optional[str] = None) -> Dict[str, List[str]]:
    base = os.path.abspath(output_dir or DEFAULT_REPLAY_DIR)
    result = {"scenarios": [], "failures": []}
    scenarios = os.path.join(base, "scenarios")
    failures = os.path.join(base, "failures")
    if os.path.isdir(scenarios):
        result["scenarios"] = sorted(
            f for f in os.listdir(scenarios) if f.endswith(".json")
        )
    if os.path.isdir(failures):
        result["failures"] = sorted(
            f for f in os.listdir(failures) if f.endswith(".json")
        )
    return result


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    session: ClientSession = None  # type: ignore[assignment]
    output_dir: str = DEFAULT_REPLAY_DIR

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: str) -> None:
        with open(path, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send_file(
                os.path.join(_STATIC_DIR, "index.html"), "text/html; charset=utf-8"
            )
        elif route == "/api/state":
            self._send_json(self.session.state())
        elif route == "/api/files":
            self._send_json(list_acceptance_files(self.output_dir))
        elif route == "/api/ledger":
            path = os.path.join(self.output_dir, "coverage_ledger.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    self._send_json(json.load(handle))
            else:
                self._send_json({"error": "coverage ledger not generated yet"}, 404)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, 400)
            return
        try:
            if route == "/api/action":
                self._send_json(self.session.act(action_from_dict(payload)))
            elif route == "/api/reset":
                self._send_json(self.session.reset())
            elif route == "/api/new":
                self._send_json(self.session.new_session())
            elif route == "/api/replay/open":
                path = self._resolve_replay_path(payload["file"])
                self._send_json(self.session.open_scenario(path))
            elif route == "/api/replay/step":
                self._send_json(self.session.replay_step())
            elif route == "/api/replay/run":
                self._send_json(self.session.replay_all())
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # surface coordinator errors to the page
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def _resolve_replay_path(self, filename: str) -> str:
        # Never accept absolute paths or traversal: only the two known folders.
        if os.path.basename(filename) != filename:
            raise ValueError("file must be a bare file name")
        for sub in ("failures", "scenarios"):
            candidate = os.path.join(self.output_dir, sub, filename)
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError(filename)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    output_dir: Optional[str] = None,
) -> Tuple[ThreadingHTTPServer, ClientSession]:
    session = ClientSession()
    directory = os.path.abspath(output_dir or DEFAULT_REPLAY_DIR)
    handler = type(
        "BoundHandler",
        (_Handler,),
        {"session": session, "output_dir": directory},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd, session
