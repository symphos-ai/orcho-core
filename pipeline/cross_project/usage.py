"""Usage accounting helpers for cross-project runtime invokes."""

from collections.abc import Callable, Mapping
from typing import Any

from core.infra import config
from core.observability.accounting_display import format_cost_reference_key_value
from pipeline.cross_project.rendering import C, paint, success, warn

# Process-local memo of unpriced models we've already warned about, so
# multi-call phases don't spam the same warning. Exposed for test resets only.
_UNPRICED_MODELS_WARNED: set[str] = set()


def _default_warn(message: str) -> None:
    print(message)


def _warn_unpriced_model(
    model: str,
    *,
    warned_models: set[str] | None = None,
    warn_fn: Callable[[str], None] | None = None,
) -> None:
    """Emit a one-time warning naming an unpriced model."""
    seen = _UNPRICED_MODELS_WARNED if warned_models is None else warned_models
    if model in seen:
        return
    seen.add(model)
    emit = _default_warn if warn_fn is None else warn_fn
    emit(
        f"No pricing for model {model!r} — cost reference will be "
        f"blank for this model's invokes. Fix: run "
        f"'orcho pricing refresh', or add an entry to "
        f"~/.orcho/pricing.local.toml."
    )


def _split_total_by_text_ratio(
    total: int, prompt: str, output: str,
) -> tuple[int, int] | None:
    """Scale ``estimate_tokens(prompt) : estimate_tokens(output)`` to ``total``."""
    from core.observability.metrics import estimate_tokens

    r_in = estimate_tokens(prompt)
    r_out = estimate_tokens(output)
    if r_in + r_out <= 0:
        return None
    tin = (r_in * total) // (r_in + r_out)
    tout = total - tin
    return tin, tout


def capture_invoke_usage(
    agent: Any,
    duration_s: float = 0.0,
    *,
    prompt: str | None = None,
    output: str | None = None,
    model: str | None = None,
    warned_models: set[str] | None = None,
    warn_fn: Callable[[str], None] | None = None,
) -> dict:
    """Normalize one invoke's usage into a single per-call dict."""
    tin_exact = int(getattr(agent, "last_tokens_in", 0) or 0)
    tout_exact = int(getattr(agent, "last_tokens_out", 0) or 0)
    tin_cache_read = int(getattr(agent, "last_tokens_in_cache_read", 0) or 0)
    tin_cache_create = int(getattr(agent, "last_tokens_in_cache_create", 0) or 0)
    tot_runtime = int(getattr(agent, "last_tokens_total", 0) or 0)
    tin_runtime_est = int(getattr(agent, "last_estimated_tokens_in", 0) or 0)
    tout_runtime_est = int(getattr(agent, "last_estimated_tokens_out", 0) or 0)
    real_cost = getattr(agent, "last_cost_usd", None)
    resolved_model = model if model is not None else getattr(agent, "model", None)

    tokens_in = 0
    tokens_out = 0
    total_tokens = 0
    split_source = "exact"
    split_estimated = False

    if tin_exact or tout_exact:
        tokens_in = tin_exact
        tokens_out = tout_exact
        total_tokens = tin_exact + tout_exact
        split_source = "exact"
        split_estimated = False
    elif tin_runtime_est or tout_runtime_est:
        tokens_in = tin_runtime_est
        tokens_out = tout_runtime_est
        total_tokens = tin_runtime_est + tout_runtime_est
        split_source = "runtime_estimate"
        split_estimated = True
    elif tot_runtime > 0:
        scaled: tuple[int, int] | None = None
        if prompt is not None and output is not None:
            scaled = _split_total_by_text_ratio(tot_runtime, prompt, output)
        if scaled is not None:
            tokens_in, tokens_out = scaled
            split_source = "text_estimate_scaled"
        else:
            half = tot_runtime // 2
            tokens_in = half
            tokens_out = tot_runtime - half
            split_source = "aggregate_total_only"
        total_tokens = tot_runtime
        split_estimated = True

    out: dict = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "total_tokens": total_tokens,
        "duration_s": round(float(duration_s), 3),
        "calls": 1,
        "token_split_source": split_source,
        "token_split_estimated": split_estimated,
    }
    if tin_cache_read:
        out["tokens_in_cache_read"] = min(tin_cache_read, tokens_in)
    if tin_cache_create:
        out["tokens_in_cache_create"] = tin_cache_create
    if resolved_model:
        out["model"] = resolved_model

    use_accounting = config.accounting_enabled()
    if use_accounting and real_cost is not None:
        out["cost_usd_equivalent"] = round(float(real_cost), 4)
        out["cost_estimated"] = False
    elif use_accounting and resolved_model and total_tokens > 0:
        from core.observability.pricing import estimate_cost_usd

        est = estimate_cost_usd(
            resolved_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached_tokens_in=int(out.get("tokens_in_cache_read") or 0),
        )
        if est is not None:
            out["cost_usd_equivalent"] = round(float(est), 4)
            out["cost_estimated"] = True
        else:
            _warn_unpriced_model(
                resolved_model,
                warned_models=warned_models,
                warn_fn=warn_fn,
            )
    return out


