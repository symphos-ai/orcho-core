"""Project test-gate execution helpers."""

import subprocess
import time
from collections.abc import Callable

from agents.entities import TestResult
from pipeline.plugins import PluginConfig, TestingConfig


def run_single_test(
    cwd: str,
    run_command: str,
    fail_keyword: str,
    timeout: int,
    *,
    subprocess_run: Callable[..., subprocess.CompletedProcess] | None = None,
    timeout_expired: type[BaseException] | None = None,
) -> TestResult:
    """Run one shell test command and return a TestResult.

    ``subprocess_run`` / ``timeout_expired`` default to ``None`` and
    resolve to ``subprocess.run`` / ``subprocess.TimeoutExpired`` at
    call time so test suites that ``patch("subprocess.run", ...)`` or
    ``patch("pipeline.project_testing.subprocess.run", ...)`` see the
    rebinding take effect. (Capturing the defaults at definition time
    snapshots the original function objects, defeating the patch.)
    """
    from core.observability import events as _events

    if subprocess_run is None:
        subprocess_run = subprocess.run
    if timeout_expired is None:
        timeout_expired = subprocess.TimeoutExpired

    argv_summary = run_command if len(run_command) <= 200 else run_command[:197] + "..."
    _events.emit(
        "command.start", argv_summary=argv_summary, cwd=cwd,
        command_kind="tests",
    )
    t0 = time.monotonic()
    try:
        result = subprocess_run(
            run_command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except timeout_expired as e:
        duration = time.monotonic() - t0
        partial = ((e.stdout or "") + (e.stderr or "")).strip()
        _events.emit(
            "command.end", exit_code=-1, duration_s=round(duration, 3),
            outcome="timeout",
        )
        return TestResult(
            passed=False,
            output=f"[TIMEOUT after {timeout}s]\n{partial}",
            duration=duration,
        )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    keyword_hit = bool(fail_keyword) and fail_keyword in output
    passed = result.returncode == 0 and not keyword_hit
    duration = time.monotonic() - t0
    _events.emit(
        "command.end",
        exit_code=int(result.returncode),
        duration_s=round(duration, 3),
        outcome="ok" if passed else "failed",
    )
    return TestResult(
        passed=passed,
        output=output,
        duration=duration,
    )


def resolve_tests_config(plugin: PluginConfig) -> TestingConfig:
    """Resolve the active TestingConfig from ``plugin.quality_gates["tests"]``."""
    from pipeline.plugins import TestSuiteConfig

    raw = (plugin.quality_gates or {}).get("tests")
    if not raw or not isinstance(raw, dict):
        return TestingConfig()

    testing_known = {f for f in TestingConfig.__dataclass_fields__}
    suites_raw = raw.get("suites", [])
    filtered = {
        k: v for k, v in raw.items()
        if k in testing_known and k != "suites"
    }
    if suites_raw:
        suite_known = {f for f in TestSuiteConfig.__dataclass_fields__}
        filtered["suites"] = [
            TestSuiteConfig(**{k: v for k, v in s.items() if k in suite_known})
            if isinstance(s, dict) else s
            for s in suites_raw
        ]
    return TestingConfig(**filtered)


def run_tests(
    cwd: str,
    plugin: PluginConfig,
    *,
    run_single_test_fn: Callable[[str, str, str, int], TestResult] | None = None,
    success_fn: Callable[[str], None] | None = None,
) -> TestResult:
    """Run the project's configured test suite(s).

    ``run_single_test_fn`` defaults to ``None`` and resolves to
    :func:`run_single_test` (this module) at call time so test suites
    that ``monkeypatch.setattr(pipeline.project_testing, "run_single_test", ...)``
    see the rebinding take effect. (Capturing the default at definition
    time snapshots the original function object, defeating the patch.)
    """
    if run_single_test_fn is None:
        run_single_test_fn = run_single_test
    cfg = resolve_tests_config(plugin)

    if cfg.suites:
        runnable = [s for s in cfg.suites if s.run_command]
        if not runnable:
            return TestResult(skipped=True)

        all_passed = True
        combined_output: list[str] = []
        total_duration = 0.0

        for suite in cfg.suites:
            if not suite.run_command:
                combined_output.append(f"[{suite.name or 'suite'}] skipped (no run_command)")
                continue
            label = suite.name or suite.run_command[:40]
            if success_fn is not None:
                success_fn(f"Running suite: {label}")
            result = run_single_test_fn(
                cwd, suite.run_command, suite.fail_keyword, suite.timeout,
            )
            total_duration += result.duration
            status = "✓ passed" if result.passed else "✗ FAILED"
            combined_output.append(
                f"[{label}] {status} ({result.duration:.1f}s)\n{result.output}",
            )
            if not result.passed:
                all_passed = False

        return TestResult(
            passed=all_passed,
            output="\n\n".join(combined_output),
            duration=total_duration,
        )

    if not cfg.run_command:
        return TestResult(skipped=True)

    return run_single_test_fn(cwd, cfg.run_command, cfg.fail_keyword, cfg.timeout)


def test_result_to_dict(tr: TestResult) -> dict:
    """Serialise TestResult for the session JSON log."""
    return {
        "skipped": tr.skipped,
        "passed": tr.passed,
        "duration": round(tr.duration, 3),
        "output": tr.output,
    }


__all__ = [
    "resolve_tests_config",
    "run_single_test",
    "run_tests",
    "test_result_to_dict",
]
