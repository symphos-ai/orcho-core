# Scheduled verification

Scheduled verification is the public way to turn project-native commands into
readiness evidence. A plugin declares the facts; Orcho selects and schedules
them, executes eligible gates, and writes immutable receipts.

Author a contract in this order: inspection → environments → commands → cost →
`gate_sets` → selection → schedule → policy/action → `orcho quality-gates` →
receipt/readiness.

## 1. Inspect the project first

Before declaring a gate, inspect the repository's root instructions, manifests,
package scripts, CI workflows, and developer documentation. Record the
authoritative command, its working directory, required services or credentials,
and whether it is safe in an isolated checkout. Do not infer these facts from a
project's language or copy a broad suite into an implementation task.

## 2. Declare environments and commands

Use `verification_envs` for the command contexts the project actually needs.
Then declare each project-native command once under `verification.commands`;
the engine, not a plan, owns its official scheduled execution.

## A complete small contract

```python
PLUGIN = {
    "verification_envs": {"project": {"python": "python"}},
    "work_mode": "pro",
    "verification": {
        "default_env": "project",
        "commands": {
            "lint": {"run": ["python", "-m", "ruff", "check", "."], "cost": "fast"},
            "unit": {"run": ["python", "-m", "pytest", "-q", "tests/unit"], "cost": "fast"},
            "functional": {
                "run": ["python", "-m", "pytest", "-q", "tests/functional"],
                "cost": "slow",
            },
            "e2e": {"run": ["python", "-m", "pytest", "-q", "tests/e2e"], "cost": "unknown"},
        },
        "gate_sets": {
            "hygiene": {"commands": ["lint", "unit"], "default_policy": "require"},
            "broad": {"commands": ["functional"], "default_policy": "require"},
            "manual-proof": {"commands": ["e2e"]},
        },
        "selection": [{"always": ["hygiene", "broad", "manual-proof"]}],
        "schedule": [
            {"after_phase": "implement", "gate_sets": ["hygiene"], "action": "repair_loop"},
            {"after_phase": "implement", "gate_sets": ["broad"], "action": "repair_loop"},
            {"manual_only": True, "gate_sets": ["manual-proof"], "policy": "suggest"},
        ],
    },
}
```

Note what the costs say here, because the naming tempts the opposite. A unit
suite is `fast`: it is bounded, hermetic, and its whole purpose is quick
feedback — do not classify it as `slow` because the word "tests" sounds
expensive. Reserve `slow` for the broad, service-heavy proof (functional,
integration, a full cross-layer suite), and `unknown` for anything networked,
credential-dependent, or otherwise unpredictable — an end-to-end suite usually
belongs there, and it is normally operator-owned rather than engine-executed.

Cost and ownership are separate axes: `e2e` above is `unknown` **and**
`manual_only` with a `suggest` policy — "we cannot predict what this costs, and
a person decides when it runs" — not "it is expensive, therefore optional".

Cost is descriptive, not a budget: labelling a command `slow` gives it no extra
wall-clock. Every command runs under a ceiling (600s by default) and a command
that exceeds it degrades to a failed receipt — `exit_code: null`, empty output,
`duration_s` pinned to the ceiling. If a suite's honest runtime is anywhere near
that, declare its own budget so a long-but-healthy run is not recorded as a red
gate:

```python
"functional": {
    "run": ["python", "-m", "pytest", "-q", "tests/functional"],
    "cost": "slow",
    "timeout": 1800,   # positive int seconds; raises the ceiling for this command only
},
```

Size it generously above observed runtime rather than removing the ceiling — the
empty-output-at-the-ceiling receipt is what makes a genuine hang legible.

The lifecycle is declaration → selection → scheduled identity → execution →
immutable receipt → readiness. Use `orcho quality-gates` to inspect the
resolved identities without executing commands.

## 3. Classify cost from evidence

Cost has exactly four values:

- `fast`: a bounded, deterministic local check that provides quick feedback.
- `moderate`: a bounded check that needs materially more setup or time than
  fast feedback, but is still routine to run locally.
- `slow`: a broad, expensive, service-heavy, or long-running proof.
- `unknown`: no reliable evidence yet, or variable, destructive, networked, or
  credential-dependent behavior whose cost cannot be predicted safely.

Cost describes expected evidence scope. It is independent of selection,
schedule, executor, policy, action, consequence, and receipt authority. Put a
cost on each command or use a gate set's `default_cost`.

### Why the classification pays

Cost is not a label for reporting. It is how you decide where a check earns its
place in the schedule, and the difference is measured in wall-clock and tokens
on every failing run.

A failing gate with a `required_action` consequence routes the run into repair.
If the check that catches a defect is cheap and runs early, the run turns
around in seconds; if the only thing that catches it is the broad proof, the
run pays for the broad proof first, every time. In one recorded run a `ruff`
gate failed **one second** after the implementation phase and sent the run
straight to repair — the broad test suite, which takes minutes on the same
project, never had to execute for that round.

The engine does not reorder your gates by cost. Selected identities are
scheduled by hook and phase, so the ladder is something the contract author
builds:

- declare hygiene-shaped checks (`lint`, format, type) as `fast` and give them
  a hook that fires as early as a defect can exist — typically right after the
  implementation phase;
- keep the broad, service-heavy proof in its own group at its own hook, so a
  cheap failure does not have to wait behind it;
- do not put a fast check and a slow one in a group whose schedule makes the
  fast one meaningless — the early exit is the whole point of declaring it.

An honest `fast` on a check that is actually slow costs you nothing at
declaration time and everything on the first failing run.

## 4. Group, select, and schedule gates

Group commands with `gate_sets`, then use `selection` to say which groups apply
and `schedule` to give selected commands a phase identity. `selection` answers
which evidence applies; `schedule` answers when it is relevant; the executor
owns the actual invocation; `policy` and `action` determine a failure's
consequence; and the receipt records immutable evidence. `work_mode` projects
policy defaults, but never turns cost into a policy decision.

## 5. Inspect the resolved contract and act on receipts

Run `orcho quality-gates --project .` to inspect selected identities, resolved
costs, schedule hooks, and policy before executing anything. A `require` or
`warn` scheduled identity is engine-owned and produces an authoritative,
immutable engine receipt. `manual` and `suggest` entries remain operator-owned.
Use the finalized receipts and delivery readiness result to decide whether work
can be handed off. Do not copy official scheduled commands into implementation
tasks: agents may run targeted, non-overlapping feedback, while scheduled gates
remain engine-owned.

## Next steps

See the [plugin reference](../expert/01_plugin.md) for every public field and
[task-file guidance](../authoring-task-files.md) for ownership boundaries. The
older [quality-gate extension mechanism](quality_gate_authoring.md) is for
registered handler plugins, not this native-command contract.