def _merge_provenance(prev: Any, incoming: Any) -> Any:
    """Per-field "agree-or-mixed" merger for usage provenance."""
    if incoming is None:
        return prev
    if prev is None:
        return incoming
    if prev == incoming:
        return prev
    return "mixed"


def accumulate_phase_usage(target: dict, phase: str, usage: dict) -> None:
    """Fold a single normalized invoke's usage into the per-phase rollup dict."""
    entry = target.setdefault(phase, {
        "tokens_in": 0, "tokens_out": 0, "total_tokens": 0,
        "duration_s": 0.0, "calls": 0,
    })
    entry["tokens_in"] += int(usage.get("tokens_in") or 0)
    entry["tokens_out"] += int(usage.get("tokens_out") or 0)
    entry["total_tokens"] += int(usage.get("total_tokens") or 0)
    cache_read = int(usage.get("tokens_in_cache_read") or 0)
    if cache_read:
        entry["tokens_in_cache_read"] = (
            int(entry.get("tokens_in_cache_read") or 0) + cache_read
        )
    cache_create = int(usage.get("tokens_in_cache_create") or 0)
    if cache_create:
        entry["tokens_in_cache_create"] = (
            int(entry.get("tokens_in_cache_create") or 0) + cache_create
        )
    entry["duration_s"] = round(
        entry["duration_s"] + float(usage.get("duration_s") or 0.0), 3,
    )
    entry["calls"] += int(usage.get("calls") or 1)
    cost = usage.get("cost_usd_equivalent")
    if cost is not None:
        entry["cost_usd_equivalent"] = round(
            float(entry.get("cost_usd_equivalent") or 0.0) + float(cost), 4,
        )
        if usage.get("cost_estimated"):
            entry["cost_estimated"] = True
        elif "cost_estimated" not in entry:
            entry["cost_estimated"] = False
    if usage.get("token_split_estimated"):
        entry["token_split_estimated"] = True
    elif "token_split_estimated" not in entry:
        entry["token_split_estimated"] = False
    src_in = usage.get("token_split_source")
    if src_in is not None:
        entry["token_split_source"] = _merge_provenance(
            entry.get("token_split_source"), src_in,
        )
    model_in = usage.get("model")
    if model_in:
        entry["model"] = _merge_provenance(entry.get("model"), model_in)


