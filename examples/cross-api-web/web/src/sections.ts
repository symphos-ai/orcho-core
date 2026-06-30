import { PROJECT_FIELDS, TEAM_FIELDS, USER_FIELDS } from "./contracts.ts";

export const SECTIONS = [
  {
    slug: "users",
    label: "Users",
    title: "Add a user",
    blurb: "Provision a new user record.",
    submitLabel: "Create user",
    fields: [
      ["name", "Full name", "text", "Alice Doe", "Alice Doe"],
      [
        "email_address",
        "Email address",
        "email",
        "alice@example.com",
        "alice@example.com"
      ]
    ],
    contractFields: USER_FIELDS,
    displayCols: [
      ["user_id", "ID"],
      ["name", "Name"],
      ["email", "Email"],
      ["created_at", "Created"]
    ]
  },
  {
    slug: "teams",
    label: "Teams",
    title: "Create a team",
    blurb: "Spin up a new team. Members are added separately.",
    submitLabel: "Create team",
    fields: [
      ["name", "Name", "text", "Platform", "Platform"],
      ["owner_id", "Owner user ID", "user_lookup", "", "u-1001"]
    ],
    contractFields: TEAM_FIELDS,
    displayCols: [
      ["team_id", "ID"],
      ["name", "Name"],
      ["owner_id", "Owner"],
      ["created_at", "Created"]
    ]
  },
  {
    slug: "projects",
    label: "Projects",
    title: "Create a project",
    blurb: "Spin up a new project under an existing team.",
    submitLabel: "Create project",
    fields: [
      ["name", "Name", "text", "Orcho", "Orcho"],
      ["team_id", "Team ID", "team_lookup", "", "t-1"],
      ["status", "Status", "status_select", "", "active"]
    ],
    contractFields: PROJECT_FIELDS,
    displayCols: [
      ["project_id", "ID"],
      ["name", "Name"],
      ["team_id", "Team"],
      ["status", "Status"],
      ["created_at", "Created"]
    ]
  }
];

export function sectionBySlug(slug) {
  return SECTIONS.find((section) => section.slug === slug) || SECTIONS[0];
}
