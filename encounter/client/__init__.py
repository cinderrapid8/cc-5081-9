"""Graphical client package: a browser UI driven by the real coordinator.

The client holds no hard-coded boss phase or occupancy data.  Every button is
a thin HTTP wrapper around :class:`~encounter.coordinator.PhaseCoordinator`
methods, and every rendered value comes from a snapshot taken off the live
coordinator (or from an acceptance replay file stepped through the same
coordinator code path).
"""

from .server import ClientSession, create_server

__all__ = ["ClientSession", "create_server"]
