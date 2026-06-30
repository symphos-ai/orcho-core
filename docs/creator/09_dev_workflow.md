# Dev Workflow — DEV ↔ STABLE via `orcho-promote`

Orcho develops itself through two **separate** installs of its own core.
This lets you hack on the DEV copy while the STABLE copy quietly serves
your everyday runs — no "broke orcho mid-plan and now everything is
stuck".

## Concept

```
┌──────────────────────────────────────┐    ┌──────────────────────────────────────┐
│  DEV                                  │    │  STABLE                               │
│  $HOME/www/orcho/orcho-core           │    │  $HOME/.local/share/orcho-core    │
│  ────────────────────                 │    │  ──────────────────────────          │
│  active code you edit                 │    │  working installation                 │
│  pytest runs right here               │    │  where your production runs start    │
│  CLI: ``orcho-dev …``                 │    │  CLI: ``orcho …``                    │
└──────────────────────────────────────┘    └──────────────────────────────────────┘
                │                                                 ▲
                │   orcho-promote (main-only)                     │
                │   ──────────────────────                        │
                │   0. guard: refuse if not on main               │
                │   1. push DEV main          ──── github ──→     │
                │   2. pull STABLE                                │
                │   3. pip install -e "."                         │
                └─────────────────────────────────────────────────┘
```

