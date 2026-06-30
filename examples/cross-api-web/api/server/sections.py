"""Typed section registry for the demo admin tool.

Three sections (users / teams / projects) are declared as immutable
dataclasses. Each section points to a producer module by name so the
wire layer can hot-reload source edits.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FormField:
    name: str
    label: str
    type: str
    placeholder: str
    default: str


@dataclass(frozen=True)
class ProducerRef:
    """Locator for a producer callable inside the demo api package.

    The wire layer re-imports ``module`` on every call so edits land
    without a server restart.
    """

    module: str
    func: str
    arg_names: Sequence[str]


@dataclass(frozen=True)
class UpdateSpec:
    """Optional section capability — declare to enable PUT /api/<slug>/<id>.

    No demo section ships an :class:`UpdateSpec` today; the type is the
    extension point a future section would fill in. ``payload_aliases``
    bridges a producer that emits a different wire key than the storage
    column name.
    """

    producer: ProducerRef
    columns: Sequence[str]
    payload_aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Section:
    slug: str
    label: str
    title: str
    blurb: str
    table: str
    pk_col: str
    columns: Sequence[str]
    fields: Sequence[FormField]
    display_cols: Sequence[tuple[str, str]]
    submit_label: str
    create: ProducerRef
    update: UpdateSpec | None = None


SECTIONS: list[Section] = [
    Section(
        slug="users",
        label="Users",
        title="Add a user",
        blurb="Provision a new user record.",
        table="users",
        pk_col="user_id",
        columns=("user_id", "name", "email"),
        fields=(
            FormField("name", "Full name", "text", "Alice Doe", "Alice Doe"),
            FormField(
                "email", "Email address", "email",
                "alice@example.com", "alice@example.com",
            ),
        ),
        display_cols=(
            ("user_id", "ID"),
            ("name", "Name"),
            ("email", "Email"),
            ("created_at", "Created"),
        ),
        submit_label="Create user",
        create=ProducerRef(
            "api.payload", "build_user_payload",
            ("user_id", "name", "email"),
        ),
    ),
    Section(
        slug="teams",
        label="Teams",
        title="Create a team",
        blurb="Spin up a new team. Members are added separately.",
        table="teams",
        pk_col="team_id",
        columns=("team_id", "name", "owner_id"),
        fields=(
            FormField("name", "Name", "text", "Platform", "Platform"),
            FormField(
                "owner_id", "Owner user ID", "user_lookup", "", "u-1001",
            ),
        ),
        display_cols=(
            ("team_id", "ID"),
            ("name", "Name"),
            ("owner_id", "Owner"),
            ("created_at", "Created"),
        ),
        submit_label="Create team",
        create=ProducerRef(
            "api.teams", "build_team_payload",
            ("team_id", "name", "owner_id"),
        ),
    ),
    Section(
        slug="projects",
        label="Projects",
        title="Create a project",
        blurb="Spin up a new project under an existing team.",
        table="projects",
        pk_col="project_id",
        columns=("project_id", "name", "team_id", "status"),
        fields=(
            FormField("name", "Name", "text", "Orcho", "Orcho"),
            FormField("team_id", "Team ID", "team_lookup", "", "t-1"),
            FormField("status", "Status", "status_select", "", "active"),
        ),
        display_cols=(
            ("project_id", "ID"),
            ("name", "Name"),
            ("team_id", "Team"),
            ("status", "Status"),
            ("created_at", "Created"),
        ),
        submit_label="Create project",
        create=ProducerRef(
            "api.projects", "build_project_payload",
            ("project_id", "name", "team_id", "status"),
        ),
    ),
]

BY_SLUG: dict[str, Section] = {s.slug: s for s in SECTIONS}
