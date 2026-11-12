"""Command-line helper for the boss encounter.

The CLI is auxiliary only -- the graphical client is the primary interactive
entry point.  It exposes four encounter-specific operations:

* ``acceptance``  run the full automated acceptance suite, write scenarios,
  failure replays and the coverage ledger under ``acceptance_output/``;
* ``scenarios``   list the generated scenarios without running them;
* ``replay``      replay one scenario/failure file and print the step trace;
* ``serve``       start the graphical client (a local browser UI);

Run ``python -m encounter.cli --help`` for the exact flags.
"""

from __future__ import annotations

import argparse
import json
from typing import List, Optional

from .acceptance.generator import generate_scenarios
from .acceptance.replay import DEFAULT_REPLAY_DIR, load_scenario
from .acceptance.runner import AcceptanceRunner
from .acceptance.suite import run_acceptance


def _cmd_acceptance(args: argparse.Namespace) -> int:
    outcome = run_acceptance(args.output)
    print(f"acceptance scenarios : {outcome.total}")
    print(f"passed               : {outcome.passed}")
    print(f"failed               : {outcome.failed}")
    if outcome.failure_ids:
        print("failure ids          : " + ", ".join(outcome.failure_ids))
    print("mutations caught     : " + json.dumps(outcome.mutations_caught, sort_keys=True))
    state_combos = outcome.coverage.get("state_combinations", {})
    print(
        "state combinations   : "
        f"{state_combos.get('exercised', 0)} exercised / "
        f"{state_combos.get('possible', 0)} feasible "
        f"({state_combos.get('uncovered_count', 0)} uncovered; see coverage_ledger.json)"
    )
    print(f"output directory     : {outcome.output_dir}")
    return 0 if outcome.ok else 1


def _cmd_scenarios(args: argparse.Namespace) -> int:
    for scenario in generate_scenarios():
        features = ",".join(scenario.features) or "-"
        print(
            f"{scenario.scenario_id:<48} {scenario.origin:<10} "
            f"{len(scenario.actions):>2} actions  [{features}]"
        )
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.file)
    result = AcceptanceRunner().check(scenario)
    print(f"scenario : {scenario.scenario_id} ({scenario.origin})")
    print(f"passed   : {result.passed}")
    for step in result.steps:
        marker = "!" if (step.snapshot_diffs or step.counter_diffs) else " "
        actual = step.actual_snapshot
        print(
            f"{marker} step {step.index:>2} {json.dumps(step.action, ensure_ascii=False)}"
        )
        print(
            f"    phase={actual['phase_id']} status={actual['status']} "
            f"gen={actual['generation']} occupancy={len(actual['occupancy'])} "
            f"active={actual['active_mechanics']}"
        )
        if step.snapshot_diffs:
            print(f"    snapshot diffs: {json.dumps(step.snapshot_diffs, ensure_ascii=False)}")
        if step.counter_diffs:
            print(f"    counter diffs : {json.dumps(step.counter_diffs, ensure_ascii=False)}")
    if result.property_violations:
        print("property violations:")
        for violation in result.property_violations:
            print("  " + json.dumps(violation, ensure_ascii=False))
    return 0 if result.passed else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    from .client.server import create_server

    httpd, _session = create_server(args.host, args.port, args.output)
    url = f"http://{args.host}:{args.port}/"
    print(f"graphical client for the boss encounter: {url}")
    print("press Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="encounter.cli",
        description="Auxiliary command line for the boss encounter coordinator.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_REPLAY_DIR,
        help="directory for generated scenarios, replays and the coverage ledger",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    acceptance = sub.add_parser("acceptance", help="run the full acceptance suite")
    acceptance.set_defaults(func=_cmd_acceptance)

    scenarios = sub.add_parser("scenarios", help="list generated scenarios")
    scenarios.set_defaults(func=_cmd_scenarios)

    replay = sub.add_parser("replay", help="replay a scenario or failure file")
    replay.add_argument("file", help="path to a scenario .json or .replay.json file")
    replay.set_defaults(func=_cmd_replay)

    serve = sub.add_parser("serve", help="start the graphical browser client")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
