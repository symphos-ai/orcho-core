# ADR 0178 — Prompt delivery through spawn-owned stdin

Status: Accepted

## Context

Runtime adapters receive one already-composed prompt string. Passing that
string in argv makes its size subject to platform command-line limits: Windows
`CreateProcessW` accepts at most 32,767 characters, while a `.cmd` or `.bat`
shim is subsequently constrained by cmd.exe's roughly 8,191-character command
line. Those limits can turn a valid large prompt into an unhelpful startup
failure. They also expose the complete prompt in process argument inspection.

The stream runner must continue to preserve PTY stdout behavior on POSIX,
drained pipe behavior elsewhere, bounded stderr capture, masking, watchdog
accounting, transcript rendering, and retry semantics. A synchronous prompt
write before those drains are active can itself deadlock when a payload exceeds
the pipe buffer.

## Decision

`agents.stream_prompt` owns prompt-delivery modes and the daemon stdin writer.
The writer UTF-8 encodes the complete prompt, writes it after spawn while the
stdout and stderr drains are active, closes stdin to signal EOF, and suppresses
early-child pipe-close errors so the child's return code and stderr remain the
authoritative failure surfaces. Every process attempt creates a fresh writer
and pipe; retries never reuse a closed stdin descriptor.

`_stream_run` retains `argv` as its default delivery mode. This is the stable
extension contract for third-party adapters: an adapter that does nothing keeps
its previous argv/stdin wiring and remains subject to the Windows argv guard.
An adapter that opts into `stdin` supplies the original prompt separately. In
that mode, transports allocate `subprocess.PIPE` only for stdin; the POSIX PTY
continues to carry stdout through its slave and the pipe transport continues to
drain stdout normally. The Windows command-line guard applies only to `argv`.

Built-in adapters opt into `stdin` while retaining their provider-defined CLI
flag shapes. Claude uses its print flag without a positional prompt; Codex uses
the `-` stdin sentinel for both a new `exec` call (after `--cd`) and `exec
resume` (after the session id); Gemini omits its prompt flag/value pair. The
Codex resume sentinel is confirmed against local Codex CLI 0.144.3. There is
no compatibility fallback for an older CLI that rejects that documented form.

Mock runtimes consume the same writer through a hermetic pipe-backed round
trip. They do not invoke a provider binary or API.

## Alternatives considered

1. Keep every prompt in argv. This preserves the immediate implementation but
   cannot safely support large Windows prompts and continues to expose prompt
   text in process arguments.
2. Make stdin the global default. This would silently alter third-party
   runtime adapters, whose CLIs may not consume stdin.
3. Let each adapter implement its own writer or transport. This risks divergent
   EOF, retry, large-payload, and early-exit behavior.
4. Write stdin synchronously before starting stream drains. A large prompt can
   fill the pipe and deadlock before the child can make observable progress.

## Consequences

- Built-in prompt text is absent from argv, including resumed and retried
  invocations, while third-party adapters remain backward-compatible by
  default.
- Prompt composition is unchanged. The same source string remains available to
  transcript rendering, prompt tracing, masking, metrics, adapter parsing, and
  persisted evidence.
- This is a transport-internal change: public SDK and MCP wire shapes do not
  change, so no MCP alignment is required.
- Provider behavior beyond documented invocation transport is not changed; the
  adapter remains responsible only for its provider CLI's argument shape and
  session flags.
