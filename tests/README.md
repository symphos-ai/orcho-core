# Tests

Unit tests are organized by **contract**, not by implementation history. The
path of a test answers: *what does this defend?* — not *when was it added?*

## Layout

```
tests/
  unit/
    agents/           # Protocols, registry, argv, attachments, stream parsing
    cli/              # CLI entry points (orcho, transcript, web delegate)
    core/             # Event store, config, metrics, logging, platform shims
    pipeline/
      lifecycle/      # LifecycleContext, PhaseLifecycle, checkpoint
      runtime/        # Loops, dispatch, retry, escalation, review, DAG
      profiles/       # Profile loader, execution validation, profile gates
      phases/         # Built-in phase handlers
      quality_gates/  # Gate dispatch, handlers, tests-config resolution
      skills/         # Discovery, loader, migration, prompt injection, types
      prompts/        # Composer, loader, boundary, contract templates
      evidence/       # Evidence bundle
      artifacts/      # Artifact mirror, artifact types
      cross_project/  # Cross-project types, subtask owned files / prompt
      plugins/        # Entry points, plugin loader
      orchestrator/   # Cross-cutting orchestrator regressions
  integration/        # Multi-component integration tests
  acceptance/         # End-to-end acceptance tests
  sdk/                # SDK-surface unit tests
  fixtures/           # Shared fixtures (helpers, golden files, normalizers)
```

`pytest tests/` discovers everything. Per-domain `conftest.py` files keep
shared helpers close to the tests that use them.

## Naming rule

Test paths describe **contract / behavior / component / failure mode** —
never the milestone that produced them.

**Banned** in file names, class names, and new docstrings:

- Milestone identifiers: `phase5`, `phase5e`, `phase7d`, `step1`…`step9`,
  `substep<N>`, `milestone<N>`, `REA-<N>`, ticket numbers as filenames.

**Allowed** (and encouraged):

- Contract: `test_dispatch.py`, `test_loader.py`.
- Behavior: `test_resume_blocks_on_qa.py`,
  `test_invalid_profile_rejected.py`.
- Component: `test_pipeline_runtime.py`, `test_quality_gates.py`.
- Failure mode: `test_unknown_phase_at_top_level_caught`.

### "Phase" as a domain noun is fine

The pipeline has phases (`plan`, `build`, `review`, `fix`, `final_qa`,
`compliance_check`). Tests named `test_builtin_phases.py` or class names
like `TestPhaseStep`, `TestPhaseRegistry`, `TestPhaseFixV2Escalation` use
"phase" as a **domain noun**, not as a milestone identifier. Those stay.
The cleanup target is the milestone form (`Phase 5e step 3:`,
`# Phase 6:`, `class TestPhase5cStep4Escalation`).

Likewise, runtime phase labels in test data — `"PLAN"`, `"VALIDATE_PLAN"`,
`log_phase("IMPLEMENT", ...)` — are domain data, not milestones, and stay.

### Where milestone context belongs

Historical context (ADRs, design decisions, milestone summaries) belongs in
`docs/` and commit messages. Don't carry it into test docstrings — it rots
as the codebase evolves, and new contributors can't read it without the
backstory.
