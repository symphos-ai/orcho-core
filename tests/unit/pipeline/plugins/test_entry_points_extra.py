"""entry_points discovery for orcho.profiles /
orcho.execution_modes / orcho.session_adapters / orcho.quality_gates.

Pin the shared discovery contract:

 * ``discover_entry_points`` accepts the three load shapes (instance /
 factory function / class), invokes callables once, returns a
 {name → instance} dict.
 * One broken plugin entry doesn't block discovery for the rest —
 failures logged, loop continues, partial registry returned.
 * ``register_entry_points`` integrates plugin entries onto the
 target registry via its ``register`` method (matching the shape
 of QualityGateRegistry / ExecutionModeRegistry /
 SessionAdapterRegistry).
 * Plugin overrides win: re-registering an entry name shadows the
 built-in (supported customer-overlay mechanism).
 * ``load_profiles_v2_with_plugins`` merges plugin-shipped Profile
 instances into the shipped JSON registry.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.entry_points import discover_entry_points, register_entry_points

# ── Test fixtures: fake EntryPoint objects ───────────────────────────────────


class _FakeEP:
    """Mimic the shape of importlib.metadata.EntryPoint that
 ``ep.name`` + ``ep.load()`` access. Tests pass a list of these via
 ``patch("importlib.metadata.entry_points", return_value=[...])``."""

    def __init__(self, name: str, load_result):
        self.name = name
        self._load_result = load_result

    def load(self):
        if isinstance(self._load_result, BaseException):
            raise self._load_result
        return self._load_result


def _patch_entry_points(eps, *, target_group: str | None = None):
    """Patch importlib.metadata.entry_points so the supplied list is
 returned for the named ``target_group``. ``target_group=None``
 matches any group (used by ``discover_entry_points`` /
 ``register_entry_points`` direct tests that pass
 ``"test.group"``).

 All other groups return ``[]`` so the patch doesn't accidentally
 inject the fakes into unrelated entry_points scans elsewhere in
 the test (e.g. ``AgentRegistry.default()`` running concurrently).
 """

    def _entry_points_replacement(*, group=None):
        if group is None:
            return []
        if target_group is None:
            # Caller didn't constrain — return the fakes for any group
            # the production code asks about. Used for the helper-level
            # tests that pass ``"test.group"``.
            return eps if group == "test.group" else []
        return eps if group == target_group else []

    return patch(
        "importlib.metadata.entry_points",
        side_effect=_entry_points_replacement,
    )


# ── discover_entry_points ────────────────────────────────────────────────────


class TestDiscoverEntryPoints:
    def test_bare_instance_returned_as_is(self) -> None:
        sentinel = object()
        with _patch_entry_points([_FakeEP("alpha", sentinel)]):
            result = discover_entry_points("test.group")
        # ``object`` is not callable, so bare-instance path returns
        # the value unchanged.
        assert result == {"alpha": sentinel}

    def test_zero_arg_callable_invoked(self) -> None:
        instance = {"x": 1}

        def factory():
            return instance

        with _patch_entry_points([_FakeEP("beta", factory)]):
            result = discover_entry_points("test.group")
        # Callable was invoked; result is the return value.
        assert result == {"beta": instance}

    def test_class_with_no_arg_constructor_instantiated(self) -> None:
        class MyClass:
            pass

        with _patch_entry_points([_FakeEP("gamma", MyClass)]):
            result = discover_entry_points("test.group")
        assert isinstance(result["gamma"], MyClass)

    def test_callable_instance_returned_as_is(self) -> None:
        """Callable handler instances are still bare instances. Discovery
 must not execute arbitrary ``__call__`` methods while loading
 plugins."""

        class CallableHandler:
            def __init__(self) -> None:
                self.called = False

            def __call__(self):
                self.called = True
                return "wrong-shape"

        handler = CallableHandler()
        with _patch_entry_points([_FakeEP("callable_handler", handler)]):
            result = discover_entry_points("test.group")

        assert result["callable_handler"] is handler
        assert handler.called is False

    def test_load_failure_is_logged_and_skipped(self, capsys) -> None:
        good = object()
        with _patch_entry_points([
            _FakeEP("good_one", good),
            _FakeEP("broken", ImportError("module gone")),
        ]):
            result = discover_entry_points("test.group")
        # Broken plugin omitted; good plugin still registered.
        assert "broken" not in result
        assert result["good_one"] is good
        captured = capsys.readouterr().out
        assert "broken" in captured
        assert "module gone" in captured

    def test_factory_invocation_failure_logged_and_skipped(
        self, capsys,
    ) -> None:
        def boom():
            raise RuntimeError("factory exploded")

        with _patch_entry_points([_FakeEP("explosive", boom)]):
            result = discover_entry_points("test.group")
        assert "explosive" not in result
        captured = capsys.readouterr().out
        assert "explosive" in captured
        assert "factory exploded" in captured

    def test_skip_filter_excludes_named_entries(self) -> None:
        with _patch_entry_points([
            _FakeEP("keep", object()),
            _FakeEP("drop", object()),
        ]):
            result = discover_entry_points("test.group", skip={"drop"})
        assert "keep" in result
        assert "drop" not in result

    def test_unknown_group_returns_empty_dict(self) -> None:
        with _patch_entry_points([]):
            result = discover_entry_points("nonexistent.group")
        assert result == {}


# ── register_entry_points ────────────────────────────────────────────────────


class _StubRegistry:
    """Minimal ``register(name, value)`` shape — mirrors every
 registry surface."""

    def __init__(self) -> None:
        self.items: dict[str, object] = {}

    def register(self, name: str, value: object) -> None:
        self.items[name] = value


class TestRegisterEntryPoints:
    def test_plugin_entries_landed_on_registry(self) -> None:
        reg = _StubRegistry()
        a, b = object(), object()
        with _patch_entry_points([
            _FakeEP("alpha", a),
            _FakeEP("beta", b),
        ]):
            count = register_entry_points(reg, "test.group")
        assert count == 2
        assert reg.items == {"alpha": a, "beta": b}

    def test_plugin_override_replaces_existing_registration(self) -> None:
        """A plugin entry with name matching a built-in shadows it.
 Built-ins should be registered before ``register_entry_points``
 is called so this ordering holds."""
        reg = _StubRegistry()
        builtin = object()
        plugin = object()
        reg.register("tests", builtin)
        with _patch_entry_points([_FakeEP("tests", plugin)]):
            register_entry_points(reg, "test.group")
        assert reg.items["tests"] is plugin

    def test_register_failure_does_not_block_other_entries(
        self, capsys,
    ) -> None:
        """Each ``registry.register(...)`` call is wrapped in
 try/except — one failure logs but continues."""

        class _PickyRegistry(_StubRegistry):
            def register(self, name: str, value: object) -> None:
                if name == "broken":
                    raise ValueError(f"{name} not allowed")
                super().register(name, value)

        reg = _PickyRegistry()
        with _patch_entry_points([
            _FakeEP("ok1", object()),
            _FakeEP("broken", object()),
            _FakeEP("ok2", object()),
        ]):
            count = register_entry_points(reg, "test.group")
        assert count == 2
        assert "ok1" in reg.items and "ok2" in reg.items
        assert "broken" not in reg.items
        captured = capsys.readouterr().out
        assert "broken" in captured

    def test_missing_register_method_raises(self) -> None:
        class _NoRegister:
            pass

        with pytest.raises(AttributeError, match="register"):
            register_entry_points(_NoRegister(), "test.group")


# ── End-to-end: each registry picks up plugin entries ───────────────


class TestQualityGateRegistryDiscovery:
    """``default_quality_gate_registry()`` includes plugin gates."""

    def test_plugin_gate_registered_alongside_builtin_tests(
        self, monkeypatch,
    ) -> None:
        # Reset the singleton so the patched entry_points take effect.
        import pipeline.quality_gates as qg_module
        from pipeline.quality_gates import (
            GateKind,
            QualityGateResult,
            default_quality_gate_registry,
        )
        monkeypatch.setattr(qg_module, "_DEFAULT_REGISTRY", None)

        class _PluginGate:
            def execute(self, plugin, cwd, gate_config=None):
                return QualityGateResult(
                    name="custom_gate",
                    passed=True,
                    output="ok",
                    duration_s=0.0,
                    kind=GateKind.COMPUTATIONAL,
                )

        with _patch_entry_points(
            [_FakeEP("custom_gate", _PluginGate())],
            target_group="orcho.quality_gates",
        ):
            reg = default_quality_gate_registry()

        assert reg.has("tests"), "built-in 'tests' gate must remain"
        assert reg.has("custom_gate"), "plugin gate must be registered"


class TestExecutionModeRegistryDiscovery:
    def test_plugin_executor_registered(self) -> None:
        from pipeline.lifecycle import (
            default_execution_mode_registry,
        )

        class _PluginExecutor:
            def execute(self, step, state, ctx):
                return state

        with _patch_entry_points(
            [_FakeEP("parallel_review", _PluginExecutor())],
            target_group="orcho.execution_modes",
        ):
            reg = default_execution_mode_registry()

        assert reg.has("linear"), "built-in 'linear' must remain"
        assert reg.has("parallel_review"), "plugin executor must register"


class TestSessionAdapterRegistryDiscovery:
    def test_plugin_adapter_registered_alongside_builtins(
        self, monkeypatch,
    ) -> None:
        import pipeline.session_adapters as sa_module
        from pipeline.session_adapters import default_session_adapter_registry
        monkeypatch.setattr(sa_module, "_DEFAULT_REGISTRY", None)

        class _PluginAdapter:
            def write(self, phase_name, state, session, *, round_n=None):
                session.setdefault("phases", {})[phase_name] = {"plugin": True}

        with _patch_entry_points(
            [_FakeEP("compliance_check", _PluginAdapter())],
            target_group="orcho.session_adapters",
        ):
            reg = default_session_adapter_registry()

        assert reg.has("plan"), "built-in 'plan' adapter must remain"
        assert reg.has("compliance_check"), "plugin adapter must register"


class TestProfileLoaderPluginDiscovery:
    """``load_profiles_v2_with_plugins`` merges plugin profiles."""

    def test_plugin_profile_added_to_registry(self, tmp_path) -> None:
        from pipeline.profiles.loader import load_profiles_v2_with_plugins
        from pipeline.runtime import (
            PhaseStep,
            Profile,
            ProfileKind,
        )

        # Minimal shipped JSON so the loader doesn't fail on missing
        # built-ins. One profile is enough.
        path = tmp_path / "profiles.json"
        path.write_text(
            '{"shipped": {"kind": "custom", "variant": null, '
            '"description": "test", "steps": ['
            '{"phase": "plan", "execution": "linear"}'
            "]}}",
            encoding="utf-8",
        )

        custom = Profile(
            name="customer_special",
            kind=ProfileKind.CUSTOM,
            variant=None,
            description="customer-shipped",
            steps=(PhaseStep(phase="plan", execution="linear"),),
        )

        with _patch_entry_points(
            [_FakeEP("customer_special", custom)],
            target_group="orcho.profiles",
        ):
            profiles = load_profiles_v2_with_plugins(path)

        assert "shipped" in profiles, "JSON-loaded profile remains"
        assert "customer_special" in profiles, "plugin profile merged"
        assert profiles["customer_special"] is custom

    def test_plugin_can_override_shipped_profile(self, tmp_path) -> None:
        """Plugin entry with name matching a shipped profile wins —
 documented customer-overlay mechanism."""
        from pipeline.profiles.loader import load_profiles_v2_with_plugins
        from pipeline.runtime import (
            PhaseStep,
            Profile,
            ProfileKind,
        )

        path = tmp_path / "profiles.json"
        path.write_text(
            '{"task": {"kind": "scoped", "variant": "task", '
            '"description": "shipped-task", "steps": ['
            '{"phase": "implement", "execution": "linear"}'
            "]}}",
            encoding="utf-8",
        )

        override = Profile(
            name="task",
            kind=ProfileKind.SCOPED,
            variant="task",
            description="customer-overridden",
            steps=(PhaseStep(phase="implement", execution="linear"),),
        )

        with _patch_entry_points(
            [_FakeEP("task", override)],
            target_group="orcho.profiles",
        ):
            profiles = load_profiles_v2_with_plugins(path)

        assert profiles["task"].description == "customer-overridden"

    def test_profile_entry_point_name_is_registry_key(self, tmp_path) -> None:
        """Override semantics use the entry point name. A stale
 ``Profile.name`` should be normalized, otherwise an entry named
 ``task`` would fail to override the shipped ``task`` profile."""
        from pipeline.profiles.loader import load_profiles_v2_with_plugins
        from pipeline.runtime import (
            PhaseStep,
            Profile,
            ProfileKind,
        )

        path = tmp_path / "profiles.json"
        path.write_text(
            '{"task": {"kind": "scoped", "variant": "task", '
            '"description": "shipped-task", "steps": ['
            '{"phase": "implement", "execution": "linear"}'
            "]}}",
            encoding="utf-8",
        )

        stale_name = Profile(
            name="customer_task",
            kind=ProfileKind.SCOPED,
            variant="task",
            description="override-by-entry-point",
            steps=(PhaseStep(phase="implement", execution="linear"),),
        )

        with _patch_entry_points(
            [_FakeEP("task", stale_name)],
            target_group="orcho.profiles",
        ):
            profiles = load_profiles_v2_with_plugins(path)

        assert set(profiles) == {"task"}
        assert profiles["task"].name == "task"
        assert profiles["task"].description == "override-by-entry-point"

    def test_non_profile_entry_skipped_with_diagnostic(
        self, tmp_path, capsys,
    ) -> None:
        """Plugin author misconfigured: entry resolves to a string,
 not a Profile. Loader skips with a clear log line; other
 plugins still load."""
        from pipeline.profiles.loader import load_profiles_v2_with_plugins
        from pipeline.runtime import (
            PhaseStep,
            Profile,
            ProfileKind,
        )

        path = tmp_path / "profiles.json"
        path.write_text(
            '{"shipped": {"kind": "custom", "variant": null, '
            '"description": "test", "steps": ['
            '{"phase": "plan", "execution": "linear"}'
            "]}}",
            encoding="utf-8",
        )

        valid = Profile(
            name="ok",
            kind=ProfileKind.CUSTOM,
            variant=None,
            description="valid",
            steps=(PhaseStep(phase="plan", execution="linear"),),
        )
        with _patch_entry_points(
            [
                _FakeEP("malformed", "not a Profile"),
                _FakeEP("ok", valid),
            ],
            target_group="orcho.profiles",
        ):
            profiles = load_profiles_v2_with_plugins(path)

        assert "malformed" not in profiles
        assert "ok" in profiles
        captured = capsys.readouterr().out
        assert "malformed" in captured
        assert "expected Profile instance" in captured
