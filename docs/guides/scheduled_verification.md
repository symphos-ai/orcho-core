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
            "unit": {"run": ["python", "-m", "pytest", "-q", "tests/unit"], "cost": "slow"},
        },
        "gate_sets": {
            "hygiene": {"commands": ["lint"], "default_policy": "require"},
            "tests": {"commands": ["unit"], "default_policy": "warn"},
        },
        "selection": [{"always": ["hygiene", "tests"]}],
        "schedule": [
            {"after_phase": "implement", "gate_sets": ["hygiene"], "action": "repair_loop"},
            {"after_phase": "implement", "gate_sets": ["tests"]},
        ],
    },
}
```

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
