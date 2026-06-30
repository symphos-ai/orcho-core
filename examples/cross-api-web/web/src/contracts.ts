// Frontend field contract for the demo admin UI.

export const USER_FIELDS = Object.freeze(["name", "email_address"]);
export const TEAM_FIELDS = Object.freeze(["name", "owner_id"]);
export const PROJECT_FIELDS = Object.freeze([
  "name",
  "team_id",
  "status"
]);

export function formatUserLabel(payload) {
  return `${payload.name} <${payload.email_address}>`;
}

export function renderTeamCard(payload) {
  return `${payload.name}  (id=${payload.team_id}, owner=${payload.owner_id})`;
}

export function renderProjectSummary(payload) {
  return `[${payload.status}] ${payload.name} - team ${payload.team_id}`;
}
