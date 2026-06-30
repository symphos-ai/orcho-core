# Terminal IO Instructions

## Scope

This file applies to `orcho-core/core/io/`.

Also obey the repository-level `../../AGENTS.md`.

## Terminal Color Discipline

Production renderers route every ANSI insertion through
`core.io.ansi.paint()` / `strip_ansi()`. Prompts route through
`core.io.journey_prompt` helpers. Raw `\033[…m` and inline
`f"{C.X}…{C.RESET}"` patterns belong only in `core/io/ansi.py`, the
palette/policy module. Tests may assert on raw ANSI codes when verifying the
colored path; the discipline applies to production code, not test fixtures.

Stderr-bound renderers must pass `stream=sys.stderr` to `paint()`.
Auto-detect defaults to `sys.stdout`, so without the explicit stream an
`orcho run > out.log` invocation with stderr attached to a TTY and stdout
piped would suppress color on stderr, while the mirror case can leak ANSI into
a stdout file. The explicit `stream=` argument is the guard.

Process-level overrides such as `set_color_enabled(True/False)` are CLI-startup
wiring only. Do not set them inside a renderer or request handler. `--no-color`
and any future force-color flag route through this one knob; renderers stay
side-effect-free. The enforcement test for this rule lands at the end of the
T3 migration; until then reviewers apply it manually.
