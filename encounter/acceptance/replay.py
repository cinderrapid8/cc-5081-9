"""Failure replay files and openable scenario records.

A replay is a self-contained JSON document: the full action sequence, the
expected vs. actual snapshot at every step, the dispatch-count diffs, the named
property that failed and the raw production events around the divergence.  The
graphical client opens these files and steps through them so a failed run can be
inspected at the exact mechanic position where the model and the coordinator
parted ways.

The same on-disk shape is used for ordinary generated scenarios, so any
acceptance input -- not only failures -- is openable in the client.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .actions import Scenario
from .properties import PropertyFailure, classify_step_failure
from .runner import CheckResult

DEFAULT_REPLAY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "..", "acceptance_output"
)


def _canonical_dir(directory: Optional[str]) -> str:
    return os.path.abspath(directory or DEFAULT_REPLAY_DIR)


class ReplayWriter:
    def __init__(self, directory: Optional[str] = None) -> None:
        self.directory = _canonical_dir(directory)
        self.scenario_dir = os.path.join(self.directory, "scenarios")
        self.failure_dir = os.path.join(self.directory, "failures")
        os.makedirs(self.scenario_dir, exist_ok=True)
        os.makedirs(self.failure_dir, exist_ok=True)

    # ------------------------------------------------------------------ #

    def write_scenario(self, scenario: Scenario) -> str:
        path = os.path.join(self.scenario_dir, f"{scenario.scenario_id}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(scenario.to_dict(), handle, indent=2, ensure_ascii=False)
        return path

    def write_all_scenarios(self, scenarios: List[Scenario]) -> List[str]:
        return [self.write_scenario(scenario) for scenario in scenarios]

    def write_failure(
        self,
        scenario: Scenario,
        result: CheckResult,
        *,
        mutation: Optional[str] = None,
    ) -> str:
        failures: List[Dict[str, Any]] = [
            self._failure_dict(failure)
            for failure in self._collect_failures(scenario, result)
        ]
        for violation in result.property_violations:
            failures.append(
                {
                    "property": violation.get("property", "reset"),
                    "scenario_id": scenario.scenario_id,
                    "step": violation.get("step"),
                    "detail": violation.get("detail", "property violation"),
                    "evidence": {
                        key: value
                        for key, value in violation.items()
                        if key not in ("property", "step", "detail")
                    },
                }
            )
        document = {
            "schema": "encounter-replay/1",
            "scenario": scenario.to_dict(),
            "mutation": mutation,
            "passed": result.passed,
            "reason": result.reason,
            "property_failures": failures,
            "expected_counters": result.expected_counters,
            "actual_counters": result.actual_counters,
            "steps": [self._step_dict(step) for step in result.steps],
        }
        suffix = f"_{mutation}" if mutation else ""
        path = os.path.join(
            self.failure_dir, f"{scenario.scenario_id}{suffix}.replay.json"
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
        return path

    def write_coverage(self, ledger_dict: Dict[str, Any]) -> str:
        path = os.path.join(self.directory, "coverage_ledger.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(ledger_dict, handle, indent=2, ensure_ascii=False)
        return path

    def write_summary(self, summary: Dict[str, Any]) -> str:
        path = os.path.join(self.directory, "acceptance_summary.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, ensure_ascii=False)
        return path

    # ------------------------------------------------------------------ #

    def _collect_failures(
        self, scenario: Scenario, result: CheckResult
    ) -> List[PropertyFailure]:
        failures: List[PropertyFailure] = []
        for step in result.steps:
            if step.snapshot_diffs or step.counter_diffs:
                failures.extend(classify_step_failure(scenario.scenario_id, step.index, step))
        return failures

    @staticmethod
    def _failure_dict(failure: PropertyFailure) -> Dict[str, Any]:
        return {
            "property": failure.property,
            "scenario_id": failure.scenario_id,
            "step": failure.step,
            "detail": failure.detail,
            "evidence": failure.evidence,
        }

    @staticmethod
    def _step_dict(step: Any) -> Dict[str, Any]:
        return {
            "index": step.index,
            "action": step.action,
            "time": step.time,
            "snapshot_diffs": step.snapshot_diffs,
            "counter_diffs": step.counter_diffs,
            "expected_snapshot": step.expected_snapshot,
            "actual_snapshot": step.actual_snapshot,
            "events": step.events,
        }


def load_replay(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if document.get("schema") != "encounter-replay/1":
        raise ValueError(f"{path!r} is not an encounter replay document")
    return document


def load_scenario(path: str) -> Scenario:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if "actions" not in data:
        # A replay document embeds its scenario under a nested key.
        if data.get("schema") == "encounter-replay/1":
            data = data["scenario"]
        else:
            raise ValueError(f"{path!r} is neither a scenario nor a replay")
    return Scenario.from_dict(data)
