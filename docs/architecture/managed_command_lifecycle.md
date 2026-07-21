# Managed long-command lifecycle

Authoring agents sometimes need a legitimate repo-wide or long-running command
that is not one of the project's configured verification gates. Those commands
use a run-scoped admission boundary so loss of a provider tool handle cannot
start an equivalent second process.

```bash
orcho command run \
  --run-dir /path/to/run \
  --phase implement \
  --cwd /path/to/checkout \
  -- python -m pytest tests/integration
```

The phase prompt supplies the exact `--run-dir`, `--phase`, and `--cwd`
prefix. Append the executable and arguments without wrapping them in a second
shell. Targeted and diff-scoped checks do not need this boundary. Configured
broad verification commands are executed by Orcho's gate engine and should not
be duplicated by the authoring agent.

The command creates an atomic identity lease before spawning its child. Only an
exact child exit writes the terminal receipt and releases the identity. If the
lease already exists, the command exits with code 75 and explains that the
terminal state is unresolved. Do not bypass that refusal or infer completion
from partial output, elapsed time, a missing provider handle, or process-name
searches.

Inspect one identity without launching it by replacing `run` with `status` and
passing the same arguments. `exited` includes the exact recorded exit code;
`unknown` is deliberately fail-closed.

See [ADR 0147](../adr/0147-run-scoped-managed-command-admission.md) for the
protocol decision and [ADR 0143](../adr/0143-provider-owned-child-lifecycle.md)
for ownership of the top-level provider process.
