---
name: orcho-core-test-infra-goldens
description: "Use when editing orcho-core shared test infrastructure: tests/conftest.py, tests/fixtures/*, golden snapshots, snapshot normalizers, pytest config, collection hooks, acceptance fixtures, schema/golden regeneration, or broad fixture behavior. High blast radius; inspect consumers before editing."
---

# Orcho Core Test Infra Goldens

Own shared test infrastructure and golden/snapshot assets with high blast
radius.

## First Reads

- `orcho-core/AGENTS.md`
- `/Users/smartgamma/www/orcho/DEVELOPMENT_PIPELINE.md`
- `orcho-core/tests/conftest.py`
- `orcho-core/tests/fixtures/`
- relevant consumer tests found with `rg`

## Owns

- shared pytest fixtures and collection hooks
- golden snapshots and normalizers
- acceptance fixtures
- schema/golden regeneration workflow
- high-blast-radius test support code

## Does Not Own

- domain behavior under test -> relevant domain specialist
- final verification gate -> `orcho-integrity-pipeline`
- targeted domain test selection -> `orcho-core-verification-matrix`

## Invariants

- Inspect consumers with `rg` before changing shared test infrastructure.
- Run affected consumer slices, not only the helper's own tests.
- Do not normalize away meaningful contract differences.
- Review golden diffs before accepting them.

## Verification

- From `orcho-core`: use `rg` to list consumers of changed fixture/helper.
- From `orcho-core`: run targeted consumer tests plus the fixture/golden tests.
- From `orcho-core`: run schema snapshot tests when schema/golden shape changes.

## Neighbor Skills

- domain specialist for the behavior under test
- `orcho-core-verification-matrix` for targeted test map
- `orcho-integrity-pipeline` before commit-ready claims
