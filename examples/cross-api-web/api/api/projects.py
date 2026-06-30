"""Project payload producer for the demo API."""
from __future__ import annotations

PROJECT_FIELDS: tuple[str, ...] = (
    "project_id", "name", "team_id", "status",
)


def build_project_payload(
    project_id: str, name: str, team_id: str, status: str = "active",
) -> dict[str, str]:
    """Payload the API service emits when a project is created."""
    return {
        "project_id": project_id,
        "name":       name,
        "team_id":    team_id,
        "status":     status,
    }
