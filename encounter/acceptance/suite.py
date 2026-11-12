"""Programmatic entry point for the full acceptance suite.

Running :func:`run_acceptance` generates every scenario, executes each one on
both the real coordinator and the independent reference model, writes all
scenarios and any failure replays to disk, and returns a structured summary.
The same function backs the command line helper and the unittest wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .generator import generate_scenarios, coverage_ledger
from .replay import DEFAULT_REPLAY_DIR, ReplayWriter
from .runner import AcceptanceRunner


@dataclass
class SuiteOutcome:
    total: int
    passed: int
    failed: int
    failure_ids: List[str] = field(default_factory=list)
    output_dir: str = ""
    replay_paths: List[str] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
    mutations_caught: Dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def run_acceptance(
    output_dir: Optional[str] = None,
    *,
    write_scenarios: bool = True,
    verify_mutations: bool = True,
) -> SuiteOutcome:
    scenarios = generate_scenarios()
    writer = ReplayWriter(output_dir or DEFAULT_REPLAY_DIR)

    if write_scenarios:
        writer.write_all_scenarios(scenarios)

    failure_ids: List[str] = []
    replay_paths: List[str] = []
    for scenario in scenarios:
        result = AcceptanceRunner().check(scenario)
        if not result.passed:
            failure_ids.append(scenario.scenario_id)
            replay_paths.append(writer.write_failure(scenario, result))

    mutations_caught: Dict[str, int] = {}
    if verify_mutations:
        mutations_caught = _verify_mutations(scenarios, writer)

    ledger = coverage_ledger(scenarios)
    coverage_dict = ledger.to_dict()
    writer.write_coverage(coverage_dict)

    outcome = SuiteOutcome(
        total=len(scenarios),
        passed=len(scenarios) - len(failure_ids),
        failed=len(failure_ids),
        failure_ids=failure_ids,
        output_dir=writer.directory,
        replay_paths=replay_paths,
        coverage=coverage_dict,
        mutations_caught=mutations_caught,
    )
    writer.write_summary(
        {
            "total": outcome.total,
            "passed": outcome.passed,
            "failed": outcome.failed,
            "failure_ids": outcome.failure_ids,
            "mutations_caught": outcome.mutations_caught,
            "output_dir": outcome.output_dir,
        }
    )
    return outcome


def _verify_mutations(scenarios: List[Any], writer: ReplayWriter) -> Dict[str, int]:
    """Confirm the suite fails on injected defects, then keep production clean.

    Each mutation runs against a fault-switching runner only; the production
    coordinator source is never modified.  A mutation that no scenario detects
    is itself a suite failure.
    """

    caught: Dict[str, int] = {}
    mutations = (
        ("double_dispatch", {"double_dispatch": True}),
        ("skip_cleanup", {"skip_cleanup": True}),
    )
    for name, switches in mutations:
        detected = 0
        for scenario in scenarios:
            result = AcceptanceRunner(**switches).check(scenario)
            if not result.passed:
                detected += 1
                writer.write_failure(scenario, result, mutation=name)
        if detected == 0:
            raise AssertionError(
                f"mutation {name!r} was not detected by any acceptance scenario"
            )
        caught[name] = detected
    return caught
