# Windows OS Adapters Fix Plan

## Context

Orcho already has partial Windows support in the sandbox/runtime layer, but
several higher-level paths still call POSIX-only APIs directly. The Windows fix
should not spread `if platform.system() == "Windows"` through orchestration
logic. The repair should introduce focused OS adapters and keep engine, MCP,
workspace, and verification code behind neutral contracts.

This is a planning artifact. It describes the intended repair shape before
implementation.

## Primary Blockers

### 1. Process Streaming Adapter

Current risk:

- `orcho-core/agents/stream.py` imports Unix-only modules such as `pty` at module
  import time.
- `orcho-core/agents/__init__.py` imports `agents.stream`, so native Windows can
  fail before any run starts.
- The current streaming implementation is PTY-based.

Fix:

- Add a stream backend abstraction, for example `StreamBackend`.
- Move existing PTY logic into a POSIX backend.
- Add a Windows pipe backend using `subprocess.Popen(..., stdout=PIPE,
  stderr=PIPE)` with safe pipe draining.
- Lazy-import `pty` and other POSIX-only modules only inside the POSIX backend.
- Keep the public `_stream_run` behavior stable while delegating to the selected
  backend.

Acceptance:

- `python -c "import agents"` passes on Windows.
- `orcho --help` passes on Windows.
- POSIX PTY tests remain green.
- Windows pipe-stream tests cover stdout, stderr, exit code, timeout, and abort.

## 2. MCP Process Tree And Cancellation Adapter

Current risk:

- `orcho-mcp` supervisor uses POSIX process groups: `start_new_session=True`,
  `os.killpg`, `SIGTERM`, and `SIGKILL`.
- Native Windows does not provide the same process-group semantics.
- Async MCP run spawn, resume, cancel, and recovery are therefore unsafe on
  Windows.

Fix:

- Add a process lifecycle abstraction, for example `ProcessTreeAdapter` and
  `ProcessHandle`.
- POSIX backend:
  - use `start_new_session=True`;
  - terminate via process group.
- Windows backend:
  - use `CREATE_NEW_PROCESS_GROUP`;
  - prefer Job Object support when available;
  - provide a fallback using process termination or `taskkill /T /F` when a
    process tree must be killed.
- Store neutral supervisor metadata: `pid`, `platform`, and optional
  adapter-specific group/job identifiers.
- Change user-facing wording from Unix-specific signal language to generic
  `terminate` / `kill` lifecycle language.

Acceptance:

- MCP async spawn works on Windows.
- MCP cancel does not import or call POSIX-only process APIs on Windows.
- Resume/recovery can reason about a Windows process handle.
- Existing POSIX supervisor behavior remains unchanged.

## 3. Workspace Env Script Writer

Current risk:

- Workspace init writes `orcho-env.sh`.
- Windows users need a first-class PowerShell activation path.
- Existing docs/onboarding lean on `source .../orcho-env.sh`.

Fix:

- Add an env script writer abstraction, for example `EnvScriptWriter`.
- Keep Bash writer for `orcho-env.sh`.
- Add PowerShell writer for `orcho-env.ps1`.
- Consider writing both scripts on every platform so generated workspaces are
  portable, while onboarding points to the native command for the current OS.

Acceptance:

- `orcho workspace init` creates a valid `orcho-env.ps1`.
- Windows onboarding uses PowerShell instructions.
- Existing Bash workflow stays unchanged.

## 4. Verification Tool Path Resolver

Current risk:

- Fine-tune currently emits POSIX tool paths such as `.venv/bin/python`.
- Native Windows Python virtualenv layout is `.venv/Scripts/python.exe`.
- Project-specific details must not be hard-coded into `orcho-core`.

Fix:

- Add a platform-aware tool path resolver, for example `ToolPathResolver` or
  `RuntimeLayout`.
- Resolve common runtime layouts:
  - Python venv on POSIX: `.venv/bin/python`;
  - Python venv on Windows: `.venv/Scripts/python.exe`.
- Keep command declarations argv-based where possible.
- Move project/package-specific verification knowledge into project plugins or
  package plugins, not into `orcho-core`.

Acceptance:

- Fine-tune does not generate POSIX-only Python paths on Windows.
- Generated verification commands can be represented without shell-only syntax.
- Project verification remains argv-list based and OS-agnostic where possible.

## 5. Web Launcher Process Adapter

Current risk:

- `orcho-web` launcher has some POSIX-oriented lifecycle behavior and help text.
- Port checks already have a socket fallback, but process handling and UX should
  use the same OS-neutral semantics as the rest of Orcho.

Fix:

- Reuse the process lifecycle adapter where practical, or introduce a small
  launcher-specific adapter if the web launcher needs less surface area.
- Keep socket-based port checks as the portable default.
- Make guidance text OS-aware: avoid suggesting Unix-only `kill PID` on Windows.

Acceptance:

- Dashboard launch works without POSIX-only process assumptions.
- Windows users get Windows-appropriate process guidance.
- Existing macOS/Linux launcher behavior remains stable.

## 6. Packaging And Install Friction

Current risk:

- Some install paths depend on git+ssh package resolution.
- On clean Windows machines this requires Git, SSH setup, and working keys,
  which is fragile for normal installation.

Fix:

- Prefer PyPI, wheel, local editable workspace, or documented local path
  dependency flows for first-party packages.
- Keep git+ssh usable for maintainers, but avoid making it the only path for a
  Windows install.

Acceptance:

- A clean Windows checkout can install Orcho components without private SSH
  assumptions.
- Developer install docs state the expected Windows path clearly.

## Suggested Implementation Order

1. Make `agents.stream` import-safe and add the stream backend abstraction.
2. Add MCP process tree/cancellation adapters.
3. Add PowerShell workspace env script generation.
4. Add platform-aware verification tool path resolution.
5. Clean up web launcher process handling and UX text.
6. Fix packaging/install friction for Windows developer setup.
7. Add Windows CI smoke coverage.

## Windows Smoke Matrix

Minimum useful Windows smoke suite:

- `python -c "import agents"`;
- `orcho --help`;
- `orcho workspace init` and PowerShell env script presence;
- a mock local run that exercises command streaming;
- MCP async spawn and cancel;
- verification contract projection for argv-list commands;
- fine-tune output for a Python venv project.

## Non-Goals

- Do not encode project-specific behavior into `orcho-core`.
- Do not scatter platform checks through orchestration bodies.
- Do not rewrite unrelated runtime/profile behavior while introducing adapters.
- Do not use generated Orcho run worktrees as canonical source for these fixes.
