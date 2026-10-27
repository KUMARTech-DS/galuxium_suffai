"""Who may do what in the retail suite.

Every request runs under a Scope: the brand it may touch and, for staff logins, the one location they
work at. Brand logins (brand HQ) manage everything of their brand; SuffAI admins act on a brand they
name; warehouse and store staff read their brand's catalog and stock and write only at their location.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from retail_db import RetailError

STAFF_ROLES = frozenset({"warehouse_staff", "store_staff"})
RETAIL_ROLES = frozenset({"brand", "admin"}) | STAFF_ROLES
_HQ = frozenset({"brand", "admin"})

PERMISSIONS: dict[str, frozenset] = {
    "read": RETAIL_ROLES,
    "catalog.write": _HQ,
    "settings.write": _HQ,
    "locations.write": _HQ,
    "staff.manage": _HQ,
    "par.write": _HQ,
    "plan.write": _HQ,
    "purchase.write": _HQ,
    "purchase.receive": _HQ | {"warehouse_staff"},
    "stock.write": RETAIL_ROLES,
    "transfer.write": _HQ | {"warehouse_staff"},
    "transfer.receive": RETAIL_ROLES,
    "replenish": _HQ | {"warehouse_staff"},
    "pos.sell": _HQ | {"store_staff"},
    "pos.read": _HQ | {"store_staff"},
}

_DENIED = {
    "warehouse_staff": "Warehouse logins can't do that. Ask your brand admin.",
    "store_staff": "Store logins can't do that. Ask your brand admin.",
}


@dataclass(frozen=True)
class Scope:
    role: str
    username: str
    brand_key: str
    brand_name: str
    location_id: Optional[str] = None
    location_code: Optional[str] = None
    location_kind: Optional[str] = None

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES

    @property
    def actor(self) -> str:
        return self.username or "?"

    def require(self, action: str) -> None:
        if self.role not in PERMISSIONS[action]:
            raise RetailError(_DENIED.get(self.role, "Your login can't do that."), 403)

    def check_location(self, location_id: Optional[str]) -> None:
        """Staff write only at their own location."""
        if self.is_staff and location_id != self.location_id:
            raise RetailError("You can only do this at your own location.", 403)

    def default_location(self, location_id: Optional[str]) -> Optional[str]:
        """Staff always get their own location (asking for another one is refused); HQ gets what it asked for."""
        if not self.is_staff:
            return location_id or None
        if location_id and location_id != self.location_id:
            raise RetailError("You can only see your own location.", 403)
        return self.location_id