def format_usage_snapshot(phase_label: str, usage: Mapping[str, Any]) -> str:
    """Render one normalized usage dict as a single muted line."""
    total = int(usage.get("total_tokens") or 0)
    tin = int(usage.get("tokens_in") or 0)
    tin_cache = int(usage.get("tokens_in_cache_read") or 0)
    tout = int(usage.get("tokens_out") or 0)
    dur = float(usage.get("duration_s") or 0.0)
    calls = int(usage.get("calls") or 1)
    split_est = bool(usage.get("token_split_estimated"))
    cost = usage.get("cost_usd_equivalent")
    cost_est = bool(usage.get("cost_estimated"))
    in_op = "~" if split_est else "="
    out_op = "~" if split_est else "="
    if not config.accounting_enabled():
        cost_part = ""
    elif cost is None:
        cost_part = "cost_ref=-"
    elif cost_est:
        cost_part = format_cost_reference_key_value(
            float(cost),
            estimated=True,
            thousands=True,
        )
    else:
        cost_part = format_cost_reference_key_value(
            float(cost),
            estimated=False,
            thousands=True,
        )
    return (
        f"  usage: {phase_label}  "
        f"total={total:,}  "
        f"in{in_op}{tin:,}"
        f"{' cached=' + format(tin_cache, ',') if tin_cache else ''} "
        f"out{out_op}{tout:,}  "
        f"time={dur:.1f}s  "
        f"calls={calls}"
        f"{'  ' + cost_part if cost_part else ''}"
    )


def _capture_invoke_usage(
    agent: Any,
    duration_s: float = 0.0,
    *,
    prompt: str | None = None,
    output: str | None = None,
    model: str | None = None,
    terminal: bool = True,
) -> dict:
    """ADR 0047 Phase E — ``terminal=False`` swaps ``warn_fn`` for a
    no-op so the "unpriced model" warning suppresses under SILENT.
    Token capture + pricing math fire unconditionally; the structural
    ``cost_estimated`` field still surfaces to ``events.jsonl`` /
    ``meta.json`` so SILENT callers see the same data."""
    return capture_invoke_usage(
        agent,
        duration_s=duration_s,
        prompt=prompt,
        output=output,
        model=model,
        warned_models=_UNPRICED_MODELS_WARNED,
        warn_fn=warn if terminal else (lambda _msg: None),
    )


def _print_usage_snapshot(
    phase_label: str,
    usage: Mapping[str, Any],
    *,
    terminal: bool = True,
) -> None:
    """Print a muted single-line usage snapshot (caller-provided label).

    ADR 0047 Phase E — short-circuit under SILENT; the snapshot is
    operator-facing only, structural usage data lives on
    ``cross_phase_usage`` regardless."""
    if not terminal:
        return
    print(paint(format_usage_snapshot(phase_label, usage), C.GREY))


def _print_cross_planning_usage(
    cross_phase_usage: Mapping[str, dict],
    *,
    terminal: bool = True,
) -> None:
    """Print the cross-level planning rollup before project dispatch.

    ADR 0047 Phase E — short-circuit under SILENT (operator chip)."""
    if not terminal:
        return
    if not cross_phase_usage:
        return
    from core.observability.metrics import cross_summary_line

    success(f"Usage:   {cross_summary_line({}, dict(cross_phase_usage))}")


def _print_cross_checks_usage(
    cross_phase_usage: Mapping[str, dict],
    *,
    terminal: bool = True,
) -> None:
    """Print the terminal cross-check usage after the release verdict.

    ADR 0047 Phase E — short-circuit under SILENT (operator chip)."""
    if not terminal:
        return
    check_usage = {
        phase: dict(usage)
        for phase, usage in cross_phase_usage.items()
        if phase in {"contract_check", "cross_final_acceptance"}
    }
    if not check_usage:
        return
    from core.observability.metrics import cross_summary_line

    success(f"Cross checks usage: {cross_summary_line({}, check_usage)}")


__all__ = [
    "_UNPRICED_MODELS_WARNED",
    "_capture_invoke_usage",
    "_print_cross_checks_usage",
    "_print_cross_planning_usage",
    "_print_usage_snapshot",
    "accumulate_phase_usage",
    "capture_invoke_usage",
    "format_usage_snapshot",
]