Both point at the same github repository
[`symphos-ai/orcho-core`](https://github.com/symphos-ai/orcho-core).
DEV pushes its commits there, STABLE pulls them back. GitHub is the
single source of truth; local sync is via pull/push, not symlinks or
fs-mirroring.

**Feature branches are not promoted directly.** STABLE always tracks
``main``; to get a feature onto STABLE, first ``git switch main &&
git merge --ff-only feat/xxx && git push``, then ``orcho-promote``. This
is deliberate: auto-merging behind the user's back breaks the review
flow on projects that have one, while the guard is one line of code and
zero cognitive load when you are on main by default.

## Facade commands

In `~/.zshrc`:

```bash
export ORCHO_CORE="$HOME/.local/share/orcho-core"      # STABLE
export ORCHO_CORE_DEV="$HOME/www/orcho/orcho-core"        # DEV (sibling repos: orcho-ui-kit etc.)

orcho()     { (source "$ORCHO_CORE/.venv/bin/activate" \
              && "$ORCHO_CORE/.venv/bin/python" -m cli.orcho "$@"); }

orcho-dev() { (source "$ORCHO_CORE_DEV/.venv/bin/activate" \
              && "$ORCHO_CORE_DEV/.venv/bin/python" -m cli.orcho "$@"); }

orcho-promote() {
    echo "🚀 Promoting DEV → STABLE..."
    local branch
    branch="$(cd "$ORCHO_CORE_DEV" && git rev-parse --abbrev-ref HEAD)" || {
        echo "❌ Cannot read current branch in $ORCHO_CORE_DEV"; return 1
    }
    if [[ "$branch" != "main" ]]; then
        echo "❌ DEV is on '$branch', not 'main'. STABLE only tracks main."
        echo "   Merge into main first:"
        echo "     cd \"$ORCHO_CORE_DEV\" && git switch main && git merge --ff-only $branch && git push"
        echo "   Then re-run orcho-promote."
        return 1
    fi

    echo "1️⃣  Push DEV (main) to GitHub..."
    (cd "$ORCHO_CORE_DEV" && git push) || { echo "❌ Push failed"; return 1; }
    echo "2️⃣  Pull into STABLE..."
    (cd "$ORCHO_CORE" && git pull) || { echo "❌ Pull failed"; return 1; }
    echo "3️⃣  Reinstall deps in STABLE venv..."
    # --force-reinstall so a version bump in pyproject.toml actually
    # lands in importlib.metadata (plain ``-e .`` skips when sources
    # haven't changed). Do not use --no-deps: STABLE must receive
    # normal runtime dependencies declared by orcho-core.
    (cd "$ORCHO_CORE" && source "$ORCHO_CORE/.venv/bin/activate" \
        && pip install --force-reinstall -e "." -q) \
        || { echo "❌ Install failed"; return 1; }
    echo "4️⃣  Installed package versions..."
    "$ORCHO_CORE/.venv/bin/python" - <<'PYVERSIONS'
import importlib.metadata as md

for package in ("orcho-core", "tiktoken"):
    try:
        print(f"   {package}: {md.version(package)}")
    except md.PackageNotFoundError:
        print(f"   {package}: NOT INSTALLED")
PYVERSIONS
    echo "✅ Done! STABLE is now at: $(cd $ORCHO_CORE && git log --oneline -1)"
}
```

| Command | Which code it uses | When |
|---|---|---|
| `orcho` | STABLE | Regular runs on projects |
| `orcho-dev` | DEV | Smoke-test fresh edits without promoting |
| `orcho-promote` | both | Move STABLE to the current DEV `HEAD` |

## Local config between DEV and STABLE

`config.local.json` is looked up in layers. The last layer found wins:

| Priority | Path | Purpose |
|---:|---|---|
| 1 | `_config/config.local.json` | Quick gitignored DEV edits inside the install |
| 2 | `~/.orcho/config.local.json` | Shared user settings for DEV and STABLE |
| 3 | `$ORCHO_WORKSPACE/.orcho/config.local.json` | Settings of a specific workspace |

`orcho workspace init` creates `$ORCHO_WORKSPACE/.orcho/config.local.json`
the first time and never overwrites it afterwards. The file contains all
workspace-level knobs with real starting values from the defaults,
package-local, and user-global layers. So models, efforts, artifact
language, timeouts, and pipeline knobs can live in the workspace without
being lost after `orcho-promote`.

To migrate an old DEV-only file:

```bash
mkdir -p ~/.orcho
mv "$ORCHO_CORE_DEV/core/_config/config.local.json" ~/.orcho/config.local.json
```

For deterministic tests and smoke runs:

```bash
ORCHO_DISABLE_LOCAL_CONFIG=1 orcho-dev ...
```

## Initial setup (one machine — once)

```bash
# 1. Clone orcho-core into the DEV location (next to orcho-ui-kit and other sibling repos).
mkdir -p "$HOME/www/orcho"
cd "$HOME/www/orcho"
git clone git@github.com:symphos-ai/orcho-core.git orcho-core

# 2. DEV venv with an editable install (dev extra for pytest).
cd "$HOME/www/orcho/orcho-core"
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# 3. Clone the STABLE copy.
mkdir -p "$HOME/.local/share"
cd "$HOME/.local/share"
git clone git@github.com:symphos-ai/orcho-core.git

# 4. STABLE venv with an editable install (no extras — STABLE does not need tests,
#    the web dashboard moved to the separate ``orcho-web`` package).
cd "$HOME/.local/share/orcho-core"
python3 -m venv .venv
.venv/bin/python -m pip install -e "."

# 5. Add the variables and facades to ~/.zshrc (see the section above).
```

## Daily loop

```bash
# 1. Hack DEV
cd "$ORCHO_CORE_DEV"
$EDITOR pipeline/phases/builtin.py
"$ORCHO_CORE_DEV/.venv/bin/python" -m pytest -q

# 2. Smoke-test via orcho-dev (uses fresh DEV, not STABLE)
orcho-dev run --task "noop" --project /tmp/orcho_smoke --dry-run --max-rounds 1

# 3. Commit + promote when ready
cd "$ORCHO_CORE_DEV"
git commit -am "feat(...): ..."
orcho-promote                       # main-only — see the section below

# 4. STABLE is now at the new HEAD — the next ``orcho run …`` rides the fresh code.
orcho run --task "real task" --project /path/to/repo
```

On the lightweight `small_task` profile `validate_plan` runs in human-bypass mode, so a REJECTED plan critique does not trigger a replan loop — its findings are forwarded into `implement` as advisory reviewer feedback that the agent addresses while building rather than replanning.

### Verification receipts vs reviewer transcript

A run's output shows two different things in its verification area, and they
are not the same. The official Orcho **verification gates** (for example the
`env-provenance` and `lint` receipts from the `core-local` contract) are
recorded as durable receipts and rendered in their own `Verification gates`
block — both live/per-phase and in the DONE summary. Any commands a reviewer
happens to run while reading the diff live in the **reviewer transcript** and
carry no contract weight. When you need to confirm a gate, re-check the
receipts directly rather than trusting transcript output:

```bash
orcho verify env --env core-local --run-id <RUN_ID> --project "$ORCHO_CORE_DEV"
orcho verify run --required          --run-id <RUN_ID> --project "$ORCHO_CORE_DEV"
```

You never run these gates by hand: Orcho auto-runs them before final acceptance
in a live `Verification gates -- pre-final auto-run` block, then repeats a
compact gate line (env/command counts plus names) in the DONE summary.

### If you work on a feature branch

```bash
git switch -c feat/some-thing
$EDITOR ...                          # hack
git commit -am "..."
git push -u origin feat/some-thing   # backup on github (PR-candidate URL)

# When ready for STABLE:
git switch main
git merge --ff-only feat/some-thing  # fast-forward, no merge commit
git push
orcho-promote                        # now works: DEV is on main
```

### Smoke run blocked by `ORCHO_SYSTEM_PTY_EXHAUSTED`

If a run (or `orcho-dev` smoke) aborts at startup with
`ORCHO_SYSTEM_PTY_EXHAUSTED`, this is a **local system resource blocker** —
the pseudo-terminal pool was exhausted before the agent could start. It is
**not** a task, plan, review, or Orcho code failure, so there is nothing to fix
in the diff. The usual cause is external orphaned PTY holders: terminal
sessions or computer-use / browser-automation clients that were never cleaned
up.

Fastest diagnostic — if this raises `OSError`, the pool really is exhausted:

```bash
python -c "import pty; print(pty.openpty())"
```

Find the likely PTY holders:

```bash
lsof 2>/dev/null | grep -E '/dev/(ttys|ptmx|pty)' | \
  awk '{print $1, $2, $9}' | sort | uniq -c | sort -nr | head
```

Recovery: close or restart the leaking terminal / agent / computer-use client
(reboot as a last resort), then rerun the **same** Orcho command — no code or
plan change is needed.

## What `orcho-promote` actually does

| Step | Command | Why |
|---|---|---|
| 0 | `git rev-parse --abbrev-ref HEAD == main` guard | If DEV is on a feature branch — refuse with a hint on how to merge manually. STABLE only ever tracks main |
| 1 | `cd $ORCHO_CORE_DEV && git push` | Publish the DEV commits to github |
| 2 | `cd $ORCHO_CORE && git pull` | Pull them into STABLE |
| 3 | `cd $ORCHO_CORE && pip install --force-reinstall -e "."` | Reinstall editable dependencies in the STABLE venv. `--force-reinstall` is needed so a `version` bump in `pyproject.toml` actually lands in `importlib.metadata` (plain `-e .` skips when sources have not changed). Do not add `--no-deps`: normal runtime dependencies must be installed in STABLE. No `[web]` extra — the Streamlit dashboard moved to the separate `orcho-web` package |

Each step is fail-fast: on error it `return 1`s and the next step does not run.

## When to promote, when not to

**Promote:**
- A local feature / fix is ready, tests are green, and you want the production `orcho` commands to see the changes.
- You changed `pyproject.toml` (for example bumped a dependency pin or added a new extras block).

**Do NOT promote:**
- In the middle of a large feature on DEV — your code is broken or temporary. Use `orcho-dev` for smoke testing and keep working.
- Right before a long real run if you worry STABLE might have changed something against you. Run `orcho-promote` first, then start the run.

## In-flight runs do not pick up a promote

A running `orcho run …` process has already imported the python modules **at start time**. `orcho-promote` updates the files on disk, but the current process keeps running on the old code until it finishes. This is not a bug; it is normal python import semantics.

In practice:
- If you started a run with a hypothesis / plan / build and promote in the middle — the fixes will NOT apply to that run.
- For new fixes to actually take effect — wait for the run to finish or Ctrl+C, then start a new one.

The visible sign: the new banner line at the start of a run shows exactly the code that is actually running — for example, the `Effort:` line appeared in commit `9cbcdd1`, and runs started before the promote will not show it.

## What each venv must contain

| Scenario | DEV | STABLE |
|---|---|---|
| `pip install -e ".[dev]"` | needed (pytest is in `[dev]`) | no — bare `pip install -e "."` (we do not run tests in STABLE) |
| Editable install of orcho-core itself | yes | yes |
| Streamlit dashboard (`orcho web`) | via the separate package, `pip install orcho-web` (optional) | same |

If you accidentally run `pip install -e "."` in DEV without `[dev]`, pytest disappears. Fix it by repeating with `[dev]`.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `orcho-promote` immediately prints `❌ DEV is on '<branch>', not 'main'` | DEV is on a feature branch; STABLE only tracks main | Merge into main: `git switch main && git merge --ff-only <branch> && git push`, then re-run `orcho-promote` |
| `orcho-promote` step 1 fails with "src refspec main does not match any" | DEV `cwd` points somewhere other than the repo (e.g. the parent monorepo `~/www/orcho`) | Fix `ORCHO_CORE_DEV` in `~/.zshrc` so it points at the repo itself (`~/www/orcho/orcho-core`) |
| `orcho-promote` step 3 fails with "does not appear to be a Python project" | The subshell did not `cd` before `pip install` | Make sure the function contains `(cd "$ORCHO_CORE" && source ... && pip install ...)` |
| `orcho-promote` warning `does not provide the extra 'web'` | Old version of the function with `.[web]` — the extra was removed after web moved to `orcho-web` | Update the function to `pip install -e "."` (see the section above) |
| `orcho-dev` fails with `No module named cli.orcho` | DEV venv has no editable install | `cd "$ORCHO_CORE_DEV" && .venv/bin/python -m pip install -e ".[dev]"` |
| STABLE `orcho run` shows old behavior after a promote | Most likely an in-flight run (see the adjacent section) or nothing changed in `pyproject.toml` | Restart the run; if that does not help — `pip install --force-reinstall -e "."` in STABLE |
| `importlib.metadata.version('orcho-core')` returns the old version after a version bump | Old `orcho-promote` without `--force-reinstall` — pip skips the install when sources have not changed and leaves stale `*.dist-info` | Update the function in `~/.zshrc` to `pip install --force-reinstall -e "."` (see the section above) |
| A new runtime dependency does not import in STABLE after a promote | The old `orcho-promote` used `--no-deps`, so pip reinstalled only orcho-core | Remove `--no-deps` from the function and re-run `orcho-promote` |
| `orcho run` prints `pytest: command not found` somewhere | There are no tests in STABLE — this is a symptom of a different problem | Do not fix it by promoting; run tests only from DEV |

## Related docs

- [`08_contributing.md`](./08_contributing.md) — general contribution flow (commit style, PR process)
- [`06_testing.md`](./06_testing.md) — what and how to test in DEV before a promote
- [`docs/user/00_getting_started.md`](../user/00_getting_started.md) — for those who just use orcho, without dual-venv
