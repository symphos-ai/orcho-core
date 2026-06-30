"""cross-project types
(Milestone 13 implementation; types ship in )."""
import pytest

from pipeline.cross_project import (
    ArtifactSelector,
    BlockedPolicy,
    ContractResult,
    ContractValidation,
    CrossProjectProfile,
    ProjectRunRef,
    ProjectStatus,
    ProjectStep,
    WhenPolicy,
)
from pipeline.runtime import FailStrategy, GateKind

# ── ProjectStep ───────────────────────────────────────────────────────────────

class TestProjectStep:
    def test_minimal(self) -> None:
        s = ProjectStep(
            alias="api",
            project_dir="/p/api",
            profile="advanced",
            task_template="implement {endpoint}",
        )
        assert s.alias == "api"

    def test_invalid_alias(self) -> None:
        with pytest.raises(ValueError, match="alias .* invalid"):
            ProjectStep(alias="API", project_dir="/p", profile="x", task_template="y")

    def test_alias_starts_with_digit(self) -> None:
        with pytest.raises(ValueError, match="alias .* invalid"):
            ProjectStep(alias="1api", project_dir="/p", profile="x", task_template="y")

    def test_self_dependency(self) -> None:
        with pytest.raises(ValueError, match="self-dependency"):
            ProjectStep(
                alias="api", project_dir="/p", profile="x",
                task_template="y", depends_on=("api",),
            )

    def test_unknown_override_keys(self) -> None:
        with pytest.raises(ValueError, match="unknown override keys"):
            ProjectStep(
                alias="api", project_dir="/p", profile="x",
                task_template="y",
                overrides={"bogus_key": "value"},
            )

    def test_known_override_keys(self) -> None:
        s = ProjectStep(
            alias="api", project_dir="/p", profile="x", task_template="y",
            overrides={"model": "claude-sonnet-4-6", "dry_run": True},
        )
        assert s.overrides["model"] == "claude-sonnet-4-6"


# ── ContractValidation ────────────────────────────────────────────────────────

class TestContractValidation:
    def test_minimal(self) -> None:
        c = ContractValidation(
            name="openapi_diff",
            kind=GateKind.COMPUTATIONAL,
            on_fail=FailStrategy.HALT,
            inputs=(ArtifactSelector(project_alias="api", artifact_name="openapi"),),
            fires_after=("api", "frontend"),
        )
        assert c.when is WhenPolicy.ALL_SUCCEEDED
        assert c.on_blocked is BlockedPolicy.SKIP

    def test_empty_fires_after(self) -> None:
        with pytest.raises(ValueError, match="fires_after cannot be empty"):
            ContractValidation(
                name="x", kind=GateKind.COMPUTATIONAL, on_fail=FailStrategy.HALT,
                inputs=(ArtifactSelector(project_alias="api", artifact_name="o"),),
                fires_after=(),
            )

    def test_empty_inputs(self) -> None:
        with pytest.raises(ValueError, match="inputs required"):
            ContractValidation(
                name="x", kind=GateKind.COMPUTATIONAL, on_fail=FailStrategy.HALT,
                inputs=(), fires_after=("api",),
            )

    def test_when_all_finished(self) -> None:
        c = ContractValidation(
            name="audit",
            kind=GateKind.INFERENTIAL,
            on_fail=FailStrategy.INFORMATIONAL,
            inputs=(ArtifactSelector(project_alias="api", artifact_name="diff"),),
            fires_after=("api",),
            when=WhenPolicy.ALL_FINISHED,
        )
        assert c.when is WhenPolicy.ALL_FINISHED


# ── CrossProjectProfile ───────────────────────────────────────────────────────

