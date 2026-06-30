"""Team payload producer for the demo API."""
from __future__ import annotations

TEAM_FIELDS: tuple[str, ...] = ("team_id", "name", "owner_id")


def build_team_payload(
    team_id: str, name: str, owner_id: str,
) -> dict[str, str]:
    """Payload the API service emits when a team is created."""
    return {
        "team_id":  team_id,
        "name":     name,
        "owner_id": owner_id,
    }
