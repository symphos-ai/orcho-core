# ADR 0137 — Local skill scope default and Codex-native parity

Status: Accepted

## Context

Orcho discovers skills from project, workspace, user, compatibility, and
entry-point package roots. The original `SkillTrustPolicy` defaults enabled
workspace, user, and package sources. A project that opted into its own skills
with `SkillTrustPolicy(trust_project=True)` therefore added project skills to
the global catalogue instead of narrowing discovery to the project.

This became visible in a backend dogfood run: 49 unrelated marketing skills
from `$HOME/.agents/skills` entered the Orcho registry, although no subtask used
one. The Codex CLI independently scanned the same user root and shortened skill
descriptions after its initial skill roster reached the provider's context
budget. Orcho roster filtering alone would not remove that provider-native
context.

Changing `HOME` or `CODEX_HOME` is not an acceptable fix. Those locations also
own user configuration, authentication, and resumable session state. A skill
scope decision must not create a second provider identity or break session
continuation.

## Decision

### 1. Default Orcho discovery is workspace-local

`SkillTrustPolicy()` now enables only the workspace source:

| Source | Default |
|---|---|
| workspace `.agents/skills` | enabled |
| project `.agents/skills` | disabled; explicit trust required |
| user `$HOME/.agents/skills` | disabled; explicit opt-in required |
| `orcho.skills` entry-point packages | disabled; explicit opt-in required |
| project Claude/Forge compatibility roots | disabled; explicit trust required |

Project trust remains a separate security decision because a cloned repository
can contain executable instructions. A trusted project can declare
`SkillTrustPolicy(trust_project=True)` without implicitly enabling user or
package catalogues. Operators and plugin authors can still opt into global
sources with `trust_user=True` and/or `trust_packages=True`.

### 2. Runtime-native discovery follows the effective user scope

Provider adapters own provider-specific discovery controls. For Codex, an
Orcho-created runtime defaults to user skills disabled. The adapter enumerates
direct `$HOME/.agents/skills/<name>/SKILL.md` entries and passes Codex's
documented per-skill `skills.config` overrides with `enabled=false`.

When the effective Orcho policy declares `trust_user=True`, the adapter emits no
disable overrides and Codex retains its native user catalogue.

The adapter does not replace `HOME`, `CODEX_HOME`, the user's config file, or
session storage. Project, workspace/repository, system, and provider-bundled
skills remain available through their native roots. Other provider adapters
may project the same source decision through their own supported controls; core
does not invent a provider-neutral command-line flag.

### 3. Phase and subtask runtimes receive the same policy

Project runtime setup configures every distinct phase agent and wraps subtask
runtime factories with the same effective `SkillTrustPolicy`. A planner and a
later DAG subtask therefore cannot silently see different user catalogues.
Runtimes without a native skill-scope capability are left unchanged.

## Consequences

- Unrelated globally installed skills no longer consume Orcho planning context
  or Codex's initial skill roster by default.
- A project plugin's `trust_project=True` now means project plus workspace, not
  project plus every global catalogue.
- Existing plugins that intentionally relied on user/package discovery must opt
  in explicitly.
- Orcho retains one provider identity and one resumable session store.
- Provider-native parity is explicit adapter behavior rather than a `HOME`
  isolation side effect.

## Verification

- Skill discovery tests pin workspace-only defaults and explicit global opt-in.
- Codex scope tests pin deterministic `skills.config` overrides and user opt-in.
- Runtime-scope tests pin identical policy for phase and subtask agents.
- A local `codex debug prompt-input` probe confirmed that the effective default
  removes all global user-skill mentions from the provider-visible roster while
  repository skills remain present.
