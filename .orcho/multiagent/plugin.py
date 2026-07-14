from core.infra.platform import venv_python_subpath
from pipeline.skills import SkillTrustPolicy

# Platform-correct venv interpreter (``.venv/Scripts/python.exe`` on Windows,
# ``.venv/bin/python`` elsewhere); resolved on the host running verification.
_VENV_PY = venv_python_subpath()

PLUGIN = {
    "name": "orcho-core",
    "language": "Python 3.12",
    "architecture": (
        "Pipeline engine: run lifecycle, phase dispatch, runtimes, evidence, "
        "verification contracts, SDK, CLI, and durable run-state."
    ),
    "file_hints": [
        "pipeline/",
        "sdk/",
        "cli/",
        "core/",
        "tests/unit/",
        "docs/architecture/",
    ],
    "skill_trust": SkillTrustPolicy(trust_project=True),
    "work_mode": "pro",
    "verification_envs": {
        "core-local": {
            "python": f"{{project}}/{_VENV_PY}",
            "cwd": "{checkout}",
            "assertions": [
                {"file_exists": f"{{project}}/{_VENV_PY}"},
                {
                    "import": "pipeline",
                    "path_equals": "{checkout}/pipeline/__init__.py",
                },
                {
                    "import": "sdk",
                    "path_equals": "{checkout}/sdk/__init__.py",
                },
                {"file_exists": "pyproject.toml"},
                {"version": ["python", "--version"], "contains": "Python 3.12"},
            ],
        },
    },
    "verification": {
        "default_env": "core-local",
        "delivery_policy": "warn",
        "required": [
            "env-provenance",
            "lint",
        ],
        "commands": {
            "env-provenance": {
                "env": "core-local",
                "cheap": True,
                "run": [
                    "python",
                    "-c",
                    (
                        "import pipeline, sdk; "
                        "print('pipeline', pipeline.__file__); "
                        "print('sdk', sdk.__file__)"
                    ),
                ],
            },
            "lint": {
                "env": "core-local",
                "cheap": True,
                "run": ["python", "-m", "ruff", "check", "."],
            },
            "run-state-unit": {
                "env": "core-local",
                "parity": "differential",
                "cheap": False,
                "run": [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/unit/pipeline/run_state",
                    "tests/unit/pipeline/lifecycle/test_execute_step.py",
                    "tests/unit/pipeline/orchestrator/test_done_summary.py",
                ],
            },
            "verification-unit": {
                "env": "core-local",
                "parity": "differential",
                "cheap": False,
                "run": [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/unit/pipeline/verification",
                    "tests/unit/pipeline/project/test_verification_contract_projection.py",
                    "tests/unit/sdk/test_verify.py",
                    "tests/unit/sdk/test_fine_tune.py",
                ],
            },
            "cli-sdk-unit": {
                "env": "core-local",
                "parity": "differential",
                "cheap": False,
                "run": [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/unit/cli/test_cli_orcho.py",
                    "tests/unit/sdk",
                ],
            },
            "broad-non-e2e": {
                "env": "core-local",
                "parity": "differential",
                "cheap": False,
                "run": [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "-m",
                    "not e2e and not packaging",
                ],
            },
            "e2e": {
                "env": "core-local",
                "parity": "differential",
                "cheap": False,
                "run": ["python", "-m", "pytest", "-q", "-m", "e2e"],
            },
        },
        "gate_sets": {
            "baseline": {
                "commands": ["env-provenance", "lint"],
                "default_policy": "warn",
                "default_cheap": True,
            },
            "run-state": {
                "commands": ["run-state-unit"],
                "default_policy": "require",
                "default_cheap": False,
            },
            "verification": {
                "commands": ["verification-unit"],
                "default_policy": "require",
                "default_cheap": False,
            },
            "cli-sdk": {
                "commands": ["cli-sdk-unit"],
                "default_policy": "require",
                "default_cheap": False,
            },
            "broad": {
                "commands": ["broad-non-e2e"],
                "default_policy": "require",
                "default_cheap": False,
            },
            "e2e": {
                "commands": ["e2e"],
                "default_policy": "suggest",
                "default_cheap": False,
            },
        },
        "selection": [
            {"always": ["baseline", "broad"]},
            {
                "paths": [
                    "pipeline/run_state/**",
                    "pipeline/lifecycle.py",
                    "pipeline/project/finalization.py",
                    "pipeline/phases/builtin/subtask_dag.py",
                    "tests/unit/pipeline/run_state/**",
                    "tests/unit/pipeline/lifecycle/**",
                ],
                "include": ["run-state"],
            },
            {
                "paths": [
                    "pipeline/verification*.py",
                    "pipeline/verification/**",
                    "pipeline/project/gate_repair.py",
                    "pipeline/phases/builtin/prompt_parts.py",
                    "pipeline/phases/builtin/review_support.py",
                    "tests/unit/pipeline/verification/**",
                    "tests/unit/pipeline/project/test_verification_contract_projection.py",
                ],
                "include": ["verification"],
            },
            {
                "paths": [
                    "cli/**",
                    "sdk/**",
                    "tests/unit/cli/**",
                    "tests/unit/sdk/**",
                ],
                "include": ["cli-sdk"],
            },
            {
                "paths": [
                    "pyproject.toml",
                    "tests/conftest.py",
                    "core/_config/**",
                ],
                "include": ["broad"],
            },
            {"operator": ["e2e"]},
        ],
        "schedule": [
            {
                "after_phase": "implement",
                "gate_sets": ["baseline"],
                "policy": "warn",
            },
            {
                "after_phase": "implement",
                "gate_sets": ["run-state", "verification", "cli-sdk"],
                "policy": "require",
                "action": "repair_loop",
            },
            {
                "after_phase": "implement",
                "gate_sets": ["broad"],
                "policy": "require",
                "action": "repair_loop",
            },
            {
                "manual_only": True,
                "gate_sets": ["e2e"],
                "policy": "suggest",
            },
        ],
    },
}
