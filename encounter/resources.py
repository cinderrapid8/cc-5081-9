"""Resource occupancy: shared objects/positions and mutually exclusive groups.

Mechanics contend for two kinds of claims:

* concrete resources such as ``object:relic`` or ``position:north_dais``;
* abstract mutex groups such as ``group:airstrike``, which only one mechanic
  may hold at a time even if they touch different resources.

The manager is deliberately synchronous and single-threaded: a failed claim
changes nothing, which makes the coordinator's contention policy easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Claim:
    resource: str
    owner: str


class ResourceBusy(Exception):
    """Raised on a hard acquire; the coordinator normally uses ``try_claim``."""

    def __init__(self, resource: str, owner: str):
        super().__init__(f"{resource!r} already held by {owner!r}")
        self.resource = resource
        self.owner = owner


class ResourceManager:
    def __init__(self) -> None:
        self._holders: Dict[str, str] = {}
        self._mutex: Dict[str, str] = {}

    def try_claim(
        self, owner: str, resources: List[str], mutex_group: Optional[str]
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Attempt every claim atomically.

        Returns ``(ok, contended_resource, current_holder)``.  On failure no
        partial claims are kept, so callers never need rollback logic.
        """

        wanted = list(resources)
        if mutex_group is not None:
            wanted.append(f"group:{mutex_group}")
        for resource in wanted:
            if resource in self._holders or resource in self._mutex:
                holder = self._holders.get(resource) or self._mutex.get(resource)
                return False, resource, holder
        for resource in resources:
            self._holders[resource] = owner
        if mutex_group is not None:
            self._mutex[f"group:{mutex_group}"] = owner
        return True, None, None

    def release(self, owner: str, resources: List[str], mutex_group: Optional[str]) -> None:
        for resource in resources:
            if self._holders.get(resource) == owner:
                del self._holders[resource]
        if mutex_group is not None:
            key = f"group:{mutex_group}"
            if self._mutex.get(key) == owner:
                del self._mutex[key]

    def holder_of(self, resource: str) -> Optional[str]:
        return self._holders.get(resource)

    def active_claims(self) -> List[Claim]:
        claims = [Claim(resource=r, owner=o) for r, o in sorted(self._holders.items())]
        claims.extend(
            Claim(resource=g, owner=o) for g, o in sorted(self._mutex.items())
        )
        return claims

    def is_empty(self) -> bool:
        return not self._holders and not self._mutex

    def reset(self) -> None:
        self._holders.clear()
        self._mutex.clear()
