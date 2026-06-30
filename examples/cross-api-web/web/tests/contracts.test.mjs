import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const contracts = readFileSync(new URL("../src/contracts.ts", import.meta.url), "utf8");
const main = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
const sections = readFileSync(new URL("../src/sections.ts", import.meta.url), "utf8");

function asBrowserScript(source) {
  return source
    .replace(/import[\s\S]*?from\s+["'][^"']+["'];?\n/g, "")
    .replace(/^export\s+/gm, "");
}

test("user consumer advertises email address field", () => {
  assert.match(
    contracts,
    /USER_FIELDS[\s\S]*"name"[\s\S]*"email_address"/
  );
});

test("team and project consumer fields stay aligned", () => {
  assert.match(contracts, /TEAM_FIELDS[\s\S]*"name"[\s\S]*"owner_id"/);
  assert.match(contracts, /PROJECT_FIELDS[\s\S]*"name"[\s\S]*"team_id"[\s\S]*"status"/);
});

test("sections bind form fields to contract constants", () => {
  assert.match(sections, /contractFields:\s*USER_FIELDS/);
  assert.match(sections, /contractFields:\s*TEAM_FIELDS/);
  assert.match(sections, /contractFields:\s*PROJECT_FIELDS/);
  assert.doesNotMatch(sections, /"user_id"[\s\S]*"User ID"/);
  assert.doesNotMatch(sections, /"team_id"[\s\S]*"Team ID"[\s\S]*"t-12"[\s\S]*"t-12"/);
  assert.doesNotMatch(sections, /"project_id"[\s\S]*"Project ID"/);
  assert.match(sections, /"email_address"[\s\S]*"Email address"/);
  assert.match(sections, /"owner_id"[\s\S]*"Owner user ID"[\s\S]*"user_lookup"/);
  assert.match(sections, /"team_id"[\s\S]*"Team ID"[\s\S]*"team_lookup"/);
  assert.match(sections, /"status"[\s\S]*"Status"[\s\S]*"status_select"/);
});

test("lookup fields render as selects from loaded rows", () => {
  assert.match(main, /isSelectField\(type\)/);
  assert.match(main, /state\.rows\.users/);
  assert.match(main, /state\.rows\.teams/);
  assert.match(main, /active[\s\S]*paused[\s\S]*archived/);
  assert.match(main, /:value="option\.value"/);
});

test("browser-served source modules stay parseable as JavaScript", () => {
  for (const source of [contracts, main, sections]) {
    assert.doesNotThrow(() => new Function(asBrowserScript(source)));
  }
});