class TestCrossProjectProfile:
    def _project(self, alias: str, project_dir: str = None, **kw) -> ProjectStep:
        return ProjectStep(
            alias=alias,
            project_dir=project_dir or f"/p/{alias}",
            profile=kw.get("profile", "lite"),
            task_template=kw.get("task_template", "task"),
            depends_on=kw.get("depends_on", ()),
        )

    def test_minimal_two_projects(self) -> None:
        p = CrossProjectProfile(
            name="fullstack",
            description="api + frontend",
            projects=(self._project("api"), self._project("frontend",
                                                         depends_on=("api",))),
        )
        assert p.parallelism == 1
        assert len(p.projects) == 2

    def test_duplicate_aliases(self) -> None:
        with pytest.raises(ValueError, match="duplicate aliases"):
            CrossProjectProfile(
                name="bad", description="x",
                projects=(self._project("api"), self._project("api")),
            )

    def test_unknown_depends_on(self) -> None:
        with pytest.raises(ValueError, match="depends_on unknown"):
            CrossProjectProfile(
                name="bad", description="x",
                projects=(self._project("api", depends_on=("ghost",)),),
            )

    def test_duplicate_canonical_dir_without_chain_rejected(self, tmp_path) -> None:
        # Two aliases targeting the same project_dir — no depends_on chain.
        d = str(tmp_path)
        with pytest.raises(ValueError, match="without depends_on ordering"):
            CrossProjectProfile(
                name="bad",
                description="x",
                projects=(
                    self._project("api1", project_dir=d),
                    self._project("api2", project_dir=d),
                ),
            )

    def test_duplicate_canonical_dir_with_chain_ok(self, tmp_path) -> None:
        # Same project_dir but ordered via depends_on: serialized, not racing.
        d = str(tmp_path)
        p = CrossProjectProfile(
            name="ok",
            description="serial",
            projects=(
                self._project("api1", project_dir=d),
                self._project("api2", project_dir=d, depends_on=("api1",)),
            ),
        )
        assert len(p.projects) == 2

    def test_unknown_contract_alias(self) -> None:
        with pytest.raises(ValueError, match="fires_after refers to unknown"):
            CrossProjectProfile(
                name="bad", description="x",
                projects=(self._project("api"),),
                contracts=(
                    ContractValidation(
                        name="c",
                        kind=GateKind.COMPUTATIONAL,
                        on_fail=FailStrategy.HALT,
                        inputs=(ArtifactSelector(project_alias="api", artifact_name="o"),),
                        fires_after=("ghost",),
                    ),
                ),
            )

    def test_zero_parallelism_rejected(self) -> None:
        with pytest.raises(ValueError, match="parallelism must be ≥1"):
            CrossProjectProfile(
                name="bad", description="x",
                projects=(self._project("api"),),
                parallelism=0,
            )

    def test_cycle_in_depends_on_rejected(self) -> None:
        """Regression — Codex P1: __post_init__ must run a Kahn cycle
 check, not just unknown-dep detection. A → B → A would
 previously construct successfully."""
        with pytest.raises(ValueError, match="cycle"):
            CrossProjectProfile(
                name="cyclic", description="x",
                projects=(
                    self._project("api", depends_on=("web",)),
                    self._project("web", depends_on=("api",)),
                ),
            )

    def test_three_node_cycle_rejected(self) -> None:
        """A → B → C → A should also be caught."""
        with pytest.raises(ValueError, match="cycle"):
            CrossProjectProfile(
                name="cyclic3", description="x",
                projects=(
                    self._project("api", depends_on=("web",)),
                    self._project("web", depends_on=("admin",)),
                    self._project("admin", depends_on=("api",)),
                ),
            )

    def test_self_loop_caught_in_projectstep(self) -> None:
        """Self-dependency should be caught at the ProjectStep level
 before it reaches CrossProjectProfile cycle detection."""
        with pytest.raises(ValueError, match="self-dependency"):
            self._project("api", depends_on=("api",))

    def test_complex_dag_no_cycle_ok(self) -> None:
        """A diamond-shape DAG (api → admin, web → admin) constructs
 cleanly — validates that the cycle check doesn't false-positive
 on shared dependencies."""
        p = CrossProjectProfile(
            name="diamond", description="x",
            projects=(
                self._project("api"),
                self._project("web"),
                self._project("admin", depends_on=("api", "web")),
            ),
        )
        assert len(p.projects) == 3


# ── ProjectRunRef + ContractResult shells ────────────────────────────────────

class TestProjectRunRef:
    def test_basic(self) -> None:
        ref = ProjectRunRef(
            alias="api",
            run_id="run-123",
            project_dir="/p/api",
            artifact_index={"openapi": "docs/openapi.yaml"},
            status=ProjectStatus.SUCCEEDED,
        )
        assert ref.failed_phase is None
        assert ref.artifact_index["openapi"] == "docs/openapi.yaml"


class TestContractResult:
    def test_basic(self) -> None:
        r = ContractResult(
            contract_name="openapi_diff",
            passed=True,
            output="schemas match",
            duration_s=0.42,
            kind=GateKind.COMPUTATIONAL,
        )
        assert r.passed is True
