# ADR 0161 — Normalized commit authorship and release-summary fallback

## Status

Accepted.

## Decision

The commit-message JSON contract carries an unprefixed imperative `subject`.
Its structured `type`, `scope`, and `breaking` fields are the only source of
the Conventional Commit header rendered for delivery.

When the single `llm_generate` invocation returns JSON that cannot be parsed
or validated, delivery does not retry. It uses the existing `release_summary`
message fallback and records one bounded diagnostic in the existing
`meta.commit_delivery.delivery_warnings` list. The diagnostic includes the
rejection class and reason and explicitly says that the `release_summary`
fallback was used.

Delivery persistence merges this warning with existing branch and provider
warnings without duplicates. The existing terminal delivery-outcome renderer
shows every delivery warning; no new UI, SDK, or MCP field is introduced.

## Consequences

Operators can distinguish an authored commit message from a deliberate,
observable fallback while durable run metadata retains the reason. Release
verdict, delivery actions, publication policy, and the public wire shape are
unchanged.
