"""Automated acceptance suite for the phase coordinator.

The package treats three coordinator guarantees as independently checkable
properties:

* ``phase``      -- phase eligibility: phases are entered only when every entry
                    condition holds, and mechanics dispatch only inside their
                    owning phase;
* ``exclusivity`` -- mechanic exclusivity: two committed mechanics never share a
                    resource or mutex group, and contention is blocked rather
                    than queued;
* ``reset``      -- reset cleanup: a wipe/victory leaves no claim, active
                    mechanic or live timer behind, and callbacks from a dead
                    encounter generation are provably rejected.

Short event sequences are enumerated by :mod:`encounter.acceptance.generator`
and fed both to the real :class:`~encounter.coordinator.PhaseCoordinator` (via
:mod:`encounter.acceptance.runner`) and to the independent
:class:`~encounter.acceptance.oracle.ReferenceCoordinator`.  Phase trajectory,
occupancy sets and actual dispatch counts are compared step by step; any
mismatch is materialized as a replay file that the graphical client can open.
"""

from .actions import Scenario, action_from_dict, action_to_dict
from .generator import generate_scenarios, coverage_ledger
from .oracle import ReferenceCoordinator
from .replay import ReplayWriter, load_replay
from .runner import AcceptanceRunner, CheckResult
from .properties import PropertyFailure

__all__ = [
    "Scenario",
    "action_from_dict",
    "action_to_dict",
    "generate_scenarios",
    "coverage_ledger",
    "ReferenceCoordinator",
    "ReplayWriter",
    "load_replay",
    "AcceptanceRunner",
    "CheckResult",
    "PropertyFailure",
]
