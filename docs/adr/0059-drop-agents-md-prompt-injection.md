# ADR 0059 — Drop AGENTS.md prompt injection; rely on native runtime discovery

- **Status:** Accepted
- **Date:** 2026-05-30
- **Deciders:** project owner
- **Relates to:** [ADR 0009](0009-composable-prompt-parts.md),
  [ADR 0028](0028-cache-first-physical-wire-layout.md)

## Context

Every agent prompt — `plan` / `replan` / `implement` / `validate_plan` /
`review_changes` / `repair_changes` / `final_acceptance` — prepended the
project's `AGENTS.md` content as a `PROJECT RULES (from AGENTS.md):` block.
The path: `pipeline/agents_md.py` discovered and read the file,
`PluginConfig.agents_md` carried it, `_agents_md_prefix` wrapped it, and each
phase handler spliced it ahead of the phase prompt (folded into the reviewer
`turn_input` focus for the review/acceptance gates).

The injection was unconditional and runtime-blind. But the only runtimes
Orcho ships — `claude` (Claude Code CLI) and `codex` (Codex CLI) — already
discover their own project-instruction file natively from the worktree `cwd`
(Codex reads `AGENTS.md`; Claude Code reads `CLAUDE.md`). The agent runs in
the isolated worktree checkout, where the tracked instruction file is present.
So for the common case (a native CLI runtime with the file at the repo root)
Orcho was re-pasting a document the agent already loads — a second, *worse-
placed* copy: the native copy lands in the harness's cached system slot, while
Orcho's copy landed in the per-turn, uncached, `decision_bearing` `turn_input`
(see ADR 0028 wire layout / `output_class.py`). Net effect: duplicated tokens
and a diluted reviewer focus, re-sent every round.

A capability-gated alternative was considered (a runtime declares which
project-rules filenames it discovers natively, and core injects only as a
fallback). It was rejected as premature: with three native CLI runtimes for
the foreseeable future and no generic/API runner shipping, the abstraction
would be pure backcompat ceremony (cf. orcho-core "no backcompat ceremony"
rule). Direct removal is the honest change.

## Decision

Remove prompt-side `AGENTS.md` injection entirely. Project rules reach the
agent through its native discovery, not through Orcho's prompt composer.

Removed:

- `pipeline/agents_md.py` (`load_agents_md`, `inject_agents_md`,
  `AGENTS_MD_FILENAMES`) and its `load_plugin` population sites.
- `PluginConfig.agents_md` field. This was a framework-populated field, not a
  value plugin authors set; dropping it is a deliberate, documented contract
  removal, not a silent break.
- `_agents_md_prefix` and the per-phase prepend logic.
- The now-dead `prompt_prefix` parameter on `run_build`, `run_review`,
  `run_fix`, and `_review_plan_artifact` (it carried only AGENTS.md on those
  surfaces).

Kept:

- The **TEXT-attachment** path is independent of AGENTS.md and survives intact.
  `_plan_prompt_prefix` still renders Phase 4.5 attachments; `run_plan` keeps
  its `prompt_prefix`; the `text_prefix` PromptPart kind and `_text_prefix_part`
  helper remain (now honestly labelled attachments-only:
  `id="text_prefix:attachments:…"`, `name="attachments"`).
- The `text_prefix` entry in `output_class.PROMPT_PART_CLASS_RULES` stays —
  the kind is still produced by attachments and is reserved for future
  tool-result prefixes.

## Scope / non-goals

- No wire-type, profile, or evidence-shape change. The reviewer JSON / release
  JSON contracts are untouched; MCP/web consumers are unaffected. The matching
  `orcho-mcp` mock smoke is run per the cross-repo wire-format rule.
- Does not change attachment behavior, codemap injection, or hypothesis-suffix
  injection — only the AGENTS.md leg is removed.
- `.orcho/multiagent/AGENTS.md` (the non-root fallback discovery location) is
  removed with the loader. A project that kept rules there, off the path a
  native runtime scans, must move them to the runtime's conventional location
  (`AGENTS.md` / `CLAUDE.md` at the worktree root). Acceptable given the
  single-developer, no-install-base reality.

## Consequences

- Prompts shrink by the size of `AGENTS.md` on every phase; the reviewer focus
  is no longer diluted by project rules the agent already holds.
- Project rules now depend on the runtime's native discovery. A future
  generic/API runtime with no native discovery would need rules delivered
  explicitly again — at which point the capability-gated design rejected here
  becomes the right reintroduction, not unconditional injection.
